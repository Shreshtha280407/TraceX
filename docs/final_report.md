# Historical report — superseded deployment instructions

Measurements in this document describe the earlier release; they were not rerun after removal of the LLM. Current release contains no LLM. Use integrated_implementation_2026-10-04.md for current status. Legacy setup instructions are not current prerequisites.

# TraceX — final project report (SIH 26146)

**Scope of this document:** a complete, reproducible account of everything
built and measured across Phases 0–7 of the backend/data-pipeline
implementation guide. It does not include the presentation deck or the demo
video called for by the same guide's Phase 7 deliverable list — those are
separate, out-of-repository artifacts and are intentionally excluded here.
Every number and claim below is either read back from a committed test run,
a committed benchmark JSON file, or a doc already checked into this repo; none
is estimated for the purpose of this report.

## 1. What TraceX is

An offline, case-scoped Bitcoin-intelligence backend: upload bulk
CSV/NDJSON/XML/JSON transaction data into an isolated case, get back a
receipt-approved UTXO graph, a set of deterministic reviewable findings, and
(since Phase 5C) an unsupervised anomaly ranking layered on top — all without
any network call at runtime, and with every claim traceable back to an exact
raw source record. Frontend implementation was explicitly out of scope for
the phased guide this project followed; this report covers backend and data
pipeline only.

The project followed a strict one-way sequence — **0 → 1 → 2 → 3 → 4 → 5 → 6
→ 7** — where each phase's exit gate had to pass before the next could start,
and Phase 5 (model selection) was walled off from Phases 0–4 so the
deterministic path always works with ML disabled. That sequencing held for
the whole project: nothing in Phases 6–7 required reopening 0–4's contracts.

## 2. Phase-by-phase summary

