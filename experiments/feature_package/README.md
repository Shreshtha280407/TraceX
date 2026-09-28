# Phase 5B frozen feature package — column contract

This is documentation only. The actual generated feature package (matrix + `feature_manifest.json`) is written per-run under `experiments/runs/<run-id>/` by `scripts/phase5b_prepare_features.py` — gitignored, never committed. This file is the frozen contract that code and tests are checked against; a code change to the column list must update this file in the same change.

Population: rows from `GET /v1/cases/{case_id}/features/export`, filtered to `window_seconds == 900` (15-minute windows) — the window size the deterministic engine's own peeling-chain/coinjoin key-resolution uses.

## Included columns (37, in this exact order)

| # | Column | Source field | Impute when null | Why |
| - | --- | --- | --- | --- |
| 1 | `in_event_count` | `features.in_event_count` | never null | distinct inbound txids this window |
| 2 | `out_event_count` | `features.out_event_count` | never null | distinct spending txids this window |
| 3 | `received_output_count` | `features.received_output_count` | never null | inbound output rows |
| 4 | `outgoing_output_count` | `features.outgoing_output_count` | never null | outgoing output rows in rapid-spend context |
| 5 | `observed_counterparties` | `features.observed_counterparties` | never null | distinct inbound txids (proxy) |
| 6 | `received_value_min_sats` | `features.received_value_distribution_sats.min_sats` | never null | inbound value floor |
| 7 | `received_value_max_sats` | `features.received_value_distribution_sats.max_sats` | never null | inbound value ceiling |
| 8 | `received_value_median_sats` | `features.received_value_distribution_sats.median_sats` | never null | inbound value median |
| 9 | `received_value_total_sats` | `features.received_value_distribution_sats.total_sats` | never null | inbound value total |
| 10 | `outgoing_value_min_sats` | `features.outgoing_value_distribution_sats.min_sats` | `0` | outgoing value floor |
| 11 | `outgoing_value_max_sats` | `features.outgoing_value_distribution_sats.max_sats` | `0` | outgoing value ceiling |
| 12 | `outgoing_value_median_sats` | `features.outgoing_value_distribution_sats.median_sats` | `0` | outgoing value median |
| 13 | `outgoing_value_total_sats` | `features.outgoing_value_distribution_sats.total_sats` | `0` | outgoing value total |
| 14 | `outgoing_value_missing` | `1` iff source object was `null` | — | no outgoing outputs this window |
| 15 | `inter_event_gap_count` | `features.inter_event_gaps_seconds.count` | never null | number of gaps between distinct inbound event times |
| 16 | `inter_event_gap_min_seconds` | `features.inter_event_gaps_seconds.minimum_seconds` | `0` | shortest gap |
| 17 | `inter_event_gap_max_seconds` | `features.inter_event_gaps_seconds.maximum_seconds` | `0` | longest gap |
| 18 | `inter_event_gap_median_seconds` | `features.inter_event_gaps_seconds.median_seconds` | `0` | median gap |
| 19 | `inter_event_gap_missing` | `1` iff `count < 2` | — | fewer than two distinct event times |
| 20 | `component_node_delta` | `features.bounded_component_change.node_delta` | never null | one-hop node delta (not an ownership/entity claim) |
| 21 | `component_edge_delta` | `features.bounded_component_change.edge_delta` | never null | one-hop edge delta |
| 22 | `component_resolved_spend_edge_delta` | `features.bounded_component_change.resolved_spend_edge_delta` | never null | verified spend-edge delta |
| 23 | `first_observed_activity` | `features.first_observed_activity` (bool→0/1) | never null | also the shared missingness indicator for columns 24–28 |
| 24 | `prior_window_gap_seconds` | `features.prior_window_gap_seconds` | `0` | null exactly when col 23 is 1 |
| 25 | `baseline_in_event_count_mean` | `features.baseline_in_event_count_mean` | `0` | null exactly when col 23 is 1 |
| 26 | `activity_surge_ratio` | `features.activity_surge_ratio` | `1.0` (neutral "no change" — not 0) | null exactly when col 23 is 1 |
| 27 | `baseline_value_sats_mean` | `features.baseline_value_sats_mean` | `0` | null exactly when col 23 is 1 |
| 28 | `value_surge_ratio` | `features.value_surge_ratio` | `1.0` (neutral) | null exactly when col 23 is 1 |
| 29 | `peeling_chain_score` | `features.peeling_chain_score` | never null (defaults `0.0`) | Phase 4.1, allowed ML input |
| 30 | `peeling_chain_length` | `features.peeling_chain_length` | never null (defaults `0`) | Phase 4.1, allowed ML input |
| 31 | `peeling_chain_total_duration_sec` | `features.peeling_chain_total_duration_sec` | never null (defaults `0.0`) | Phase 4.1, allowed ML input |
| 32 | `peeling_chain_evidence_count` | `features.peeling_chain_evidence_count` | never null (defaults `0`) | Phase 4.1, allowed ML input |
| 33 | `coinjoin_like_score` | `features.coinjoin_like_score` | never null (defaults `0.0`) | Phase 4.1, allowed ML input |
| 34 | `equal_output_count` | `features.equal_output_count` | never null (defaults `0`) | Phase 4.1, allowed ML input |
| 35 | `equal_output_value_sats` | `features.equal_output_value_sats` | `0` | null when no equal-output group |
| 36 | `equal_output_value_missing` | `1` iff source was `null` | — | no equal-output group this window |
| 37 | `coinjoin_like_evidence_count` | `features.coinjoin_like_evidence_count` | never null (defaults `0`) | Phase 4.1, allowed ML input |

## Explicitly excluded (denylisted, never in the matrix)

- `feature_contract_version`, `window_seconds` (constant after the 900s filter), `bounded_component_change.scope` (constant string) — zero-information metadata.
- `rule_thresholds.*` — versioned rule constants, identical for every row in a run; metadata, not a per-row signal.
- `risk_propagation_score`, `risk_seed_distance`, `risk_path_evidence_count`, `risk_seed_count` — hard-excluded by `docs/phase5a_handoff.md`: metadata only, never model input, at any phase.
- `detector_result.*` — free-text explanation/evidence-ref metadata, not numeric.
- Row-level identifiers returned alongside `features`: `entity_ref` (raw address), `feature_row_id`, `snapshot_id`, `graph_snapshot_id`, `source_refs`, `coverage`.
- `window_start`/`window_end` — kept as a separate metadata column for split assignment and reporting only; never concatenated into the model input array.
- Raw IP, endpoint, GeoIP country/ASN, or any network-observation field — none currently exist in the exporter's output; the leakage test asserts that absence rather than assuming it.
- `evaluation_truth.json` in its entirety — read only for computing evaluation labels off to the side, never ingested, never a feature.

## Missingness policy

Every nullable source field gets a fixed-constant imputation (never fit from data — imputation with a constant cannot leak). Columns 24–28 share one indicator (column 23, `first_observed_activity`) rather than five redundant separate flags, since they are null in exactly the same rows by construction (`app/engine/findings/deterministic.py::_history_features`). Columns 14, 19, and 36 are independent missingness patterns and each gets its own indicator.
