"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real environment variables win)

"""
import json
import logging
import os
import re
import threading
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from bson.json_util import JSONOptions, JSONMode
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # .env only fills gaps; real environment variables take priority

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set (environment or .env)")

# tz_aware=True: every datetime PyMongo hands back is an aware UTC datetime, so arithmetic and
# .timestamp() never silently use the server's local time zone.
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees_col = db["employees"]
logs_col = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000  # anything smaller looks like seconds
MAX_MS = 4_102_444_800_000  # 2100-01-01

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE = ["ABSENT", "LEAVE"]
ALL_STATUSES = PRESENCE + ABSENCE
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

MAX_TREND_DAYS = 92
MAX_SHIFT = timedelta(hours=24)

StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceT = Literal["PRESENT", "WFH", "ON_DUTY"]


def unprocessable(msg: str, loc: tuple = ("body",)) -> HTTPException:
    """A 422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(status_code=422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-UTC) datetime -> epoch ms. Integer arithmetic, no float rounding."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    """Current instant truncated to whole seconds (R1); used for every server-generated instant."""
    return datetime.now(UTC).replace(microsecond=0)


def parse_date(value: str) -> date:
    """Strict YYYY-MM-DD (fromisoformat alone also accepts e.g. 20260701)."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    return date.fromisoformat(value)  # raises ValueError for 2026-02-30


def parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    # Zero-padded HH:MM strings compare correctly as strings.
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of punch_in; for an overnight shift, a punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(day: date, emp: dict) -> tuple[datetime, datetime]:
    """Shift start and end instants for the shift that starts on `day` (IST). An overnight
    shift ends on the next calendar day."""
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# 3. Business rules R2-R5 (inputs are already truncated to whole seconds)
# --------------------------------------------------------------------------- #
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since shift start."""
    start, _ = shift_window(day, emp)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    _, end = shift_window(day, emp)
    seconds = int((punch_out - end).total_seconds())
    if seconds <= 0:
        return 0
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded half-up to 2 decimals, done in Decimal (round() is banker's + float)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def round_half_up(value: Any, places: int) -> float:
    """R8 rounding for reported numbers."""
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def derive_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime],
                  day: date, emp: dict) -> dict:
    """The four derived fields for a record, per the table in DATA_MODEL.md section 3."""
    if status in ABSENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    derived = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = float(hours)
        derived["overtime_minutes"] = compute_overtime(punch_out, day, emp)
        derived["half_day"] = hours < HALF_DAY_HOURS  # R5 uses the *rounded* value
    return derived


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
# strict=True: rejects strings ("1783312500000"), floats (1783312500000.0) and booleans.
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]


def _reject_null(value: Any) -> Any:
    if value is None:
        raise ValueError("must not be null; omit the field instead")
    return value


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=HHMM_PATTERN)] = "09:30"
    shift_end: Annotated[str, Field(pattern=HHMM_PATTERN)] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        parse_date(v)
        return v

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceT = "PRESENT"

    _no_null = field_validator("punched_at", "status", mode="before")(_reject_null)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    _no_null = field_validator("punched_at", mode="before")(_reject_null)


class RegularizeRequest(BaseModel):
    """Derived fields (work_hours, late_minutes, ...) are not declared, so they are ignored."""
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    _no_null = field_validator("status", "punch_in", "punch_out", mode="before")(_reject_null)


def query_date(name: str, value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    try:
        return parse_date(value)
    except ValueError as exc:
        raise unprocessable(str(exc), ("query", name))


def query_month(value: Optional[str]) -> tuple[str, str]:
    """'YYYY-MM' -> ('YYYY-MM-01', 'YYYY-MM-<last>') as stored date strings."""
    if value is None:
        raise unprocessable("field required", ("query", "month"))
    if not MONTH_RE.match(value):
        raise unprocessable("month must be YYYY-MM", ("query", "month"))
    try:
        first = date(int(value[:4]), int(value[5:]), 1)
        last = first.replace(day=monthrange(first.year, first.month)[1])
    except ValueError:
        raise unprocessable("month is not a valid calendar month", ("query", "month"))
    return first.isoformat(), last.isoformat()


def require(name: str, value: Any) -> Any:
    if value is None:
        raise unprocessable("field required", ("query", name))
    return value


# --------------------------------------------------------------------------- #
# 5. Serialisation (no _id ever leaves the API; instants become epoch ms)
# --------------------------------------------------------------------------- #
def to_api(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_history(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": to_api(entry.get("changes") or {}),  # nested punch times are BSON dates too
    }


def serialize_record(doc: dict) -> dict:
    """Legacy documents may omit half_day / history / late / overtime: apply the documented defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history(h) for h in (doc.get("history") or [])],
    }


