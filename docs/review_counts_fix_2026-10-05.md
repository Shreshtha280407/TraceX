# Review workload counter correction — 2026-10-05

Base HEAD: `0d5e68e` (inspected, not assumed). The existing unrelated edit to
`tests/unit/test_phase_six_crash_retry.py` was preserved. No AGENTS.md applies.

## Cause and fix

The dashboard still showed raw **Open findings**, although the primary human
review unit is a group. The overview's per-case count also used its shortened
preview, grouped by display name rather than case ID. Those counters were not
the actual prioritized group queue.

The development frontend on localhost:5173 targets localhost:8001. That API and
its worker were still running older code: the grouped-review API route was
absent, and previously completed imports had no published groups. Restarting
only the frontend could not fix that.

- Dashboard primary workload: real `queued_groups`, default capacity 100 per
  case; real additional unresolved groups are shown separately.
- Overview: one backend summary per case, keyed by case ID; preview lengths and
  duplicate case names never determine totals. Across-case totals can exceed
  100 because capacity is **per case**, not a global cap.
- Findings feed: the grouped queue is primary. Raw counts remain in the
  underlying-observations section, explicitly not independent human tasks.
- Counts refresh after group decisions/import completion/failure and tab focus;
  historical SSE replay is debounced. No whole-case fetch or background study.
- API reports grouped/ungrouped coverage via an indexed membership existence
  query. Total/covered findings share one SQL aggregate to avoid inconsistent
  subtraction across concurrent commits. Inactive/historical/building group
  generations cannot falsely satisfy coverage or double-count replacements.
- Missing routes and incomplete grouping are explicit, not replaced by raw
  alert counts or silently reported as a completed zero workload.

No finding is suppressed or automatically marked reviewed. Individual open
statuses can remain after a group proposition decision; their histories are
intentionally independent. Scoring, grouping rules/hashes and queue priority
procedures did not change. No precision or alert-reduction claim is made.

## Running setup and existing imports

The exact local API and matching host worker were reloaded after verifying no
queued/running/checkpointed imports, preserving their original process
environment, PostgreSQL database, vault and session-signing configuration.
All Docker services, volumes, images, source data and completed jobs were left
intact. No commit/push or LLM integration.

Refresh the browser. An older import without groups now shows **Grouping
incomplete**. For that existing case, Evidence Intake provides **Retry analysis
from committed evidence**; the case-authorized retry uses the updated worker.
The existing 100K uploads were not automatically rerun or reimported during this
counter fix. New uploads use the current required grouping stage.

## Actual checks

- **20 backend tests passed**, 11.27 s, including coverage of unpublished/inactive
  generations, capacity 0/1/100, authorization, preserved reviews and failure
  recovery. XML: `var/grouped-benchmark-20261005-counts-final-tests.xml`.
- Node count/graph test files passed. The count fixture uses **23,000 as mocked
  metadata**, not 23,000 allocated/imported findings.
- **9 mocked browser checks passed**: queue versus observations, actual backlog,
  same-named cases, event refresh, 0/1 capacity, missing grouping/route failures.
  `var/browser-runs/review-counts-1791176657186/result.json`.
- **67 live browser checks passed** on a separate **128-TX** PostgreSQL/API/worker
  case, including complete grouping coverage, independent group review, source
  replay, graph bounds and cross-case denial.
  `var/browser-runs/review-counts-live-20261005-02/result.json`.
- Final authenticated HTTP query with the combined SQL aggregate: **42 underlying
  findings / 42 groups / 41 unresolved / 41 queued / 0 backlog**, after one group
  was triaged. Coverage: 42 grouped / 0 ungrouped.
  `var/grouped-review-counts-final-http-20261005.json`.
- TypeScript/build, targeted Ruff and `git diff --check` passed. Frontend lint
  exits successfully with existing React warnings; not warning-free certification.

Earlier test-only CSS-text/wait assertions failed, were corrected and rerun;
their diagnostic reports were retained. No 100K, 1M or 3M benchmark was run.
Separate-origin development browser checks are not native-Linux appliance
network-isolation evidence.

Reproduction (running local dev services and Chrome required):

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 timeout 120 .venv/bin/pytest -q tests/unit/test_investigations.py tests/unit/test_analysis_acceptance.py
cd frontend
node --test e2e/investigation-counts.test.mjs e2e/graph-window.test.mjs
node e2e/review-counts.mjs
npm run build
```

For live small acceptance, `e2e/acceptance.mjs` additionally supports
`TRACEX_E2E_API_BASE=http://localhost:8001` with
`TRACEX_E2E_BASE=http://localhost:5173`, the existing 128-TX source and a new unique
output directory. Its single-origin appliance default remains unchanged.
