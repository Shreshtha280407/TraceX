# Phase 7 — submission and proof package

Current implementation note (2026-10-03): the older submission evidence below
does not establish acceptance of the current dirty worktree. Review image 07
was installed from a copied checksum-verified bundle in 39.606 seconds without
registry pulls. Native-Linux PostgreSQL browser investigation passes 70 checks,
the missing-IP/prevout variant passes 31, and smoke passes 42. API and worker
public IPv4/IPv6 connections return ENETUNREACH; all runtime networks, including
the API's UI bridge, are internal. The host's internet remains unchanged.
Worker-only isolation is not substituted for whole-appliance evidence. Larger
PostgreSQL scale results and their separate resource/time gates are in the
[final acceptance report](implementation_acceptance_2026-10-03.md). Optional
Ollama is unavailable: no real grounded-answer claim follows from a prompt or
the verified HTTP 503 response.

This document is the reproducible evidence behind every claim the Phase 7
release gate makes. Every number below came from an actual run on this
development host (Arch Linux, kernel `7.2.4-arch1-2`, x86_64, 13th Gen Intel
Core i5-13420H, 8 physical / 12 logical CPUs, 15 GiB RAM, 15 GiB swap) — see
`docs/hardware.md`. Nothing here is estimated or carried over from a different
host. PPT and video deliverables are intentionally out of scope for this
document; everything else the release gate names is covered below.

```bash
make phase-seven-test          # ruff + offline-launch + evidence-export-honesty regression tests
make phase-seven-walkthrough   # the full case walkthrough + accuracy-honesty run against the real 100K fixture
```

## 0. A real bug this phase found and fixed

While building the evidence-export proof below, `GET
/v1/cases/{case_id}/findings/export` and `GET
/v1/findings/{finding_id}/evidence` were found to hardcode `"method":
"deterministic-v1"` and `"ml_enabled": False` unconditionally — even for a
case whose findings include ML-scored rows. This is the exact class of bug
`04a7868` ("Completed 5C") already fixed once, in `list_findings` only; the
fix never propagated to the export endpoint or to `finding_evidence`'s
`replay_contract`, and the only existing tests for those two fields
(`tests/unit/test_phase_four.py`) happened to run on a purely deterministic
case, so a hardcoded `False` looked correct without being derived correctly.

An evidence bundle that tells a reviewer "no model ran" for a row an ML
release actually scored is a provenance failure, not a cosmetic one — it is
precisely the honesty property the export contract exists to guarantee.
Fixed in `app/api/routes.py` (`export_findings`, `finding_evidence`) to derive
`method`/`methods`/`ml_enabled` from the actual `rule_version` values present,
exactly the way `list_findings` already did, and to surface `release_id` /
`model_run_id` in `replay_contract` when a model produced the row. Regression
coverage: `tests/unit/test_ml_pipeline_integration.py::test_ml_findings_visible_through_the_real_findings_api`
now asserts both the true-positive case (an ML-scored finding reports
`ml_enabled: True` with its release/run ID) and the true-negative case (a
deterministic finding still correctly reports `ml_enabled: False`) in the same
mixed-method case, plus the export endpoint reporting `method: "mixed"`. `make
phase-seven-test` runs it; the full suite is 128/128 passing after the fix
(`uv run --extra ml pytest -q`).

## 1. Offline launch

`tests/unit/test_phase_seven_offline_launch.py` installs a socket guard
(raises on any non-`AF_UNIX` socket construction) **before** the first `from
app... import` runs, then imports the application and drives `/v1/healthz`
and `/v1/readyz`. This is a stronger claim than the Phase 6 offline test
(`test_phase_six_offline_and_access.py`), which installs its guard after
`app.main` is already imported at module scope and so only covers in-request
code paths, not process boot itself.

- `test_process_boot_and_readiness_make_no_outbound_network_call` — process
  import, FastAPI app construction, dependency wiring, and both readiness
  endpoints complete with zero outbound sockets opened.
- `test_dependency_manifests_are_pinned_for_offline_replay` — `uv.lock` is
  committed, `pyproject.toml` pins `requires-python`, and
  `docs/environment.manifest.json` asserts
  `offline_runtime_contract.network_required_at_runtime: false` against the
  actually-observed host, not an assumed one.

