# Phase 5A local 100K synthetic preparation fixture

This is a deterministic, seed-controlled source generator for a local dataset with exactly 100,000 canonical synthetic Bitcoin transaction records. It supports Phase 5A data preparation and SIH demonstration only. It is not blockchain data, an attribution dataset, proof of real-world detection performance, or Phase 5B model training.

Generate both supported ingestion adapters and verify deterministic output:

```bash
python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson --verify
```

The generator uses the Python standard library and the versioned seed in `fixture_config.json`. `--verify` checks every UTXO/split/raw-record invariant and every generated file hash against its manifest; the focused test independently regenerates a second directory and compares hashes. It writes only beneath the supplied output directory. `datasets/phase5a_100k/` is intentionally ignored by Git; source, documentation, tests, and the manifest schema in this directory are the reviewable repository artifacts.

## Local output

| File | Purpose |
| --- | --- |
| `ingestion_rows.csv` / `ingestion_rows.ndjson` | Equivalent SIH-compatible raw ingestion rows, including structured inputs/outputs. |
| `transactions.ndjson`, `inputs.ndjson`, `outputs.ndjson` | Canonical normalized-shaped facts used to validate UTXO correctness. |
| `network_observations.ndjson` | Separate TXID-linked relay/endpoint observations. It never asserts wallet ownership, transaction origin, or control of an IP. |
| `duplicate_candidates.ndjson` | Additional source-record candidates; they do not change the 100,000 canonical count. |
| `evaluation_truth.json` | Evaluation-only labels, expected controls, and time/group split truth. Never upload or ingest it. |
| `fixture_manifest.json` | Generated counts, deterministic hashes, invariants, and provenance locators. |

The raw source has opaque deterministic addresses and transaction IDs. It deliberately contains no scenario names, labels, split/group identifiers, outcomes, or future information. Those live only in the generated `evaluation_truth.json`, marked `evaluation_only=true` and `do_not_ingest=true`.

## Scenario coverage

The fixture has more than ten time-bounded entity/scenario groups, with 82.5% benign controls and 17.5% review-pattern groups. It includes low-activity flows, benign batch and treasury-shaped transfers, a verified peeling-chain candidate and negative sequence, equal-output structure and regular multi-output control, a synthetic review seed with a verified two-hop downstream path, a disconnected control, unresolved prevouts, duplicate candidates, and repeated relay/NAT-like observations. The final holdout has 15,000 rows, and split groups are disjoint and temporally ordered.

`feature_contract.md` documents exactly which required SIH feature families the current exporter genuinely produces. It does not fabricate a model-input feature file or modify the frozen `feature_schema.json`.
