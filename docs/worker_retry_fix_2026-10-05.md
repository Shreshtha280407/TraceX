# Worker retry correction — 2026-10-05

Implemented against HEAD `9c7bd59` without committing or pushing. The two
existing local worker/test edits were revised in place; unrelated files were
not changed.

## Behavior

- Retry classification uses PostgreSQL driver codes `40P01`, `40001`, and
  `55P03`, or SQLite primary `BUSY`/`LOCKED` codes, including extended codes.
  Both SQLAlchemy-wrapped exceptions and direct driver exceptions (such as
  PostgreSQL `COPY` errors) are supported. Legacy wrapped `pgcode` is supported.
- SQL text, SQL parameters, arbitrary exception wording and standalone
  `PendingRollbackError` do not establish a retryable database condition.
- At most one automatic retry occurs per claimed worker invocation, with a
  100 ms backoff. It retains the same job and lease instead of reclaiming from
  the global queue. The durable attempt number increases for the retry.
- After rollback, recovery locks and reloads the job using its saved ID. If
  another worker owns it or it is no longer active, this worker does not
  overwrite its state. This is not a replacement for continuous lease renewal
  throughout long-running stages.
- Interrupted running stage outcomes are recorded as failed. Successful prior
  outcomes, committed fragment receipts, immutable confidence pins and reviewer
  decisions remain available for receipt-based recovery.
- Retry reasons are recorded in `import.retrying` case events, not terminal
  job-error fields. Reclaimed legacy `DB_DEADLOCK_RETRY` details are preserved
  in an `import.retry_recovered` event before clearing the stale error fields.
  Existing completed jobs are not migrated or automatically reprocessed.
- A second retryable failure, or an unrelated first failure, is recorded as a
  durable terminal failure with the lease released. Source integrity failures
  never become retryable merely because their wording contains a keyword.

## Verification actually run

Audited discovery selected **63 tests**, all passed in **17.66 seconds**.
Ruff and `git diff --check` passed. One existing Starlette/httpx deprecation
warning remains; dependencies were not changed.

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 timeout 120 \
  .venv/bin/pytest -q -o faulthandler_timeout=30 \
  tests/unit/test_phase_six_crash_retry.py \
  tests/unit/test_phase_one.py tests/unit/test_phase_two.py \
  tests/unit/test_analysis_acceptance.py tests/unit/test_investigations.py \
  tests/unit/test_phase_six_offline_and_access.py
.venv/bin/ruff check workers/runner.py tests/unit/test_phase_six_crash_retry.py
git diff --check
```

The saved result is `var/worker-retry-fix-20261005-tests.xml`. Regression coverage
includes successful recovery through the real ingestion/grouping path, retry
exhaustion, preserved receipts/pins/individual reviews/stage outcomes, unrelated
queued imports, changed lease ownership, source tampering, crash recovery,
group-review independence, provisional activity, graph cursors, case-authorized
source replay/export and offline operation. Fixtures contain at most 60
transactions per import.

PostgreSQL error classes were injected into temporary SQLite-backed worker
tests; this is **not** a live concurrent PostgreSQL deadlock benchmark. Missing
Geo-IP and insufficient ML data remain explicitly degraded where applicable.
No large workload, model-quality evaluation or browser test was run. Scoring,
grouping and ranking procedures and frontend styling are unchanged.

## Owner handoff

Review the diff and this report before staging. No services were restarted;
restart the worker normally (or rebuild its appliance) when applying the code
to a running installation.

```bash
git diff -- workers/runner.py tests/unit/test_phase_six_crash_retry.py
git add workers/runner.py tests/unit/test_phase_six_crash_retry.py docs/worker_retry_fix_2026-10-05.md
git diff --cached
git commit -m "Fix bounded worker database retries and verify recovery"
git push
```

No datasets, evidence vaults, reports, Docker images or volumes were deleted.