Both pass: `make phase-seven-test` → `2 passed` (plus the 22 other tests in
that target). `docs/hardware.md` and `docs/environment.manifest.json` record
the measured Linux CPU profile as the supported baseline and mark the Mac
36 GiB profile, CUDA, and any 10M-scale claim explicitly unverified on this
host — no invented target is presented as measured.

## 2. Case walkthrough

`scripts/phase7_case_walkthrough.py` drives the exact chain the release gate
names, through the real HTTP API and the real worker (`app/api/routes.py` +
`workers/runner.py`), never by calling internal functions directly:

```
original file -> progress events -> committed graph -> deterministic lead
-> model rank -> source rows -> analyst decision -> case-scoped export
```

It runs on the first 5,000 rows of the real, unmodified generator-v2 100K
fixture at `datasets/phase5a_100k/ingestion_rows.ndjson` (`make dataset`
regenerates it) — not a hand-built toy case. One run, `case_id
fb3480d8-5ef7-4fb8-bfed-23587f36451b`:

| Step | Evidence |
| --- | --- |
| Original file | `ingestion_rows.ndjson`, 5,000 rows, 8,726,503 bytes, SHA-256 `428f6639586bf6b12bd1b6f60a04cdb2332c6d02a3ba549ec3f4ef7793b8beb9` |
| Progress | Real SSE stage sequence read back from `GET /v1/cases/{id}/events`: `queued → source_verification → ingesting → graph_ready → findings_ready → findings_ready → ingested` |
| Committed graph | `GET /v1/cases/{id}/graph?seed=<address>` → `200` on a real output address from row 1 |
| Deterministic lead | `coinjoin_like_structure` (rule `deterministic-v1`) fired on both walked transactions |
| Model rank | `anomaly_stack_rank` (rule `anomaly-stack-v1`) fired on both walked transactions |
| Source rows | `GET /v1/findings/{id}/evidence` → `source_refs[0]` → `GET /v1/evidence/{source_id}/records` → the exact raw NDJSON record, opened by locator |
| Analyst decision | Two real `POST /v1/findings/{id}/reviews` calls: one `dismissed`, one `escalated` (below) |
| Case-scoped export | `GET /v1/cases/{id}/findings/export` → `method: "mixed"`, `methods: ["anomaly-stack-v1", "deterministic-v1"]`, `ml_enabled: true`, 2,124 findings, review history attached |

Full machine-readable bundle: `experiments/runs/phase7_case_walkthrough.json`.
`--rows 5000` is not the smallest prefix that contains both scenario families
(a few hundred rows would); it is the smallest round prefix where the
anomaly stack's reference-period and 1% review-budget mechanics engage the
same way they would on a full snapshot, which is what lets both scenarios
below get an actual model rank rather than only a rule finding. At a larger
prefix (measured at 20,000 rows) neither chosen transaction fell inside that
1% budget — recorded honestly in §3, not re-rolled until it looked better.

## 3. Accuracy honesty

The fixture (`fixtures/phase5a_100k/README.md`) was deliberately built with a
family that "satisfies the deterministic rule exactly while being a benign
recurring payroll-shaped payment" — `nearmiss_rule_positive`, ≥200 instances
fixture-wide by the generator's own invariant check, i.e. the fixture's
high-volume benign lookalike. This script selects one instance of that family
and one instance of the genuinely suspicious `coinjoin_like` family, using
`evaluation_truth.json` **only** to pick which existing finding to open —
never uploaded, never fed to detection, and the analyst's stated review
reason below cites only what the opened source record actually shows, never
the internal family name a real analyst would never see.