def page_params(page: int, page_size: int) -> tuple[int, int]:
    return (page - 1) * page_size, page_size


# --------------------------------------------------------------------------- #
# 6. Indexes and lifespan
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # Identity + the database-level guarantee behind 409 on concurrent duplicate creates.
        ({"keys": [("emp_code", ASCENDING)], "name": "emp_code_unique", "unique": True}),
        # GET /employees?department=..., sorted by emp_code; department summary / trend with a department.
        ({"keys": [("department", ASCENDING), ("emp_code", ASCENDING)], "name": "department_emp_code"}),
        # Department summary without a department filter (headcount: joined_on <= month end).
        ({"keys": [("joined_on", ASCENDING)], "name": "joined_on"}),
    ],
    "attendance_logs": [
        # One record per (emp_code, date): makes punch-in race-safe; serves per-employee ranges
        # (monthly summary, $lookup from employees) and PATCH lookups.
        ({"keys": [("emp_code", ASCENDING), ("date", ASCENDING)], "name": "emp_code_date_unique", "unique": True}),
        # GET /attendance default sort (date desc, emp_code asc) and month/day range scans.
        ({"keys": [("date", DESCENDING), ("emp_code", ASCENDING)], "name": "date_desc_emp_code"}),
        # GET /attendance?status=... with the same sort.
        ({"keys": [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], "name": "status_date_emp_code"}),
        # Punch-out: latest record whose punch_in <= punched_at.
        ({"keys": [("emp_code", ASCENDING), ("punch_in", DESCENDING)], "name": "emp_code_punch_in"}),
    ],
}


INDEXES_READY = threading.Event()  # /health stays 503 until the unique indexes really exist


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists. A unique index that
    cannot be built (e.g. duplicate data) raises: without it punch-in and employee creation are not
    race-safe, so the app must not pretend to be ready."""
    for coll_name, specs in INDEXES.items():
        coll = db[coll_name]
        for spec in specs:
            opts = {k: v for k, v in spec.items() if k != "keys"}
            coll.create_index(spec["keys"], **opts)
    INDEXES_READY.set()


def _ensure_indexes_in_background() -> None:
    while True:
        try:
            ensure_indexes()
            log.info("indexes ensured")
            return
        except ConnectionFailure as exc:  # only connectivity is worth retrying
            log.warning("MongoDB not reachable, retrying index creation: %s", exc)
            time.sleep(2)
        except PyMongoError:
            log.exception("index creation failed; the API will stay unready (/health 503)")
            return


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except ConnectionFailure as exc:  # Mongo not reachable yet: keep starting, /health reports 503
        log.warning("MongoDB not ready at startup (%s); retrying index creation in background", exc)
        threading.Thread(target=_ensure_indexes_in_background, daemon=True).start()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 7. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
# Handlers are plain `def`: FastAPI runs them in its thread pool, and PyMongo's client is thread-safe.
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    if not INDEXES_READY.is_set():
        raise HTTPException(503, "indexes are not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_seconds()
    try:
        employees_col.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    skip, limit = page_params(page, page_size)
    total = employees_col.count_documents(q)
    cursor = employees_col.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(limit)
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees_col.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs_col.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()

    # Most recent record whose punch_in is at or before the punch-out (works across midnight).
    # Null punch_in (ABSENT/LEAVE) never matches $lte on a date.
    rec = logs_col.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if ts <= pin:
        raise unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if ts - pin > MAX_SHIFT:
        raise unprocessable("punched_at is more than 24 hours after punch_in", ("body", "punched_at"))

    day = parse_date(rec["date"])  # the shift's own date (start day for overnight shifts)
    derived = derive_fields(rec["status"], pin, ts, day, emp)
    # Conditional update: only the request that still sees punch_out == null wins.
    updated = logs_col.find_one_and_update(
        {"_id": rec["_id"], "punch_in": pin, "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def build_attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                           status: Optional[str]) -> dict:
    d_from, d_to = query_date("date_from", date_from), query_date("date_to", date_to)
    if d_from and d_to and d_from > d_to:
        raise unprocessable("date_from must be on or before date_to", ("query", "date_from"))
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    skip, limit = page_params(page, page_size)
    total = logs_col.count_documents(q)
    cursor = logs_col.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip(skip).limit(limit)
    return {"items": [serialize_record(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _path_date(value: str) -> date:
    try:
        return parse_date(value)
    except ValueError as exc:
        raise unprocessable(str(exc), ("path", "date"))


TRACKED_FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    day = _path_date(date)
    emp = get_employee_or_404(emp_code)
    rec = logs_col.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    status = body.status if body.status is not None else old["status"]

    if status in ABSENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"punch times cannot be set with status {status}")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pin is None:
            raise unprocessable(f"status {status} requires a punch_in", ("body", "punch_in"))
        if body.punch_in is not None and attendance_date(pin, emp) != day:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if pout is not None and not (pin < pout <= pin + MAX_SHIFT):
            raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derive_fields(status, pin, pout, day, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise unprocessable("the request changes nothing")

    entry = {"at": now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if the record still looks exactly like the
    # version we read (same punch fields, status and number of history entries). A concurrent
    # PATCH or punch-out makes this match nothing -> 409, never a lost history entry.
    guard = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
             "punch_out": rec.get("punch_out")}
    history = rec.get("history")
    if history is None:  # legacy record: no history field (or null)
        guard["history"] = None
        update = {"$set": {**new, "history": [entry]}}
    else:
        guard["history"] = {"$size": len(history)}
        update = {"$set": new, "$push": {"history": entry}}

    updated = logs_col.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics - pipeline builders are shared with /admin/explain
# --------------------------------------------------------------------------- #
def _weekday_expr(date_field: str) -> dict:
    """True when a 'YYYY-MM-DD' string field falls on Mon-Fri."""
    return {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}}, WEEKDAYS]}


def _present_value_expr(status: str, half_day: str) -> dict:
    """R5/R6: 1 for a present record, 0.5 for a present half day, 0 otherwise."""
    return {"$cond": [{"$in": [{"$ifNull": [status, ""]}, PRESENCE]},
                      {"$cond": [{"$eq": [{"$ifNull": [half_day, False]}, True]}, 0.5, 1]},
                      0]}


def _round4_decimal(expr: Any) -> dict:
    """Half-up to 4 decimals inside the database. $round is half-to-even, so: floor(x * 10^4 + 0.5) / 10^4,
    on Decimal128 so x * 10^4 is exact."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, 10000]},
                                              {"$toDecimal": "0.5"}]}}, 10000]}


