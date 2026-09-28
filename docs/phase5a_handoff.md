# Phase 5A handoff — frozen Phase 4.1 features

The frozen address-plus-time-window dataset is exported with `GET /v1/cases/{case_id}/features/export`. Every row is case-scoped and carries `snapshot_id`, `graph_snapshot_id`, window boundaries, coverage, source references, and `feature_schema_version: phase4.1-feature-v1`. Only use rows from an explicit immutable snapshot/version; do not combine snapshots without a separately versioned preparation step.

## Phase 4.1 fields

| Field | Type | Range / missing behaviour | Phase 5A role |
| --- | --- | --- | --- |
| `peeling_chain_score` | float | `0..1`; `0` when no verified candidate | ML input candidate |
| `peeling_chain_length` | integer | `>=0`; `0` when absent | ML input candidate |
| `peeling_chain_total_duration_sec` | float | `>=0`; `0` when absent or timing is unavailable (see coverage) | ML input candidate |
| `peeling_chain_evidence_count` | integer | `>=0`; `0` when absent | metadata / quality control |
| `coinjoin_like_score` | float | `0..1`; `0` when no qualifying structure | ML input candidate |
| `equal_output_count` | integer | `>=0`; `0` when absent | ML input candidate |
| `equal_output_value_sats` | integer/null | `null` when no equal-output group | ML input candidate after explicit imputation policy |
| `coinjoin_like_evidence_count` | integer | `>=0`; `0` when absent | metadata / quality control |
| `risk_propagation_score` | float | `0..1`; `0` when no supported synthetic path | **metadata only; never model input** |
| `risk_seed_distance` | integer/null | `null` when no supported path | **metadata only; never model input** |
| `risk_path_evidence_count` | integer | `>=0`; `0` when absent | **metadata only; never model input** |
| `risk_seed_count` | integer | `>=0`; `0` when absent | **metadata only; never model input** |

The existing aggregate/time-window features can be considered separately under their documented Phase 4 contract. Coverage, uncertainty, source references, graph paths, detector reason codes, evidence counts, snapshot IDs, and graph snapshot IDs are metadata rather than ML inputs.

## Hard exclusion rule

Actual wallet/address/script values, raw IP addresses or endpoints, TXIDs, case IDs, source locators, synthetic scenario labels, synthetic review seed identities/reasons, finding IDs, and reviewer decisions must **not** be model input features. Synthetic seed proximity exists only for deterministic evaluation/demo graph context and must not be used as a target, feature, or attribution signal.

No ML model, dependency, training, selection, download, or inference is part of Phase 4.1.

## Development smoke gate

Run `make phase-five-a-smoke` to regenerate the separate deterministic 10K synthetic fixture and prove the frozen input contract through upload, ingestion, graph construction, deterministic findings, Phase 4.1 detector output, and feature export. The fixture's evaluation truth is never ingested and no model work begins at this gate.
