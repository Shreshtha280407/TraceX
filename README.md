# TraceX

Offline, case-scoped Bitcoin-intelligence backend. **Phases 0–4** are implemented: frozen contract/fixture, case-scoped upload/jobs, receipt-approved bulk ingestion, a correct UTXO graph, and deterministic reviewable findings. ML has not started.

## Phase 0 quick check

```bash
python3 fixtures/demo_100k/generate.py --verify
python3 -m unittest discover -s tests -v
```

The generated demonstration fixture is synthetic, deterministic, and must never be described as official SIH data. The official PS-linked material has not been supplied to this repository; its schema, licensing, labels, and outpoint availability remain pending verification in `docs/requirements.md`.

For Phase 1 setup, routes, security boundary, and smoke-test evidence, see `docs/phase1.md`.
For ingestion/snapshots, graph behavior, and deterministic findings, see `docs/phase2.md`, `docs/phase3.md`, and `docs/phase4.md`.

## Contract layout

- `docs/requirements.md` — requirement, authority, uncertainty, and acceptance register.
- `docs/hardware.md` and `docs/environment.manifest.json` — measured local preflight and offline runtime constraints.
- `data_manifest.json` — source/fixture inventory and checksums after fixture generation.
- `schemas/v1/` — canonical JSON Schema definitions.
- `examples/` — valid v1 records and source-location examples.
- `fixtures/demo_100k/` — reproducible 100,000-transaction synthetic fixture and evaluation-only truth.

See `fixtures/demo_100k/README.md` before using the fixture. Never turn an address, script, or network observation into an ownership claim without separately versioned, reviewable evidence.
