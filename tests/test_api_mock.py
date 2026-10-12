"""API tests against mongomock (in-memory). Covers CRUD, validation, R1-R5 end to end and the
concurrency guards. Analytics pipelines need a real MongoDB: see verify.py.
Run: python -m pytest tests/test_api_mock.py"""
import os
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m


def ms(y, mo, d, h, mi, s=0, extra_ms=0):
    """IST wall-clock -> epoch ms."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=m.IST).timestamp()) * 1000 + extra_ms


@pytest.fixture()
def api(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    mdb = mc["t"]
    monkeypatch.setattr(m, "client", mc)
    monkeypatch.setattr(m, "db", mdb)
    monkeypatch.setattr(m, "employees_col", mdb["employees"])
    monkeypatch.setattr(m, "logs_col", mdb["attendance_logs"])
    with TestClient(m.app) as c:
        for code, dept, ss, se, joined in [("EMP0001", "Eng", "09:30", "18:30", "2026-01-05"),
                                           ("EMP0002", "Eng", "09:30", "18:30", "2026-07-13"),
                                           ("EMP0005", "Support", "22:00", "06:00", "2026-03-10")]:
            r = c.post("/employees", json={"emp_code": code, "name": "N " + code, "email": f"{code}@x.com",
                                           "department": dept, "shift_start": ss, "shift_end": se, "joined_on": joined})
            assert r.status_code == 201, r.text
        yield c


# ---------------- employees ----------------
def test_employee_shape_and_duplicates(api):
    r = api.post("/employees", json={"emp_code": "EMP0001", "name": "x", "email": "a@b.co", "department": "Eng",
                                     "joined_on": "2026-01-01"})
    assert r.status_code == 409
    items = api.get("/employees").json()["items"]
    assert isinstance(items[0]["created_at"], int) and "_id" not in items[0]


@pytest.mark.parametrize("patch", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"name": ""}, {"joined_on": "2026-02-30"},
    {"joined_on": "20260101"}, {"shift_start": "25:00"}, {"shift_start": "18:30", "shift_end": "18:30"},
    {"department": ""},
])
def test_employee_validation(api, patch):
    body = {"emp_code": "EMP0099", "name": "x", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-01"}
    body.update(patch)
    assert api.post("/employees", json=body).status_code == 422


def test_employee_list_filter_pagination(api):
    r = api.get("/employees", params={"department": "Eng", "page": 1, "page_size": 1}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0001"]
    r = api.get("/employees", params={"department": "Eng", "page": 2, "page_size": 1}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0002"]
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_concurrent_employee_create(api):
    body = {"emp_code": "EMP0777", "name": "x", "email": "a@b.co", "department": "Eng", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: api.post("/employees", json=body).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


# ---------------- punch-in ----------------
def test_punch_in_shape_late_truncation(api):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 1, 900)})
    assert r.status_code == 201
    b = r.json()
    assert b == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                 "punch_in": ms(2026, 7, 6, 9, 40, 1), "punch_out": None, "work_hours": None, "late_minutes": 10,
                 "overtime_minutes": 0, "half_day": False, "history": []}


def test_punch_in_grace_boundary_with_ms(api):
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 0, 900)}).json()
    assert b["late_minutes"] == 0 and b["punch_in"] == ms(2026, 7, 6, 9, 40, 0)


def test_punch_in_ist_date(api):
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 2, 0)}).json()
    assert b["date"] == "2026-07-07"


def test_punch_in_overnight(api):
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 0, 30)}).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 150


@pytest.mark.parametrize("value", [1783312500, "1783312500000", 1783312500000.0, 1783312500000.5, True,
                                   99_999_999_999, 4_102_444_800_001, None])
def test_punch_in_bad_timestamps(api, value):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "X"])
def test_punch_in_bad_status(api, status):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0), "status": status})
    assert r.status_code == 422


def test_punch_in_unknown_and_duplicate(api):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    assert api.post("/attendance/punch-in", json=body).status_code == 201
    body["punched_at"] = ms(2026, 7, 6, 13, 0)
    assert api.post("/attendance/punch-in", json=body).status_code == 409


def test_concurrent_punch_in(api):
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert sorted(codes) == [201] + [409] * 9


# ---------------- punch-out ----------------
def test_punch_out_values_and_conflict(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (9.08, 35, 40, False)
    assert b["history"] == []
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 20)}).status_code == 409


def test_punch_out_half_day_rounding(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0) + 16182_000}).json()
    assert b["work_hours"] == 4.5 and b["half_day"] is False


def test_punch_out_overnight(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    b = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert (b["date"], b["work_hours"], b["overtime_minutes"]) == ("2026-07-06", 8.75, 40)


def test_punch_out_errors(api):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code == 404
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    # no record with punch_in <= punched_at -> 404 (the contract's selection rule)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 8, 0)}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0, 1)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": "x"}).status_code == 422


def test_concurrent_punch_out(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: api.post("/attendance/punch-out", json=body).status_code, range(10)))
    assert sorted(codes) == [200] + [409] * 9


# ---------------- list ----------------
def test_list_attendance(api):
    for code, d in [("EMP0001", 6), ("EMP0002", 6), ("EMP0001", 7)]:
        api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, d, 9, 0)})
    r = api.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001"),
                                                                ("2026-07-06", "EMP0002")]
    assert all("id" not in i and "_id" not in i for i in r["items"])
    r = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06", "page_size": 1}).json()
    assert r["total"] == 2 and len(r["items"]) == 1
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422
    assert api.get("/attendance", params={"status": "LATE"}).status_code == 422


# ---------------- PATCH ----------------
def seed_open(api, code="EMP0001", h=10, mi=5):
    api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, 7, h, mi)})
    api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": ms(2026, 7, 7, 18, 30)})


def test_patch_punch_in_history(api):
    seed_open(api)
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch",
                                                         "regularized_by": "hr.admin", "late_minutes": 999})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["late_minutes"] == 0 and b["punch_in"] == ms(2026, 7, 7, 9, 28)
    h = b["history"]
    assert len(h) == 1 and h[0]["by"] == "hr.admin" and isinstance(h[0]["at"], int)
    assert h[0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert h[0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert set(h[0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    # a second correction appends, never overwrites
    r = api.patch("/attendance/EMP0001/2026-07-07", json={"status": "WFH", "reason": "was at home", "regularized_by": "hr"})
    assert len(r.json()["history"]) == 2 and r.json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_patch_rules(api):
    seed_open(api)
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "fixing it", "regularized_by": "hr"}
    assert api.patch(url, json={**base, "status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0)}).status_code == 422
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 0)}).status_code == 422  # leaves the date
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422  # before punch_in
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 8, 11, 0)}).status_code == 422  # > 24 h
    assert api.patch(url, json={**base, "status": "PRESENT"}).status_code == 422  # changes nothing
    assert api.patch(url, json={**base, "punch_in": None}).status_code == 422
    assert api.patch(url, json={"reason": "abc", "regularized_by": "hr", "status": "WFH"}).status_code == 422  # reason too short
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "WFH"}).status_code == 404
    assert api.patch("/attendance/EMP9999/2026-07-07", json={**base, "status": "WFH"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-7-7", json={**base, "status": "WFH"}).status_code == 422
    r = api.patch(url, json={**base, "status": "LEAVE"}).json()
    assert (r["punch_in"], r["punch_out"], r["work_hours"], r["late_minutes"]) == (None, None, None, 0)
    # back to presence requires a punch_in
    assert api.patch(url, json={**base, "status": "PRESENT"}).status_code == 422
    r = api.patch(url, json={**base, "status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 45)})
    assert r.status_code == 200 and r.json()["late_minutes"] == 15 and r.json()["work_hours"] is None


def test_patch_legacy_record_without_history(api):
    m.logs_col.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                           "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                           "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                           "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    rec = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert rec["half_day"] is False and rec["history"] == []
    r = api.patch("/attendance/EMP0002/2026-07-14", json={"status": "ON_DUTY", "reason": "client visit", "regularized_by": "hr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1


def test_concurrent_patch_never_loses_history(api):
    seed_open(api)
    def go(i):
        return api.patch("/attendance/EMP0001/2026-07-07",
                         json={"punch_out": ms(2026, 7, 7, 18, 31 + i), "reason": f"fix number {i}", "regularized_by": "hr"}).status_code
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(go, range(10)))
    ok = codes.count(200)
    assert ok >= 1 and set(codes) <= {200, 409}
    assert len(api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]) == ok


# ---------------- regressions from the external review ----------------
def test_punch_out_selected_closed_record_is_409(api):
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)})
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0)})
    # selected record = closed 6 July record (latest punch_in <= 08:00 on the 7th)
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 8, 0)})
    assert r.status_code == 409


@pytest.mark.parametrize("month", ["0000-01", "2026-00", "2026-13", "26-01"])
def test_unusable_month_is_422(api, month):
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": month}).status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": month}).status_code == 422
    assert api.get("/admin/explain/department_summary", params={"month": month}).status_code == 422


def test_trend_pipeline_builds_for_last_calendar_day():
    from datetime import date
    m.department_trend_pipeline("Eng", date(9999, 12, 31), date(9999, 12, 31))  # must not overflow
    assert m.trend_range("9999-12-31", "9999-12-31") == (date(9999, 12, 31), date(9999, 12, 31))


def test_failed_unique_index_is_not_downgraded(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    mdb = mc["dup"]
    mdb.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"},
                                     {"emp_code": "EMP0001", "date": "2026-07-06"}])
    monkeypatch.setattr(m, "db", mdb)
    monkeypatch.setattr(m, "INDEXES_READY", m.threading.Event())
    with pytest.raises(Exception):
        m.ensure_indexes()
    assert not m.INDEXES_READY.is_set()
    names = [i["name"] for i in mdb.attendance_logs.list_indexes()]
    assert not any("nonunique" in n for n in names)


def test_server_instants_are_whole_seconds(api):
    e = api.get("/employees", params={"page_size": 1}).json()["items"][0]
    assert e["created_at"] % 1000 == 0
