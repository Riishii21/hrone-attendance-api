"""Generate a ~100k-record test dataset full of the awkward cases the business rules describe.

    python tools/seed_test_data.py            # uses MONGO_URI / MONGO_DB (env or .env); DROPS both collections

What it contains (Jan-Sep 2026):
  * ~690 employees in 7 departments, including "Sales" and "sales" (department is case-sensitive)
  * day shifts and overnight shifts (22:00-06:00, 21:00-05:00), some punch-ins after midnight
  * mid-month joiners, employees who join after the analysed months, employees with no logs at all
  * weekday gaps (no record), ABSENT and LEAVE records, weekend records (some late)
  * half days, work hours right at the 4.49 / 4.50 boundary, open records (no punch-out)
  * legacy records without history / half_day / late_minutes / overtime_minutes
  * logs for an employee that does not exist (must be ignored)
  * a crafted "Ties" department for the leaderboard (Aug 2026: 120, 90, 90, 60, 60 late minutes)
  * a "Future" department whose people join in Dec 2026 (headcount 0 for the analysed months)
Derived fields are computed with the rules R1-R5, as the API would have stored them.
Like the grader's dataset, it is seeded with no indexes; the app creates them at startup.
"""
import os
import random
import sys
from datetime import date, datetime, time, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()
from app.main import attendance_date, derive_fields, shift_window  # noqa: E402  (reuse R1-R5 for stored values)

rng = random.Random(20260710)
UTC = timezone.utc
START, END = date(2026, 1, 1), date(2026, 9, 30)

DEPARTMENTS = {"Engineering": 200, "Sales": 150, "sales": 25, "Support": 120, "Operations": 135, "HR": 50}
SHIFTS_DAY = [("09:30", "18:30"), ("09:00", "18:00"), ("10:00", "19:00")]
SHIFTS_NIGHT = [("22:00", "06:00"), ("21:00", "05:00")]


def rand_join() -> str:
    r = rng.random()
    if r < 0.70:
        d = date(2025, 1, 1) + timedelta(days=rng.randrange(365))
    elif r < 0.95:  # joiners during the analysed period, often mid-month
        d = START + timedelta(days=rng.randrange((END - START).days))
    else:  # join after the analysed period
        d = date(2026, 10, 1) + timedelta(days=rng.randrange(60))
    return d.isoformat()


def employee(code: int, dept: str, joined: str, shift=None) -> dict:
    ss, se = shift or (rng.choice(SHIFTS_NIGHT) if rng.random() < 0.10 else rng.choice(SHIFTS_DAY))
    return {
        "emp_code": f"EMP{code:04d}", "name": f"Person {code}", "email": f"p{code}@example.com",
        "department": dept, "shift_start": ss, "shift_end": se, "joined_on": joined,
        "created_at": datetime.combine(date.fromisoformat(joined), time(4, 0), UTC),
    }


def record(emp: dict, day: date, status: str, late_off_min: float, hours: float | None) -> dict:
    """A record whose punch-in is `late_off_min` minutes after shift start and which lasts `hours`."""
    if status in ("ABSENT", "LEAVE"):
        doc = {"emp_code": emp["emp_code"], "date": day.isoformat(), "status": status, "punch_in": None,
               "punch_out": None, **derive_fields(status, None, None, day, emp), "history": []}
        return doc
    start, _ = shift_window(day, emp)
    pin = (start + timedelta(seconds=int(late_off_min * 60))).astimezone(UTC)
    pout = None if hours is None else pin + timedelta(seconds=int(hours * 3600))
    real_day = attendance_date(pin, emp)
    doc = {"emp_code": emp["emp_code"], "date": real_day.isoformat(), "status": status, "punch_in": pin,
           "punch_out": pout, **derive_fields(status, pin, pout, real_day, emp), "history": []}
    return doc


