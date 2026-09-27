# Phase 3 — accurate Bitcoin graph

Every graph snapshot is derived only from receipt-approved Phase 2 Parquet fragments. The builder creates an immutable DuckDB file with indexed `nodes` and `edges` tables and emits `graph.delta_ready` only after the file and its control-plane record are ready.

## Canonical graph rules

- Transaction identity is case-scoped `(case_id, network, txid)`; output identity is `txid:vout`.
- `transaction -> output` (`CREATES_OUTPUT`) records a created UTXO.
- `output -> transaction` (`SPENT_BY`) exists only if a supplied `prev_txid` and `prev_vout` resolve to a committed output. No input is allocated to a particular output.
- `output -> address/script` (`LOCKED_TO`) records script association, not a wallet/person identity.
- Network observations form separate `OBSERVED_TX`, `SEEN_FROM`, and `SEEN_TO` edges. They make no origin or ownership claim.
- Missing outpoints, prior outputs outside source coverage, double-spend conflicts, and value-accounting violations are reported as coverage; none create invented graph edges.

## Bounded API

`GET /v1/cases/{case_id}/graph?seed=<node-id-or-txid>&depth=1&node_limit=200&edge_limit=500`

The defaults are depth 1, 200 nodes, and 500 edges. Hard limits are depth 5, 1,000 nodes, and 3,000 edges. The response declares the source snapshot, coverage, truncation counts, and a continuation cursor when capped. All values are parameterized; no client SQL or filesystem path is accepted.

## Gate

```bash
make phase-three
```

The graph integration test ingests an explicit funding output followed by a two-output spend. It verifies the only spend lineage is `old transaction -> old output -> new transaction`, checks value accounting, confirms network records remain separate, and rejects oversized query caps.