def working_days(start: date, end: date) -> int:
    """R7: Mon-Fri between start and end inclusive (calendar arithmetic, no documents involved)."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    weekday_present = {"$cond": ["$weekday", _present_value_expr("$status", "$half_day"), 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$project": {
            "_id": 0,
            "status": 1,
            "half_day": 1,
            "weekday": _weekday_expr("$date"),
            "late": {"$ifNull": ["$late_minutes", 0]},
            "ot": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": weekday_present},  # Mon-Fri only (R7)
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late"},  # weekends included (R7)
            "total_overtime_minutes": {"$sum": "$ot"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[Optional[str], Query()] = None):
    start, end = query_month(month)
    emp = get_employee_or_404(emp_code)
    rows = list(logs_col.aggregate(employee_monthly_pipeline(emp_code, start, end)))
    r = rows[0] if rows else {}

    first_day = max(parse_date(start), parse_date(emp["joined_on"]))
    wd = working_days(first_day, parse_date(end))
    present = r.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": round_half_up(Decimal(str(present)) / wd * 100, 2) if wd else None,
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Runs on `employees` so that employees with zero logs still produce a row (R9)."""
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    present = {"$in": [{"$ifNull": ["$logs.status", ""]}, PRESENCE]}
    has_hours = {"$and": [present, {"$isNumber": "$logs.work_hours"}]}
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",  # uses the (emp_code, date) index on the logs side
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}},
            ],
            "as": "logs",
        }},
        # Keep employees without logs as one row with logs missing: they still count in headcount.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$eq": [_weekday_expr("$logs.date"), True]},
                _present_value_expr("$logs.status", "$logs.half_day"),
                0,
            ]}},
            # work_hours has 2 decimals: summing whole hundredths keeps the mean exact.
            "work_cents": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}, 0]}},
            "work_records": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                                      "$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "present_days": 1, "work_cents": 1, "work_records": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[Optional[str], Query()] = None, department: Optional[str] = None):
    start, end = query_month(month)
    items = []
    for r in employees_col.aggregate(department_summary_pipeline(start, end, department)):
        n = int(r["work_records"])
        avg = round_half_up(Decimal(int(round(float(r["work_cents"])))) / n / 100, 2) if n else None
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # Inner-join semantics: logs whose employee does not exist disappear at the $unwind.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank within the department
    pipeline += [
        # Standard competition ranking: equal totals share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, so ties at the cutoff are kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[Optional[str], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    start, end = query_month(month)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in logs_col.aggregate(late_leaderboard_pipeline(start, end, limit, department))
    ]
    return {"month": month, "items": items}


