# TraceX

Offline, case-scoped Bitcoin-intelligence backend. **Phases 0–7** are implemented: frozen contract/fixture, case-scoped upload/jobs, receipt-approved bulk ingestion, a correct UTXO graph, deterministic reviewable findings including bounded peeling-chain, CoinJoin-like structure, and synthetic-review-seed context, plus measured throughput/query-latency, proven crash-and-retry recovery, proven offline/cross-case isolation, and a reproducible submission proof package (offline launch, a real end-to-end case walkthrough, an accuracy-honesty demonstration, and case-scoped evidence export). See [docs/phase6.md](docs/phase6.md) for the measured performance numbers and [docs/phase7.md](docs/phase7.md) for the submission proof package, both on this host's actual hardware, not an assumed target. [docs/final_report.md](docs/final_report.md) is the complete phase-by-phase project report (backend/data-pipeline scope; the presentation deck and demo video are separate, out-of-repository artifacts).

**The anomaly stack is implemented, causal, and wired into the case path.** Six scoring layers over four grains — transaction shape, spend latency as survival, causal per-entity baselines, population motif-burst detection, bounded graph context, and p-value fusion — plus an ablation harness that runs every layer alone and every combination against the deterministic rule baseline. Every feature uses only facts at or before its own transaction's timestamp, proved by truncation property tests. Ingestion runs it automatically at snapshot completion, writing findings the existing findings endpoint serves (`TRACEX_ML_FINDINGS=0` to disable). See [docs/anomaly_stack.md](docs/anomaly_stack.md).

```bash
make dataset        # regenerate the 100K fixture (generator v2), all four formats
make anomaly-stack  # unsupervised ranking — the deployable configuration
make anomaly-stack-demo  # adds the supervised comparator (demo/research only)
```

## Phase 0 quick check

```bash
python3 fixtures/demo_100k/generate.py --verify
python3 -m unittest discover -s tests -v
```

The generated demonstration fixture is synthetic, deterministic, and must never be described as official SIH data. The official PS-linked material has not been supplied to this repository; its schema, licensing, labels, and outpoint availability remain pending verification in `docs/requirements.md`.

For Phase 1 setup, routes, security boundary, and smoke-test evidence, see `docs/phase1.md`.
For ingestion/snapshots, graph behavior, and deterministic findings, see `docs/phase2.md`, `docs/phase3.md`, and `docs/phase4.md`.

## Phase 5A development smoke gate

```bash
make phase-five-a-smoke
```

This regenerates and exercises the separate synthetic 10K fixture through ingestion, UTXO graph construction, deterministic findings, Phase 4.1 signals, feature export, and the Phase 5A input contract. It does not train or select a model.

## Phase 6 performance, recovery, and security

```bash
make phase-six-test             # crash/retry + offline/cross-case-access + Phase 4 regression tests
make phase-six-throughput       # staged timings against the real 100K fixture
make phase-six-throughput-1m    # the same pipeline at 1,000,000 synthetic rows
make phase-six-query-latency    # 200+ bounded graph queries at 1 and 4 concurrent readers
```

Every number is measured on this development host's actual hardware, not the
36 GiB Mac profile the master architecture describes as a target. See
[docs/phase6.md](docs/phase6.md) for the full table, what did and did not hit
the stated targets, the crash/retry and offline/isolation evidence, and a
real cross-signal duplicate-finding bug found and fixed while building the
1M-row benchmark.

## Phase 7 submission proof package

```bash
make phase-seven-test          # ruff + offline-launch + evidence-export-honesty regression tests
make phase-seven-walkthrough   # original file -> progress -> graph -> lead -> model rank -> source rows -> analyst decision -> export
```

`scripts/phase7_case_walkthrough.py` drives that whole chain through the real
HTTP API and worker on a real slice of the 100K fixture, then demonstrates
accuracy honesty with the fixture's own paired scenarios: a genuine
`coinjoin_like` structure and `nearmiss_rule_positive`, its high-volume benign
lookalike that satisfies the deterministic rule exactly. See
[docs/phase7.md](docs/phase7.md) for the full evidence, including a real
evidence-export honesty bug this phase found and fixed (an export endpoint
was hardcoding "no model ran" even when an ML finding produced the row).

## Contract layout

- `docs/requirements.md` — requirement, authority, uncertainty, and acceptance register.
- `docs/hardware.md` and `docs/environment.manifest.json` — measured local preflight and offline runtime constraints.
- `data_manifest.json` — source/fixture inventory and checksums after fixture generation.
- `schemas/v1/` — canonical JSON Schema definitions.
- `examples/` — valid v1 records and source-location examples.
- `fixtures/demo_100k/` — reproducible 100,000-transaction synthetic fixture and evaluation-only truth.

See `fixtures/demo_100k/README.md` before using the fixture. Never turn an address, script, or network observation into an ownership claim without separately versioned, reviewable evidence.