def random_logs(emp: dict) -> list:
    out = []
    first = max(START, date.fromisoformat(emp["joined_on"]))
    day = first
    while day <= END:
        weekend = day.weekday() >= 5
        r = rng.random()
        if weekend and r > 0.05:
            day += timedelta(days=1)
            continue
        if not weekend and r < 0.05:  # weekday with no record at all
            day += timedelta(days=1)
            continue
        if not weekend and r < 0.10:
            out.append(record(emp, day, "ABSENT", 0, None))
        elif not weekend and r < 0.15:
            out.append(record(emp, day, "LEAVE", 0, None))
        else:
            status = rng.choices(["PRESENT", "WFH", "ON_DUTY"], [80, 12, 8])[0]
            off = rng.uniform(-20, 9.9) if rng.random() < 0.78 else rng.uniform(10, 150)
            if rng.random() < 0.02:
                off = rng.choice([10.0, 10 + 1 / 60])  # exactly at / one second past the grace period
            h = rng.uniform(8.3, 10.2)
            hr = rng.random()
            if hr < 0.06:
                h = rng.uniform(2, 4.4)
            elif hr < 0.08:
                h = rng.choice([16181, 16182, 16199, 16200]) / 3600  # 4.49 / 4.50 boundary
            if day >= END - timedelta(days=3) and rng.random() < 0.3:
                h = None  # still open
            out.append(record(emp, day, status, off, h))
        day += timedelta(days=1)
    return out


def legacyfy(doc: dict) -> dict:
    r = rng.random()
    if r < 0.03:
        doc.pop("history", None)
        doc.pop("half_day", None)
    elif r < 0.04 and not doc.get("late_minutes") and not doc.get("overtime_minutes"):
        doc.pop("late_minutes", None)
        doc.pop("overtime_minutes", None)
    return doc


def main():
    uri, name = os.environ.get("MONGO_URI"), os.environ.get("MONGO_DB")
    if not uri or not name:
        sys.exit("set MONGO_URI and MONGO_DB")
    db = MongoClient(uri, tz_aware=True)[name]
    db.employees.drop()
    db.attendance_logs.drop()

    emps, logs, code = [], [], 1
    for dept, n in DEPARTMENTS.items():
        for _ in range(n):
            emps.append(employee(code, dept, rand_join()))
            code += 1
    no_log_codes = {e["emp_code"] for e in rng.sample(emps, 15)}  # employees with zero logs

    # Leaderboard ties (August 2026): totals 120, 90, 90, 60, 60, 0 inside "Ties"
    ties = [employee(9001 + i, "Ties", "2026-01-05", ("09:30", "18:30")) for i in range(6)]
    plan = {0: [30, 30, 30, 30], 1: [30, 30, 30], 2: [45, 45], 3: [30, 30], 4: [60], 5: []}
    for i, e in enumerate(ties):
        days = [date(2026, 8, d) for d in (3, 4, 5, 6, 7, 10, 11)]
        lates = plan[i] + [0] * (len(days) - len(plan[i]))
        for d, late in zip(days, lates):
            logs.append(record(e, d, "PRESENT", late + 0.5 if late else -5, 9))  # +30 s keeps floor exact
    emps += ties
    emps += [employee(9101, "Future", "2026-12-01"), employee(9102, "Future", "2026-12-15")]

    for e in emps:
        if e["emp_code"] in no_log_codes or e["department"] in ("Ties", "Future"):
            continue
        logs.extend(legacyfy(d) for d in random_logs(e))

    ghost = {"emp_code": "EMP999999", "shift_start": "09:30", "shift_end": "18:30"}  # not in employees
    for m in range(1, 10):
        logs.append(record(ghost, date(2026, m, 15 if date(2026, m, 15).weekday() < 5 else 16), "PRESENT", 400, 9))

    db.employees.insert_many(emps)
    for i in range(0, len(logs), 5000):
        db.attendance_logs.insert_many(logs[i:i + 5000])
    print(f"seeded {len(emps)} employees and {len(logs)} attendance records into {name}")


if __name__ == "__main__":
    main()