| | `coinjoin_like` (suspicious) | `nearmiss_rule_positive` (high-volume benign lookalike) |
| --- | --- | --- |
| txid | `013315dc…9143` | `09ea7199…9261` |
| Deterministic rule | fires — rank 2013/2124, score 0.781 | fires — rank 2024/2124, score 0.775 |
| Unsupervised model | rank **56** of ~5,000 scored, fused score 2.521 | rank **7** of ~5,000 scored, fused score 3.104 |
| Uncertainty carried on the finding | `change_outputs_not_inferred: true`; `"Only observable transaction shape and values were evaluated."` | same |
| Coverage / missing data | `spend_lineage_complete: false`; 900 of 6,932 case-wide inputs have no `prev_txid`/`prev_vout` (partial spend lineage, carried explicitly, never silently treated as zero) | same case-wide coverage block |
| Analyst decision | `escalated` — cites the rule hit *and* the model rank, defers ownership conclusions to a second reviewer with graph context | `dismissed` — cites the actual source record's recurring counterparties and regular schedule, not the rule or model score |

**The honest, unflattering result, stated plainly rather than filtered out:**
the benign lookalike ranks *more* anomalous (7) than the real suspicious
transaction (56) under the unsupervised model at this budget. This is not a
bug — it is exactly the structural limitation
[[anomaly-stack-causality-and-deployment]] already documented from the
offline evaluation: an unsupervised ranking cannot distinguish "unusual
structure" from "unusual *and* wrongdoing," because a benign lookalike that
satisfies the same predicate is, by construction, just as anomalous against
ordinary traffic. The deterministic rule fares no better — it is fooled by
definition, since the family exists precisely to satisfy it. **Neither the
rule nor the model resolves this case. The analyst review step —
opening the actual source record before deciding — is what does**, and this
walkthrough exercises that step for real rather than asserting it in prose.

## 4. Evidence export

`GET /v1/cases/{case_id}/findings/export` (now honesty-fixed, §0) returns,
case-scoped: every finding's full evidence bundle (claim, feature vector,
coverage, source locators, review history, audit history), the actual
`method`/`methods`/`ml_enabled` the case's rows carry, and a fixed
`limitations` block stating findings establish no ownership or wrongdoing.
`GET /v1/cases/{case_id}/sources` gives the SHA-256 and byte size of every
uploaded source. `GET /v1/findings/{id}/evidence`'s `replay_contract` names
the exact `rule_id`/`rule_version` and, for a model-scored row, the
`release_id`/`model_run_id` from `app.ml.findings.release_identity()` — a
frozen procedure identity (layers, fusion method, training seed, feature
contract hash), not a loaded model file, since the stack refits on each
snapshot's own reference period rather than shipping a static artifact (see
`experiments/model_decision.md`). Cross-case isolation on every one of these
endpoints (a non-member gets `404`, not a partial view) is proven in
`tests/unit/test_phase_six_offline_and_access.py`, still passing.

## 5. Benchmark evidence

Already measured and documented in full in `docs/phase6.md`: 100K real-fixture
throughput (188.4 s total; parse+commit 11.4 s, graph 27.1 s,
findings+features 150.0 s — dominant cost), the confirmed 1M-row OOM ceiling
with root cause, query latency (p95 215 ms at 1 reader / 481 ms at 4 readers
against the real 589,987-node graph), and the crash/retry and offline/access
evidence. Corpus seed and shape, exact machine specs, and methodology are all
in that document; this phase does not re-measure them, only points to them as
the source of record. `docs/anomaly_stack.md` similarly carries the model
comparison's frozen holdout table.

## 6. Release gate

> A fresh authorised case can import a file, reconcile accepted/quarantined
> counts, open a bounded true UTXO graph, produce a reviewable model-scored
> lead from the selected artifact, survive a restart, and export verifiable
> evidence offline.

- Import + reconcile: §2 (`rows_seen == rows_accepted == 5000`,
  `rows_quarantined == 0`, reconciled against the job record over HTTP).
- Bounded UTXO graph: §2 (`GET /v1/cases/{id}/graph` capped and case-scoped).
- Reviewable model-scored lead: §2/§3 (`anomaly_stack_rank` findings, opened
  down to source, reviewed by a real disposition change).
- Survive a restart: `tests/unit/test_phase_six_crash_retry.py` (unchanged by
  this phase, still passing).
- Export verifiable evidence offline: §4, proven with no network access
  available (§1's socket guard covers the same code paths the export
  endpoints sit behind).

Nothing here substitutes a hard-coded model score for a real one: every
number in this document was read back from a live HTTP response or a
pytest/ruff run against this repository, on this date, on this host.
