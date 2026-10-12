"""Pure-logic tests for R1-R5 (no database needed). Run: python -m pytest tests/test_rules.py"""
import os
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.main import (IST, attendance_date, compute_late_minutes, compute_overtime, compute_work_hours,
                      derive_fields, dt_to_ms, ms_to_dt, round_half_up, working_days)

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def test_r1_truncation_and_roundtrip():
    assert dt_to_ms(ms_to_dt(1783312500900)) == 1783312500000
    assert dt_to_ms(ms_to_dt(1783312500000)) == 1783312500000


def test_r1_ist_date_early_morning():
    # 04:00 IST on the 7th is 22:30 UTC on the 6th: date must be the 7th
    assert attendance_date(ist(2026, 7, 7, 4, 0).astimezone(timezone.utc), DAY) == date(2026, 7, 7)


def test_r1_overnight():
    assert attendance_date(ist(2026, 7, 6, 21, 55), NIGHT) == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 0, 30), NIGHT) == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), NIGHT) == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), NIGHT) == date(2026, 7, 7)


def test_r2_examples():
    d = date(2026, 7, 6)
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), d, DAY) == 0
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), d, DAY) == 10
    assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), d, DAY) == 45
    assert compute_late_minutes(ist(2026, 7, 6, 9, 0), d, DAY) == 0
    assert compute_late_minutes(ist(2026, 7, 6, 10, 5), d, DAY) == 35


def test_r2_overnight_after_midnight():
    assert compute_late_minutes(ist(2026, 7, 7, 0, 30), date(2026, 7, 6), NIGHT) == 150


def test_r3():
    d = date(2026, 7, 6)
    assert compute_overtime(ist(2026, 7, 6, 18, 35), d, DAY) == 0
    assert compute_overtime(ist(2026, 7, 6, 18, 59, 59), d, DAY) == 0
    assert compute_overtime(ist(2026, 7, 6, 19, 0), d, DAY) == 30
    assert compute_overtime(ist(2026, 7, 6, 19, 10), d, DAY) == 40
    assert compute_overtime(ist(2026, 7, 6, 17, 0), d, DAY) == 0
    assert compute_overtime(ist(2026, 7, 7, 6, 40), d, NIGHT) == 40  # sample 7


def test_r3_boundary_29_59_is_not_30():
    # 29 min 59 s is floor 29 -> below the minimum -> 0
    assert compute_overtime(ist(2026, 7, 6, 18, 59, 59), date(2026, 7, 6), DAY) == 0


def test_r4_half_up():
    pin = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(pin, pin + timedelta(seconds=16182)) == Decimal("4.50")  # 4.495 -> 4.50
    assert compute_work_hours(pin, pin + timedelta(seconds=16181)) == Decimal("4.49")
    assert compute_work_hours(pin, pin + timedelta(hours=9, minutes=7)) == Decimal("9.12")  # sample 1
    assert compute_work_hours(pin, pin + timedelta(seconds=18)) == Decimal("0.01")  # 0.005


def test_r5_uses_rounded_hours():
    pin = ist(2026, 7, 6, 9, 0)
    d = derive_fields("PRESENT", pin, pin + timedelta(seconds=16182), date(2026, 7, 6), DAY)
    assert d["work_hours"] == 4.5 and d["half_day"] is False
    d = derive_fields("PRESENT", pin, pin + timedelta(seconds=16181), date(2026, 7, 6), DAY)
    assert d["work_hours"] == 4.49 and d["half_day"] is True


def test_sample_overnight_record():
    pin = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)
    pout = datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc)
    d = derive_fields("PRESENT", pin, pout, attendance_date(pin, NIGHT), NIGHT)
    assert d == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


def test_absent_derived():
    assert derive_fields("ABSENT", None, None, date(2026, 7, 6), DAY) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_working_days():
    assert working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert working_days(date(2026, 7, 13), date(2026, 7, 31)) == 15
    assert working_days(date(2026, 8, 1), date(2026, 7, 31)) == 0
    assert working_days(date(2026, 7, 4), date(2026, 7, 5)) == 0  # Sat-Sun
    for s in range(1, 29):
        a, b = date(2026, 2, s), date(2026, 3, 31)
        brute = sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).weekday() < 5)
        assert working_days(a, b) == brute


def test_round_half_up():
    assert round_half_up(Decimal("2.675"), 2) == 2.68
    assert round_half_up(2.675, 2) == 2.68  # float repr '2.675'
    assert round_half_up(Decimal(1) / 3 * 100, 2) == 33.33
