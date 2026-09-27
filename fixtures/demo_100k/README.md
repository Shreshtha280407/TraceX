# `demo_100k` synthetic fixture

This fixture supplies **100,000 canonical synthetic transactions** for deterministic development and correctness checks. It is not blockchain data, an official SIH dataset, evidence about people, or a training corpus for real-world attribution.

## Reproduce and verify

```bash
python3 fixtures/demo_100k/generate.py
python3 fixtures/demo_100k/generate.py --verify
```

The generator uses only the Python standard library and the pinned seed in `fixture_config.json`. It produces compact NDJSON files, writes SHA-256/counts to `fixture_manifest.json`, and is deterministic across supported Python versions. The generated NDJSON bulk artifacts are intentionally ignored by Git; generate them locally before running the scale gate. `--verify` regenerates into a temporary directory, validates fixture invariants, and byte-compares every generated artifact to the checked-in manifest.

## Contents

| File | Meaning |
| --- | --- |
| `funding_outpoints.ndjson` | Explicit synthetic pre-existing UTXOs used to begin the fixture. They are a fixture ledger, not coinbase records. |
| `transactions.ndjson` | Canonical transaction records; exactly 100,000. |
| `ingestion_rows.ndjson` | Clean 100,000-row NDJSON adapter input using SIH-style address/amount arrays; the Phase 2 scale gate source. |
| `inputs.ndjson` / `outputs.ndjson` | Normalized input/output facts. Missing outpoints are explicitly `null`, never guessed. |
| `network_observations.ndjson` | Separate synthetic relay observations. They do not prove ownership or transaction origin. |
| `duplicate_candidates.ndjson` | Exact duplicate source-record candidates, separate from canonical facts. Later ingestion must count/quarantine/deduplicate explicitly. |
| `evaluation_truth.json` | Evaluation-only opaque scenario membership and expected structural properties. Do not ingest, expose, or use it as a source label. |
| `fixture_manifest.json` | Generated filenames, row counts, SHA-256 digests, and invariant counts. |

Scenarios deliberately include normal change outputs, valid multi-input/multi-output transactions, exchange-style batching, unresolved/missing outpoints, duplicate candidates, repeated relay sightings, and an opaque hidden evaluation scenario. Ordinary transactions with fully known inputs obey `sum(inputs) = sum(outputs) + fee`; records with a missing prevout are marked as partial coverage and are excluded from conservation assertions.
