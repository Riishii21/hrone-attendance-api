"""End-to-end checks against a running API and a real MongoDB.

    python tools/seed_test_data.py                      # fresh ~100k dataset (drops the collections)
    uvicorn app.main:app --port 8000                    # in another terminal
    python tools/verify.py [--base http://localhost:8000]

It reads the raw documents straight from MongoDB, recomputes every analytics answer with an
independent, deliberately naive Python implementation of the rules, and compares it with what the
API returns. It also checks explain plans (IXSCAN, no COLLSCAN), latency, and the race conditions.
Race-test data is written to department "ZZVerify" in 2027, outside the analysed months.
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
IST = timezone(timedelta(hours=5, minutes=30))
FAILS: list[str] = []
SLOW: list[str] = []


# ----------------------------------------------------------------- http
def call(base, method, path, params=None, body=None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, payload = r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"null")
    ms = (time.perf_counter() - t) * 1000
    if ms > 1000:
        SLOW.append(f"{method} {path} {params} took {ms:.0f} ms")
    return status, payload


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(f"{label}: {detail}"[:1500])


# ----------------------------------------------------------------- reference implementation
def q(x, places):
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_range(month):
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return first, last


def weekday(d: str) -> bool:
    return date.fromisoformat(d).weekday() < 5


def pval(log):
    if log["status"] not in PRESENCE:
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day") is True else Decimal(1)


class Ref:
    def __init__(self, db):
        self.emps = {e["emp_code"]: e for e in db.employees.find({}, {"_id": 0})}
        self.by_emp = defaultdict(list)
        for log in db.attendance_logs.find({}, {"_id": 0, "history": 0}):
            self.by_emp[log["emp_code"]].append(log)

    def logs(self, code, first, last):
        a, b = first.isoformat(), last.isoformat()
        return [l for l in self.by_emp.get(code, []) if a <= l["date"] <= b]

    def monthly(self, code, month):
        first, last = month_range(month)
        e = self.emps[code]
        start = max(first, date.fromisoformat(e["joined_on"]))
        wd = sum(1 for i in range((last - start).days + 1) if (start + timedelta(i)).weekday() < 5) if start <= last else 0
        ls = self.logs(code, first, last)
        present = sum((pval(l) for l in ls if weekday(l["date"])), Decimal(0))
        return {
            "emp_code": code, "month": month, "working_days": wd, "present_days": q(present, 2),
            "leave_days": sum(1 for l in ls if l["status"] == "LEAVE"),
            "late_count": sum(1 for l in ls if (l.get("late_minutes") or 0) > 0),
            "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls),
            "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in ls),
            "attendance_pct": q(present / wd * 100, 2) if wd else None,
        }

    def summary(self, month, dept=None):
        first, last = month_range(month)
        groups = defaultdict(list)
        for e in self.emps.values():
            if e["joined_on"] <= last.isoformat() and (dept is None or e["department"] == dept):
                groups[e["department"]].append(e["emp_code"])
        items = []
        for d in sorted(groups):
            ls = [l for c in groups[d] for l in self.logs(c, first, last)]
            wh = [Decimal(str(l["work_hours"])) for l in ls
                  if l["status"] in PRESENCE and isinstance(l.get("work_hours"), (int, float))]
            items.append({
                "department": d, "headcount": len(groups[d]),
                "present_days": q(sum((pval(l) for l in ls if weekday(l["date"])), Decimal(0)), 2),
                "avg_work_hours": q(sum(wh) / len(wh), 2) if wh else None,
                "late_count": sum(1 for l in ls if (l.get("late_minutes") or 0) > 0),
                "total_late_minutes": sum(l.get("late_minutes") or 0 for l in ls if (l.get("late_minutes") or 0) > 0),
                "leave_count": sum(1 for l in ls if l["status"] == "LEAVE"),
                "on_duty_count": sum(1 for l in ls if l["status"] == "ON_DUTY"),
            })
        return {"month": month, "items": items}

    def leaderboard(self, month, limit, dept=None):
        first, last = month_range(month)
        rows = []
        for code, e in self.emps.items():
            if dept is not None and e["department"] != dept:
                continue
            late = [l["late_minutes"] for l in self.logs(code, first, last) if (l.get("late_minutes") or 0) > 0]
            if late:
                rows.append({"emp_code": code, "name": e["name"], "department": e["department"],
                             "total_late_minutes": sum(late), "late_count": len(late)})
        rows.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))
        out = []
        for r in rows:
            rank = 1 + sum(1 for o in rows if o["total_late_minutes"] > r["total_late_minutes"])
            if rank <= limit:
                out.append({"rank": rank, **r})
        return {"month": month, "items": out}

    def trend(self, dept, d_from, d_to):
        codes = [c for c, e in self.emps.items() if e["department"] == dept]
        items, rates = [], []
        n = (d_to - d_from).days + 1
        for i in range(n):
            d = d_from + timedelta(i)
            ds = d.isoformat()
            hc = sum(1 for c in codes if self.emps[c]["joined_on"] <= ds)
            ls = [l for c in codes for l in self.by_emp.get(c, []) if l["date"] == ds]
            present = sum((pval(l) for l in ls), Decimal(0))
            working = d.weekday() < 5
            rate = Decimal(str(q(present / hc, 4))) if working and hc > 0 else None
            rates.append(rate)
            window = [r for r in rates[max(0, i - 6): i + 1] if r is not None]
            items.append({
                "date": ds, "is_working_day": working, "headcount": hc, "present_count": float(present),
                "late_count": sum(1 for l in ls if (l.get("late_minutes") or 0) > 0),
                "attendance_rate": float(rate) if rate is not None else None,
                "moving_avg_7d": q(sum(window) / len(window), 4) if window else None,
            })
        return {"department": dept, "items": items}


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return a == b


def first_diff(a, b, path="$"):
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return f"{path}: keys {sorted(a)} != {sorted(b)}"
        for k in a:
            d = first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if same(a, b) else f"{path}: api={a!r} expected={b!r}"


def compare(label, status, got, expected):
    check(label, status == 200, f"status {status}: {got}")
    if status == 200:
        d = first_diff(got, expected)
        check(label, d is None, d or "")


# ----------------------------------------------------------------- suites
MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]


def analytics(base, ref):
    depts = sorted({e["department"] for e in ref.emps.values()} | {"Nope"})
    for month in MONTHS:
        s, p = call(base, "GET", "/analytics/departments/summary", {"month": month})
        compare(f"summary {month}", s, p, ref.summary(month))
        for d in depts:
            s, p = call(base, "GET", "/analytics/departments/summary", {"month": month, "department": d})
            compare(f"summary {month} {d}", s, p, ref.summary(month, d))
        for limit in (1, 3, 5, 10, 50):
            s, p = call(base, "GET", "/analytics/leaderboard/late", {"month": month, "limit": limit})
            compare(f"leaderboard {month} limit={limit}", s, p, ref.leaderboard(month, limit))
        for d in depts:
            for limit in (1, 2, 3, 4):
                s, p = call(base, "GET", "/analytics/leaderboard/late", {"month": month, "limit": limit, "department": d})
                compare(f"leaderboard {month} {d} limit={limit}", s, p, ref.leaderboard(month, limit, d))

    rng = random.Random(7)
    codes = sorted(ref.emps)
    sample = rng.sample(codes, min(80, len(codes)))
    sample += [c for c, e in ref.emps.items() if "2026-01" <= e["joined_on"][:7] <= "2026-09"][:20]  # joiners
    sample += [c for c, e in ref.emps.items() if e["shift_end"] <= e["shift_start"]][:10]  # overnight
    for code in sample:
        for month in rng.sample(MONTHS, 3) + [ref.emps[code]["joined_on"][:7]]:
            if month[:4] != "2026":
                continue
            s, p = call(base, "GET", f"/analytics/employees/{code}/monthly", {"month": month})
            compare(f"monthly {code} {month}", s, p, ref.monthly(code, month))

    ranges = [(date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 2, 25), date(2026, 3, 10)),
              (date(2026, 8, 1), date(2026, 8, 1)), (date(2026, 7, 1), date(2026, 9, 30)),
              (date(2026, 9, 20), date(2026, 10, 15))]
    for d in depts:
        if d == "Nope":
            continue
        for a, b in ranges:
            s, p = call(base, "GET", f"/analytics/departments/{urllib.parse.quote(d)}/trend",
                        {"from": a.isoformat(), "to": b.isoformat()})
            compare(f"trend {d} {a}..{b}", s, p, ref.trend(d, a, b))

    # error contracts
    expect = [
        ("GET", "/analytics/departments/Nope/trend", {"from": "2026-07-01", "to": "2026-07-31"}, 404),
        ("GET", "/analytics/departments/Engineering/trend", {"from": "2026-07-31", "to": "2026-07-01"}, 422),
        ("GET", "/analytics/departments/Engineering/trend", {"from": "2026-07-01", "to": "2026-10-01"}, 422),  # 93 days
        ("GET", "/analytics/departments/Engineering/trend", {"from": "2026-07-01"}, 422),
        ("GET", "/analytics/employees/EMP99999/monthly", {"month": "2026-07"}, 404),
        ("GET", "/analytics/employees/EMP0001/monthly", {"month": "2026-13"}, 422),
        ("GET", "/analytics/departments/summary", {"month": "2026-7"}, 422),
        ("GET", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 51}, 422),
        ("GET", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 0}, 422),
        ("GET", "/attendance", {"date_from": "2026-07-10", "date_to": "2026-07-01"}, 422),
        ("GET", "/attendance", {"page_size": 101}, 422),
        ("GET", "/admin/explain/nope", None, 422),
        ("GET", "/admin/explain/employee_monthly", {"month": "2026-07"}, 422),
    ]
    for method, path, params, code in expect:
        s, p = call(base, method, path, params)
        check(f"{path} {params}", s == code, f"expected {code}, got {s}: {p}")


def lists(base, db):
    total = db.attendance_logs.count_documents({})
    s, p = call(base, "GET", "/attendance", {"page_size": 100})
    check("attendance total", s == 200 and p["total"] == total, f"{p.get('total') if s == 200 else p}")
    if s == 200:
        keys = [(i["date"], i["emp_code"]) for i in p["items"]]
        check("attendance sort", keys == sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1])), str(keys[:5]))
        check("attendance no ids", all("_id" not in i and "id" not in i for i in p["items"]))
    flt = {"date_from": "2026-07-01", "date_to": "2026-07-31", "status": "WFH"}
    s, p = call(base, "GET", "/attendance", {**flt, "page": 3, "page_size": 7})
    expected = db.attendance_logs.count_documents({"date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "WFH"})
    check("attendance filtered total", s == 200 and p["total"] == expected and len(p["items"]) == 7, str(p)[:200])
    s, p = call(base, "GET", "/employees", {"department": "Sales", "page": 2, "page_size": 10})
    check("employees filtered total", s == 200 and p["total"] == db.employees.count_documents({"department": "Sales"}))
    if s == 200:
        codes = sorted(e["emp_code"] for e in db.employees.find({"department": "Sales"}))
        check("employees page 2", [e["emp_code"] for e in p["items"]] == codes[10:20])


def explains(base, ref):
    some_emp = sorted(ref.emps)[0]
    cases = [
        ("attendance_list", {}), ("attendance_list", {"emp_code": some_emp}),
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("attendance_list", {"status": "LEAVE", "page": 3}),
        ("attendance_list", {"emp_code": some_emp, "date_from": "2026-07-01", "status": "PRESENT"}),
        ("employee_monthly", {"emp_code": some_emp, "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}), ("department_summary", {"month": "2026-07", "department": "Sales"}),
        ("late_leaderboard", {"month": "2026-07"}), ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 3}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-09-30"}),
    ]
    for name, params in cases:
        s, p = call(base, "GET", f"/admin/explain/{name}", params)
        text = json.dumps(p)
        check(f"explain {name} {params}", s == 200 and "IXSCAN" in text and "COLLSCAN" not in text,
              f"status {s}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text}")
        if s == 200:
            check(f"explain {name} shape", {"endpoint", "collection", "explain"} <= p.keys())


def races(base):
    tag = random.randrange(10000, 99999)
    code = f"EMP{tag}"
    body = {"emp_code": code, "name": "Race", "email": "r@x.com", "department": "ZZVerify", "joined_on": "2027-12-01"}
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: call(base, "POST", "/employees", body=body)[0], range(20)))
    check("race create employee", sorted(codes) == [201] + [409] * 19, str(codes))

    pin = int(datetime(2027, 12, 6, 9, 50, tzinfo=IST).timestamp() * 1000)
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(lambda _: call(base, "POST", "/attendance/punch-in",
                                           body={"emp_code": code, "punched_at": pin})[0], range(20)))
    check("race punch-in", sorted(codes) == [201] + [409] * 19, str(codes))

    pout = int(datetime(2027, 12, 6, 19, 5, tzinfo=IST).timestamp() * 1000)
    with ThreadPoolExecutor(20) as ex:
        res = list(ex.map(lambda _: call(base, "POST", "/attendance/punch-out",
                                         body={"emp_code": code, "punched_at": pout}), range(20)))
    codes = [r[0] for r in res]
    check("race punch-out", sorted(codes) == [200] + [409] * 19, str(codes))
    ok = [r[1] for r in res if r[0] == 200]
    if ok:
        r = ok[0]
        check("punch-out values", (r["late_minutes"], r["overtime_minutes"], r["work_hours"], r["half_day"]) == (20, 35, 9.25, False), str(r))

    def patch(i):
        return call(base, "PATCH", f"/attendance/{code}/2027-12-06",
                    body={"punch_out": pout + (i + 1) * 60_000, "reason": f"correction {i}", "regularized_by": f"hr{i}"})[0]
    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(patch, range(20)))
    s, p = call(base, "GET", "/attendance", {"emp_code": code})
    hist = p["items"][0]["history"] if s == 200 and p["items"] else []
    check("race patch keeps history", set(codes) <= {200, 409} and codes.count(200) == len(hist) >= 1,
          f"codes={codes} history={len(hist)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--skip-races", action="store_true")
    args = ap.parse_args()
    db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]

    s, _ = call(args.base, "GET", "/health")
    if s != 200:
        sys.exit(f"/health returned {s}")
    t = time.perf_counter()
    ref = Ref(db)
    print(f"loaded {len(ref.emps)} employees, {sum(len(v) for v in ref.by_emp.values())} records")
    for name, fn in [("lists", lambda: lists(args.base, db)), ("analytics", lambda: analytics(args.base, ref)),
                     ("explain", lambda: explains(args.base, ref))] + \
                    ([] if args.skip_races else [("races", lambda: races(args.base))]):
        before = len(FAILS)
        fn()
        print(f"{name:10s} {'OK' if len(FAILS) == before else f'{len(FAILS) - before} FAILED'}")
    print(f"done in {time.perf_counter() - t:.1f}s")
    for f in FAILS[:40]:
        print("FAIL", f)
    for s in SLOW[:10]:
        print("SLOW", s)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
