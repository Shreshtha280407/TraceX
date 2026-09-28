# Phase 5A 100K schema map

All generated Bitcoin identifiers and addresses are deterministic opaque values. The data is synthetic and scoped to `bitcoin-regtest`.

| Artifact | Key / locator | Contents | Notes |
| --- | --- | --- | --- |
| `ingestion_rows.ndjson` | `source_record_id`, `source_locator`, `txid` | SIH raw fields: timestamp, network, address/amount arrays, fee, script type, endpoint metadata, structured `inputs` and `outputs`. | One JSON object per source record. Duplicate candidates have a distinct source record ID and the same canonical TXID. |
| `ingestion_rows.csv` | same as NDJSON | Exact logical equivalent of NDJSON. Array/object fields are compact JSON strings, which the existing CSV normalizer accepts. | Reconcile using ordered `(txid, source_record_id)` pairs. |
| `transactions.ndjson` | `txid` | Canonical transaction facts, fee, timestamp, source references. | Exactly 100,000 distinct transaction IDs. |
| `inputs.ndjson` | `(txid, vin)` | `prev_txid`, `prev_vout`, address and `amount_sats`. | Null/null prevouts are intentional missing history, never inferred. |
| `outputs.ndjson` | `(txid, vout)` | address, script type and `amount_sats`. | Only a real matching `(prev_txid, prev_vout)` can form a spend link. |
| `network_observations.ndjson` | `observation_id` | TXID-linked observer/relay endpoints, timestamp, country and ASN. | Off-chain observation only; graph construction creates OBSERVED_TX / endpoint edges, never wallet ownership. |
| `duplicate_candidates.ndjson` | duplicate source record ID | Link from duplicate source candidate to its canonical TXID/source record. | Not a transaction fact and never increases canonical count. |
| `evaluation_truth.json` | opaque group IDs | Split membership, scenario/outcome truth and expected controls. | Evaluation-only; never ingest, upload, or use as model input. |
| `fixture_manifest.json` | filename | Counts, file SHA-256 values, invariant results and deterministic provenance. | Validate against `fixture_manifest.schema.json`. |

The raw field `source_locator` is provenance metadata, not a model feature. Similarly, raw addresses, IPs/endpoints, TXIDs, source record IDs, ASNs, labels, and any evaluation truth must be excluded from a future model input projection.
