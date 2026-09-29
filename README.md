# TraceX

Offline, case-scoped Bitcoin-intelligence backend. **Phases 0–4.1** are implemented: frozen contract/fixture, case-scoped upload/jobs, receipt-approved bulk ingestion, a correct UTXO graph, and deterministic reviewable findings including bounded peeling-chain, CoinJoin-like structure, and synthetic-review-seed context.

**The anomaly stack is implemented and measurable.** Six scoring layers over four grains — transaction shape, spend latency as survival, causal per-entity baselines, population motif-burst detection, bounded graph context, and p-value fusion — plus an ablation harness that runs every layer alone and every combination against the deterministic rule baseline. See [docs/anomaly_stack.md](docs/anomaly_stack.md).

```bash
make dataset        # regenerate the 100K fixture (generator v2), all four formats
make anomaly-stack  # every layer, every combination, all three evaluation tasks
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

## Contract layout

- `docs/requirements.md` — requirement, authority, uncertainty, and acceptance register.
- `docs/hardware.md` and `docs/environment.manifest.json` — measured local preflight and offline runtime constraints.
- `data_manifest.json` — source/fixture inventory and checksums after fixture generation.
- `schemas/v1/` — canonical JSON Schema definitions.
- `examples/` — valid v1 records and source-location examples.
- `fixtures/demo_100k/` — reproducible 100,000-transaction synthetic fixture and evaluation-only truth.

See `fixtures/demo_100k/README.md` before using the fixture. Never turn an address, script, or network observation into an ownership claim without separately versioned, reviewable evidence.