def trend_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[date, date]:
    d_from = query_date("from", require("from", date_from))
    d_to = query_date("to", require("to", date_to))
    if d_to < d_from:
        raise unprocessable("to must be on or after from", ("query", "to"))
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    return d_from, d_to


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    """One row per calendar day. Each employee emits a +1 'joined' event (clamped to `from`) and one
    event per log; events are grouped by day, $densify fills empty days, and window functions give
    the running headcount and the 7-row moving average."""
    f, t = d_from.isoformat(), d_to.isoformat()
    zero = {"hc": 0, "p": 0, "l": 0}
    rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        _round4_decimal({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
        None,
    ]}
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": f, "$lte": t}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            # Anchor rows on the first and last day so the range is complete even with no data.
            [{"date": f, **zero}, {"date": t, **zero}],
            {"$cond": [{"$lte": ["$joined_on", t]},
                       [{"date": {"$max": ["$joined_on", f]}, "hc": 1, "p": 0, "l": 0}],
                       []]},
            {"$map": {"input": "$logs", "as": "g", "in": {
                "date": "$$g.date",
                "hc": 0,
                "p": _present_value_expr("$$g.status", "$$g.half_day"),  # any day of the week
                "l": {"$cond": [{"$gt": [{"$ifNull": ["$$g.late_minutes", 0]}, 0]}, 1, 0]},
            }}},
        ]}}},
        {"$unwind": "$events"},
        {"$group": {"_id": "$events.date", "hc": {"$sum": "$events.hc"},
                    "p": {"$sum": "$events.p"}, "l": {"$sum": "$events.l"}}},
        {"$project": {"_id": 0, "hc": 1, "p": 1, "l": 1,
                      "day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": "full"}}},  # from..to: both anchors exist,
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "headcount": {"$sum": "$hc", "window": {"documents": ["unbounded", "current"]}},
        }}},
        {"$addFields": {
            "present_count": {"$ifNull": ["$p", 0]},
            "late_count": {"$ifNull": ["$l", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]},
        }},
        {"$addFields": {"rate": rate}},
        # $avg skips nulls; rows before `from` do not exist, so early windows are naturally shorter.
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$rate"},
            "moving_avg_7d": {"$toDouble": _round4_decimal("$ma")},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: Annotated[Optional[str], Query(alias="from")] = None,
    date_to: Annotated[Optional[str], Query(alias="to")] = None,
):
    d_from, d_to = trend_range(date_from, date_to)
    if employees_col.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": r["attendance_rate"],
            "moving_avg_7d": r["moving_avg_7d"],
        }
        for r in employees_col.aggregate(department_trend_pipeline(department, d_from, d_to))
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 9. Explain
# --------------------------------------------------------------------------- #
EXPLAIN_JSON = JSONOptions(json_mode=JSONMode.RELAXED, tz_aware=True, tzinfo=UTC)
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _run_explain(command: dict) -> dict:
    raw = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(raw, json_options=EXPLAIN_JSON))


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Builds the operation with the same builder the real endpoint uses, then explains it."""
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        skip, lim = page_params(page, page_size)
        command = {"find": logs_col.name, "filter": q, "projection": {"_id": 0},
                   "sort": dict(ATTENDANCE_SORT), "skip": skip, "limit": lim}
        collection = logs_col.name
    elif endpoint == "employee_monthly":
        start, end = query_month(month)
        pipeline = employee_monthly_pipeline(require("emp_code", emp_code), start, end)
        command, collection = {"aggregate": logs_col.name, "pipeline": pipeline, "cursor": {}}, logs_col.name
    elif endpoint == "department_summary":
        start, end = query_month(month)
        pipeline = department_summary_pipeline(start, end, department)
        command, collection = {"aggregate": employees_col.name, "pipeline": pipeline, "cursor": {}}, employees_col.name
    elif endpoint == "late_leaderboard":
        start, end = query_month(month)
        pipeline = late_leaderboard_pipeline(start, end, limit, department)
        command, collection = {"aggregate": logs_col.name, "pipeline": pipeline, "cursor": {}}, logs_col.name
    else:  # department_trend
        d_from, d_to = trend_range(from_, to)
        pipeline = department_trend_pipeline(require("department", department), d_from, d_to)
        command, collection = {"aggregate": employees_col.name, "pipeline": pipeline, "cursor": {}}, employees_col.name
    return {"endpoint": endpoint, "collection": collection, "explain": _run_explain(command)}
