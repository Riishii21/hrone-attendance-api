# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in .env; real env vars win
uvicorn app.main:app --port 8000
```

Requires MongoDB 6.0+ (uses `$setWindowFields`, `$densify` and `$lookup` with both `localField` and a sub-pipeline).
Indexes are created idempotently at startup. If MongoDB is not reachable yet, the app still starts and retries in the
background; `/health` returns 200 only once MongoDB answers **and** the indexes exist. If a unique index cannot be built
(for example duplicate data), the app does not fall back to a non-unique index: startup fails, or `/health` stays 503.

## Tests

```bash
python -m pytest tests/                        # rules R1-R5 + API/concurrency tests (in-memory mongomock, no DB needed)

python tools/seed_test_data.py                  # DROPS both collections, seeds ~100k edge-case records
uvicorn app.main:app --port 8000 &
python tools/verify.py                          # analytics vs an independent reference implementation,
                                                # explain plans (IXSCAN / no COLLSCAN), latency and race tests
```

## Interpretations worth knowing

- **Punch-out** follows the contract's selection rule literally: it takes the latest record whose
  `punch_in <= punched_at`. If there is none the answer is 404, if that record is already closed it is 409, and
  equal timestamps or more than 24 hours give 422.
- **Trend moving average** is the mean of the reported (4-decimal) `attendance_rate` values in the window, rounded
  half-up. "Exceeds 92 days" means more than 92 calendar days inclusive.
- **Server-generated instants** (`created_at`, `history[].at`) are truncated to whole seconds, like punch times (R1).
- **Explicit `null`** for `punched_at`, `status`, `punch_in` or `punch_out` is a 422 (the schemas are non-nullable).
  To use the defaults, omit the field.
- **Concurrent PATCHes** use optimistic concurrency: the update only applies if status, punch times and history length
  are unchanged since the read. The loser gets 409, so no history entry is ever lost.
- **Rounding** (R4, R8) is done with `Decimal` half-up in Python, and with Decimal128 `floor(x*10^4+0.5)` in the trend
  pipeline (`$round` is half-to-even). `avg_work_hours` sums whole hundredths so the mean is exact.
- **Working days** for the monthly summary is calendar arithmetic in Python (no documents involved). Every
  document-level computation runs in an aggregation pipeline.