| Phase | Built | Proven | Doc |
| --- | --- | --- | --- |
| 0 — Contract | Frozen `schemas/v1/` JSON Schemas, a 100K seed-controlled synthetic fixture, hardware/offline preflight | A teammate can explain every column, regenerate the fixture from its seed, and identify which incoming data are observations vs. proven ownership | `docs/requirements.md`, `docs/hardware.md` |
| 1 — Foundation | Case-scoped FastAPI, PostgreSQL control plane, durable job leasing, immutable evidence vault with SHA-256 | Upload → job ID immediately; job survives a worker restart; Case B cannot see Case A | `docs/phase1.md` |
| 2 — Ingestion | Safe CSV/NDJSON/XML parsing (external entities/DTDs disabled), batched normalize + quarantine, atomic Parquet fragment commit | 100K clean records + a malformed file: accepted/quarantined counts reconcile; kill-mid-batch + restart produces no duplicate/phantom snapshot | `docs/phase2.md` |
| 3 — UTXO graph | Canonical `(case_id, network, txid)` identity, `CREATES_OUTPUT`/`SPENT_BY`/`LOCKED_TO` edges, bounded neighbourhood API | Known 2-in/2-out fixture reproduces exactly; missing outpoints degrade to partial coverage, never an invented edge; high-degree queries respect caps | `docs/phase3.md` |
| 4 — Deterministic findings | Frozen `deterministic-v1` address-window feature contract, peeling-chain + CoinJoin-like-structure + concentrated-collection/emerging-hub/rapid-redistribution rules, review/audit trail | End-to-end journey works with ML fully disabled; a benign lookalike is shown with its counterevidence and can be dismissed by a reviewer | `docs/phase4.md` |
| 5A/5B — ML laboratory | Generator v2 100K fixture (fixes the v1 address-collision bug that manufactured ~99.99% of the rule baseline's hits — see below), frozen train/validation/holdout splits, a six-layer anomaly stack, a 63-combination ablation harness | Layer/fusion choice made from measured validation numbers, evaluated on a final holdout exactly once | `docs/anomaly_stack.md`, `experiments/protocol.md`, `experiments/model_decision.md` |
| 5C — Integration | `app/ml/findings.py` wired into the real ingestion path; findings reach the existing `/findings` API automatically; safe fallback when the `ml` extra is absent | Unseen fixture, ML on: reproducible ranking after restart; ML off: deterministic path still works | `docs/anomaly_stack.md` |
| 6 — Performance/recovery/security | Staged real-pipeline benchmark, 200+ bounded-query latency harness, crash/retry proof, offline/cross-case-isolation proof | 100K real-fixture ingest measured end to end; 1M-row ceiling confirmed and root-caused; p95 query latency measured, not assumed | `docs/phase6.md` |
| 7 — Submission proof package | Offline-boot proof, a real end-to-end case walkthrough, an accuracy-honesty demonstration, case-scoped evidence export, a real bug found and fixed | Release gate exercised over live HTTP responses on this host, on this date | `docs/phase7.md` |

## 3. The three honest, non-obvious research findings

These came out of building and measuring the anomaly stack, and each
contradicts what the design would have predicted before measuring —
worth stating plainly rather than smoothing over:

1. **The motif task is near-circular.** Its labels are the generator's own
   shape families, and the rule's predicate is almost the same predicate. Only
   the *discrimination* task — where the negatives are constructed to satisfy
   the rule exactly (`nearmiss_rule_positive`) — measures what a model
   genuinely adds beyond the threshold. On the deployable (leakage-free,
   causal) configuration, **the rule wins the discrimination task outright;
   no unsupervised combination beats it.** Surge detection and triage
   ordering are where the stack adds real, measured value (surge AP 0.5927
   vs. rule 0.2388, a 2.48× margin on the final holdout); deciding which
   equal-output transaction is a mixer is not a claim this system makes.
2. **Global scoring beats stratified scoring here**, the opposite of the
   prediction made before measuring — because the detection target *is* a
   shape family, so its members are the majority of their own cluster, which
   is exactly the setup stratified comparison handles worse, not better.
3. **Equal-weight fusion degrades with more layers.** Five-layer Stouffer
   combinations score below two-layer ones on every task measured. The
   deployed configuration (`A_global + D_burst`) was chosen because it
   measured best, not because more signals sounded more thorough.

A fourth finding, upstream of all three: the original 100K fixture (v1) had a
generator stride bug (`index * 32 + vout` colliding against 100
bootstrap outputs per transaction) that manufactured **~99.99%** of the
deterministic rule baseline's hits from bootstrap artifacts, not designed
scenarios — meaning every early rule-vs-model comparison was, until this was
found and fixed, a comparison against a bug. Generator v2
(`fixtures/phase5a_100k/`) keys addresses on the full `(index, vout)` pair and
is the fixture every number in this report and in `docs/phase6.md` and
`docs/phase7.md` is measured against.

A fifth, found while making the stack causal rather than just accurate: three
real leakage sources (`spent_output_share`, `distinct_next_tx`,
`chain_depth_forward`, whole-dataset recipient-reuse/country-rarity, and
containing-bucket endpoint/ASN co-occurrence) were removed and replaced with
expanding-window / trailing-window equivalents, proven bit-identical up to
any truncation point by `app.ml.facts.truncate_facts` property tests.
Removing the leakage *improved* the deployable numbers by +0.10 to +0.19 AP —
the leaky features were adding noise, not the inflation a leakage bug usually
produces, which is itself worth recording rather than assuming the obvious
direction.

## 4. Measured performance (Phase 6, full detail in `docs/phase6.md`)

Measured on this development host — Arch Linux, kernel `7.2.4-arch1-2`,
x86_64, 13th Gen Intel Core i5-13420H, 8 physical / 12 logical CPUs, 15 GiB
RAM, 15 GiB swap — **not** the 36 GiB Mac profile the original architecture
proposal describes as a target; that profile has never been verified on
actual hardware and is reported as unverified rather than assumed met.

| Metric | Real 100K fixture (generator v2) |
| --- | --- |
| Upload (HTTP, hash-while-write) | 0.391 s |
| Source verification + parse + commit | 11.36 s |
| Graph build | 27.05 s |
| Deterministic findings + features | 150.03 s (dominant cost) |
| **Ingest total** | **188.45 s** |
| Graph nodes / edges | 589,987 / 970,260 |
| Query latency, bounded neighbourhood | p95 215 ms @ 1 reader, 481 ms @ 4 readers (target ≤500 ms — met / borderline) |
| 1,000,000-row synthetic ingest | **confirmed OOM-killed** — root cause: `app/engine/graph/builder.py` holds every node/edge as Python dict objects, plus a second full copy just before Arrow conversion, for the whole snapshot at once; needs a streaming rewrite before attempting again |

A real Phase 4 bug was also found and fixed while building this benchmark:
two independent detector signals sharing an origin address and window
produced a duplicate `(entity_ref, window, rule_id)` key and crashed
ingestion with a SQLite `IntegrityError`; fixed with a 5-line
dedup-by-key-keep-highest-score step, with a regression test.

## 5. Phase 7 submission proof package

Full detail and every underlying number: `docs/phase7.md`. Summary:

- **A real bug found and fixed.** `GET /v1/cases/{id}/findings/export` and
  `GET /v1/findings/{id}/evidence`'s `replay_contract` hardcoded `"ml_enabled":
  false` unconditionally — the same class of bug already fixed once in
  `list_findings` (commit `04a7868`), never propagated to the two export
  surfaces. An evidence bundle that tells a reviewer "no model ran" for a row
  an ML release actually scored is a provenance failure. Fixed, with a
  regression test proving both the true-positive and true-negative case in one
  mixed-method case. Full suite: **130/130 passing** after the fix.
- **Offline launch.** A test installs a socket guard *before* importing
  anything under `app`, then boots the FastAPI app and drives `/healthz` and
  `/readyz` — a stronger claim than steady-state offline coverage, since it
  covers process import and construction, not just in-request code paths.
  Pinned dependency manifests (`uv.lock`, `pyproject.toml`,
  `docs/environment.manifest.json`) are asserted present and internally
  consistent.
- **Case walkthrough**, over the real HTTP API and worker, on a real slice of
  the generator-v2 100K fixture: original file → progress events read back
  from the real SSE stream → committed graph → deterministic lead → model
  rank → source record opened by locator → two real analyst review decisions
  → case-scoped export. Full bundle: `experiments/runs/phase7_case_walkthrough.json`.
- **Accuracy honesty**, using the fixture's own paired scenarios: a genuine
  `coinjoin_like` structure and its designed high-volume benign lookalike,
  `nearmiss_rule_positive` (satisfies the deterministic rule exactly; ≥200
  instances fixture-wide by construction). The unflattering, unfiltered
  result: at the measured budget, the benign lookalike ranked **more**
  anomalous (rank 7) than the real suspicious transaction (rank 56) under the
  unsupervised model — a live demonstration of the structural limitation
  already documented in §3, not a hidden failure. The rule fares no better —
  it is fooled by construction. The analyst review step, exercised for real in
  this walkthrough, is what actually resolves it.
- **Evidence export**, now honesty-fixed: case-scoped source hashes, row
  locators, snapshot IDs, rule/model/config versions (`release_id`,
  `model_run_id` from `app.ml.findings.release_identity()`), review history,
  and a fixed limitations statement that findings establish no ownership or
  wrongdoing.
- **Benchmark evidence**: already complete from Phase 6 (§4); this phase adds
  no new performance claims, only points to the existing measured table.

## 6. Known limitations and open items

**Two real auth bugs found and fixed** ahead of the first non-local
deployment: (1) the dev-only `X-TraceX-Actor` header was honored unconditionally
— anyone could name any actor and skip login entirely; it's now disabled
unless `TRACEX_ALLOW_DEV_ACTOR_HEADER=1` is set explicitly (the test suite and
`Makefile` opt in for local dev; a real deployment never sets it). (2)
`app/config.py` fell back to a hardcoded, public `TRACEX_SECRET_KEY` — the
same secret that HMAC-signs every session token — if the environment didn't
set one; it now refuses to start (`RuntimeError`) when `TRACEX_ENV` is not
`development` and that secret is still the default, forcing a unique key on
any real deployment (§9.2). Verified: full suite still 130/130 passing, and
both fixes confirmed live against a running server (dev header now returns
401; a production-mode boot without a real secret refuses to start, and
succeeds with one).

Stated plainly, matching the discipline this project used throughout rather
than smoothing over what is unverified:

- **There is no official SIH dataset to inspect.** The PS-linked Drive item
  is the problem-statement document itself; it states that participants work
  with a synthetic dataset modelled on real Bitcoin fields ("Dataset Link:
  Nil"). Every fixture and every number in this report is synthetic,
  generated by `fixtures/phase5a_100k/generate.py` from a versioned seed —
  never presented as real-world detection performance. Any file supplied at
  the event is verified with `tracex-dataset inspect --full --register`
  before import (`docs/ps26146_compliance.md`).
- **The 36 GiB Mac profile is unverified.** All measurements are from this
  15 GiB Linux development host; the Mac target in the original architecture
  proposal is a planning assumption, not a measured result.
- **Superseded: 1M-row ingestion now completes.** The §4 OOM was the
  in-memory graph builder; imports that do not fit this machine's memory now
  run in the bounded on-disk mode (`app/engine/bounded.py`), and feature rows
  go to a Parquet store instead of the database. Measured 1M / 3M runs are in
  `docs/scale.md`.
- **The unsupervised model structurally cannot win the discrimination
  task** (§3) — this is reported as a property of the problem (a benign
  lookalike that satisfies the same predicate is, by construction, equally
  anomalous against ordinary traffic), not a defect to be tuned away.
- **GPU/CUDA is unavailable on this host** (`nvidia-smi` cannot reach a
  driver); every measurement in this report is CPU-only.
- **The supervised comparator layer (S) is research-only.** It trains on the
  fixture's generator truth, which is close to circular for the motif task,
  and is excluded from the deployable configuration
  (`make anomaly-stack`, not `make anomaly-stack-demo`).

## 7. Reproduction guide

Every claim in this report can be regenerated from a clean checkout:

```bash
make dataset                    # regenerate the 100K fixture (generator v2), ~29 s
make phase-zero                 # Phase 0 fixture/schema verification
make phase-one phase-two phase-three phase-four   # ruff + unit tests per phase
make phase-five-a-smoke         # Phase 5A development smoke gate
make anomaly-stack              # deployable unsupervised ranking, all tasks
make anomaly-stack-holdout      # the one-shot final holdout
make anomaly-stack-test         # ruff + 39 focused ML tests
make phase-six-test             # crash/retry + offline/access + Phase 4 regression
make phase-six-throughput       # staged real-pipeline timing (100K)
make phase-six-query-latency    # bounded query latency, 1 & 4 readers
make phase-seven-test           # offline-launch + evidence-export-honesty tests
make phase-seven-walkthrough    # the full Phase 7 case walkthrough + accuracy-honesty run
uv run --extra ml pytest -q     # full suite: 130 passing
```

## 8. Historical assistant and deployment notes

The former optional assistant and its cloud/tunnel setup instructions have been retired. Historical test counts below describe the old release, not this implementation. The current product contains no LLM/chat endpoints, models, download steps or cloud runtime dependency. Deterministic evidence presentation replaces generated explanations. See [the current MacBook/Linux runbook](macbook_runbook.md).

## 10. Conclusion

The full chain the Phase 7 release gate names — a fresh authorised case
imports a file, reconciles accepted/quarantined counts, opens a bounded true
UTXO graph, produces a reviewable model-scored lead, survives a restart, and
exports verifiable evidence offline — has been exercised end to end over live
HTTP responses on this host, with every stage's evidence checked into this
repository (`experiments/runs/`, `docs/phase6.md`, `docs/phase7.md`). Where a
component has a real, measured limitation — the 1M-row ceiling, the
discrimination task, the unverified official dataset — it is stated as such
rather than worked around with an invented number. This document, together
with `docs/phase1.md` through `docs/phase7.md`, is the complete written
record of the submission; the presentation deck and demo video referenced by
the same Phase 7 deliverable list are separate artifacts outside this
repository's scope.
