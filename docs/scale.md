# Scaling TraceX to millions of rows

Status: measured on the 15 GiB / 4-CPU Linux development host (SQLite control
database). Numbers below are reproducible with the commands in each section.

## What changed to make multi-million-row imports work

| Bottleneck at 1M+ rows | Fix |
| --- | --- |
| The graph builder, deterministic findings and ML stage held every fact of the snapshot as Python objects (the 1M run was OOM-killed at ~10 GiB in Phase 6). | `app/resources.py` estimates the in-memory working set per import from its record count and the RAM this machine (or container) can actually spare. Imports that fit run fully in memory (fastest); the rest run in the **bounded** mode (`app/engine/bounded.py`): facts are staged in an on-disk DuckDB database, set-wise work is SQL that spills to disk, and per-row detector logic runs over one chunk / address partition at a time. Output is byte-identical to the in-memory path (graph, every feature row, every finding — checked by hash at 100K under 3 budgets). |
| ~8 address-window feature rows per transaction were inserted into the control database as JSON (834K rows = 3.9 GB of SQLite at 100K; ~25M rows / >100 GB at 3M). | Feature rows are written to one zstd-compressed Parquet file per snapshot (`app/engine/feature_store.py`, recorded with its SHA-256 in `feature_stores`). Same rows, a fraction of the space, no row-by-row inserts: the 100K end-to-end import dropped from 163 s to 111 s. Export streams JSON, pages it, or serves the Parquet file. |
| Duplicate-txid detection kept hex strings for every txid and variant. | Binary txid keys and 16-byte variant digests (about half the memory). |
| Entity clustering, Geo-IP enrichment, network correlation and embeddings. | Built in DuckDB plus compact numpy arrays (union-find over input pairs, sparse SVD), identical in both modes. |

## How to reproduce

```bash
# realistic load sets: N copies of the labelled 100K fixture (motifs, CoinJoins,
# peeling chains, near-misses, network fields), each with its own txids,
# addresses and time range
uv run python scripts/scale_fixture.py --copies 10 --output datasets/scaled/rows_1m.ndjson
uv run python scripts/scale_fixture.py --copies 30 --output datasets/scaled/rows_3m.ndjson

# the real deployment shape: uvicorn API + separate worker, streamed upload
uv run --extra ml python scripts/scale_benchmark.py datasets/scaled/rows_3m.ndjson
```

## Results

RESULTS_PLACEHOLDER
