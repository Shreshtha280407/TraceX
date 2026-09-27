# Phase 2 — bulk ingestion and snapshots

Phase 2 is the only component that parses uploaded bytes. The request handler saves bytes and creates a job; the worker verifies their hash, then uses a streaming adapter and commits immutable derived fragments in batches.

## Supported source envelope

CSV, NDJSON, a top-level JSON array, and XML `transaction`/`record`/`row` elements are supported. The normalized public SIH-style fields are `timestamp`, `src_ip`, `dst_ip`, `src_port`, `dst_port`, `txid`, `input_addresses[]`, `output_addresses[]`, `input_amounts[]`, `output_amounts[]`, `fee`, `script_type`, `geo_country`, and `asn`.

- Array values in CSV are JSON arrays. Amounts are parsed as exact decimal BTC and converted to integer satoshis; excess precision and overflows are quarantined.
- Generic `timestamp` is retained as a source timestamp with `unknown` meaning. It is not silently called block time. Network records retain it separately as observer time.
- If a source does not provide `prev_txid`/`prev_vout`, normalized inputs contain `null` outpoints. No spend edge is made up.
- CSV locators use logical record index, JSON locators use JSON Pointer, and XML locators use element path plus ordinal. A quoted multiline CSV field therefore remains replayable.

## Commit and recovery contract

For each batch, the worker writes Parquet to a unique staging path, hashes it, atomically renames it inside the evidence volume, then commits `fragment_receipts`, a checkpoint, snapshot state, and an outbox-backed progress event in PostgreSQL. Files without receipts are ignored as orphans. Retrying a batch compares the proposed hash with any existing immutable target.

`GET /v1/evidence/{source_id}/records?locator=record:1` replays an authorized record from the immutable original source; it accepts no client filesystem path. `GET /v1/jobs/{job_id}` reports exact parsed/accepted/quarantined counters and snapshot ID. Results remain provisional until the final source manifest is complete.

## Gates

```bash
make phase-two
make phase-two-100k
```

The first command runs CSV end-to-end ingestion with quarantined malformed/duplicate rows and JSON/XML safety tests. The second imports the deterministic 100,000-row `fixtures/demo_100k/ingestion_rows.ndjson` source into a disposable store and requires exact `100000 = 100000 accepted + 0 quarantined` reconciliation.
