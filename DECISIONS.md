# DECISIONS.md

1. **Indexes.** Employees have unique `emp_code` (identity and duplicate-safe creates), `(department, emp_code)` (department filter with the emp_code sort), and `joined_on` (headcount for the summary). Attendance has unique `(emp_code, date)` (one record per day, per-employee date ranges, the `$lookup` joins and PATCH), `(date desc, emp_code)` (matches the list sort, so no in-memory sort), `(status, date desc, emp_code)` (status filters with the same sort), and `(emp_code, punch_in desc)` (finds the latest open record for punch-out). I considered an index on `late_minutes` for the leaderboard but rejected it: the date filter already cuts the scan to one month, and it would slow every write.

2. **Simultaneous punch-ins.** Both requests calculate the attendance date and attempt `insert_one`. With the unique index in place, one insert succeeds with 201; the other raises `DuplicateKeyError`, returned as 409. An existence check is unnecessary and would not prevent races, because both requests could pass the check before either inserts.

3. **Ties at the cutoff.** MongoDB's `$rank` produces competition ranks such as 1, 2, 2, 4. Filtering `rank <= limit` afterwards preserves every tie. For totals 120, 90, 90, limit 2 returns three employees, ordered by total descending, then employee code.

4. **Employees without logs.** The department summary starts from employees who joined by month end, not from logs. The `$lookup` and `$unwind` with `preserveNullAndEmptyArrays` keep employees with no attendance. Distinct employee codes give the headcount; missing logs add nothing to the attendance totals.

5. **One thing I would change for 100x the data.** Add a daily per-department rollup collection, updated on every punch and correction. Trend and summary would then read about 30–92 small documents instead of joining millions of logs. The cost is keeping it consistent, so a nightly job would rebuild it from the raw logs, which stay the source of truth.
