# Phase 5A 10K development smoke fixture

This is a deterministic synthetic development smoke fixture with exactly 10,000 canonical Bitcoin transaction rows. It is not real blockchain data, a real-world attribution dataset, or a training corpus. It does not begin Phase 5B model training and it adds no ML dependency.

Generate or verify it with:

```bash
python3 fixtures/ml_smoke_10k/generate.py
python3 fixtures/ml_smoke_10k/generate.py --verify
```

Run the full ingestion, UTXO graph, Phase 4/4.1 finding, synthetic-seed, feature-export, and Phase 5A input-contract path with:

```bash
make phase-five-a-smoke
```

The fixture contains varied low-activity flows, benign batch/treasury-shaped controls, a verified peeling-chain review candidate and ordinary control, equal-output structure and ordinary multi-output control, a synthetic evaluation seed with a two-hop UTXO path, a disconnected control, intentionally unresolved prevouts, duplicate source candidates, and relay/NAT-like network observations. Scenario identities exist only in `evaluation_truth.json`, which is evaluation-only and must never be ingested.
