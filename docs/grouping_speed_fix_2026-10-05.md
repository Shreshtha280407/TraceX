# Review-group stage speed fix — 2026-10-05

Base HEAD: `4d6839a` (`Fixed counting reviews`). The checkout was clean before
editing; no applicable AGENTS.md was found. No commit, push or publication was
performed. No mock count of 97 or timed/fabricated completion was implemented.

## Cause and correction

The owner's CSV had already ingested 100,000 accepted transactions, built its
graph, scored findings and completed analytics. Grouping was still doing
several PostgreSQL round trips for each finding, flushing each member, and
repeatedly querying the immutable graph. Its stage had no useful progress and
did not renew the worker lease. The original upload eventually completed with
the old executor: **1,090.51 seconds for grouping**. That is historical diagnosis,
not a new optimized 100K benchmark.

The active ingestion and group-only recovery paths now use
`app/engine/grouping_materializer.py`:

- Stream 256 findings per page; prefetch normalized participation and prior
  generation links once per page.
- Query verified canonical graph edges once per page, then use the unchanged
  deterministic observation extractor. Missing edges remain missing evidence.
- Match fixed representative anchors through an indexed, disk-backed private
  SQLite work file with an 8 MiB page cache, not per-finding PostgreSQL queries
  or a global Python dictionary. Full 256-member groups leave the private
  eligible-anchor index, but retain their actual anchors and membership.
- Publish normalized groups, members, anchors, subjects and replacement links
  using bounded bulk inserts (PostgreSQL COPY). Publication remains atomic.
  Failure exposes no partially published generation and leaves previous runs,
  individual/group decisions, source references and confidence pins intact.
- Admit scratch against available disk (96 KiB/finding estimate plus the
  existing reserve). Only this new temporary work directory is automatically
  cleaned; existing datasets, evidence, reports and Docker resources are retained.
- Renew the actual PostgreSQL job lease and worker heartbeat on a separate
  operational connection while exposing prepared/total finding counts and
  the build/publication phase. Loss of the owning attempt fails closed.
  SQLite's single-writer limitation is explicit: its progress is indeterminate.
- The existing UI consumes real job progress. 100% still means actual
  completion; prepared counts do not become visible review tasks until publish.

The frozen reference file `app/engine/investigations.py` is unchanged. Rule
boundaries, fixed-anchor connectivity, representative selection, IDs, member
order/caps, replacement relationships, family ranking and queue capacity are
unchanged. Its procedure SHA256 remains:

`b429e8ee965ab3fe7ac78edd719cd565ee06c31fe38da4da9890e2e0780a0769`

Transport/storage execution is separately identified as `disk-indexed-grouping-v1`,
with its source hash recorded in stage details. This is not a scorer, calibration
or grouping-proposition change. Active scoring remains `anomaly-stack-v2`.
No new precision/accuracy improvement is claimed.

## Verification actually run

- **84 audited small backend tests passed in 27.25 seconds**, with one existing
  Starlette/httpx deprecation warning. They cover exact reference parity,
  256-member/page/time boundaries, repeated observations and hubs, unchanged
  reviews/replacements, atomic failure, idempotency, authorization, source replay,
  capacities 0/1/100, backlog, crash/retry and lease renewal/failure.
  Final XML: `var/grouping-speed-20261005-final-tests.xml`.
- On **607 finding records associated with a three-transaction fixture**, final
  reference grouping took **0.980 s**, disk-indexed grouping **0.066 s**.
  IDs, normalized memberships, anchors, subjects, counts and replacements were
  equal. A separate 307-finding test requires fewer than 20 SQL SELECTs, rather
  than several SELECTs per finding. This is small-fixture evidence, not large-scale
  parity or a universal speed guarantee.
- Fresh **1,000-canonical-transaction CSV** through the real local upload/API,
  PostgreSQL and ordinary worker: **2.180 s for grouping**, **7.770 s from upload
  initiation to authenticated final group/member retrieval**. All 1,000 source
  records were accepted; zero quarantined. Actual counts at retrieval were
  1,000 underlying findings / 1,000 groups / 100 queued / 900 backlog. These are
  fixture counts, not a claim that every finding should be merged. The browser
  then triaged one isolated test group, without changing its individual finding.
  Report: `var/grouping-speed-live-20261005T075130-c5c7199e/result.json`.
- **7 live API/worker assertions + 4 real browser checks passed**, including
  backend/UI queue agreement, group/member navigation, immutable CSV source
  replay, unauthenticated rejection, persisted group decision and unchanged
  individual verdict. Browser report: `browser.json` in that live run directory.
- **13 existing mocked-count browser regressions**, two Node test files,
  TypeScript/production build, targeted Ruff and `git diff --check` passed.
  No unrelated frontend styling was changed; production assets remain unchanged.

The small CSV intentionally contains no IP metadata. Its overall analysis is
**degraded**, with network coverage explicitly incomplete; this was not an
all-coverage acceptance benchmark or labelled quality study. The final successful
run's grouping time is 2.180 s; an earlier run measured 1.075 s but its browser
harness failed. Two earlier diagnostic runs are retained:
`var/grouping-speed-live-20261005T074334-6ed9b3d8/` and
`var/grouping-speed-live-20261005T074433-0e802917/` (incorrect test heading/history
expectations, corrected before the successful run). They are not reported as passes.

The verified idle local API and worker were reloaded with their exact original
environment, session settings, PostgreSQL and vault. No active upload was stopped;
Docker services/images/volumes were untouched. Reload diagnostics:
`var/local-service-reloads/20261005T073916330981Z/`.

## Reproduce without a large workload

With the existing local API/worker running, the pinned Python/npm dependencies
installed and Chrome available:

```bash
.venv/bin/python scripts/verify_grouping_speed.py --help
.venv/bin/python scripts/verify_grouping_speed.py --rows 1000 --browser
```

Implemented options: `--api-base`, `--frontend-base`, `--rows` (3..10,000),
`--deadline-seconds` (10..600), `--browser`, `--output` (must be a new directory).
The command creates only a new synthetic test account/case, uses a finite
diagnostic deadline, retains its evidence and saves unique results on failure.
Tokens/passwords are never written to reports. Defaults are the owner's local
API on port 8001 and frontend on 5173. No cloud or LLM is required.

Refresh the normal app; subsequent uploads use the optimized executor. The
original completed upload need not be reuploaded. Pending review counts remain
`min(actual unresolved groups, capacity)`; default capacity is 100, not 97.

A new optimized 100K timing check, 1M/3M benchmark and labelled quality studies
were **NOT RUN** in this fix. Seconds-only performance on every future upload is
not established by the small tests. No existing data or report was deleted.
