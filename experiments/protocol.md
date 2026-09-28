# Phase 5A experiment protocol v1 — preparation only

Status: **No candidate has won yet.** This protocol is written before any model work. It permits no Phase 5B training, no model selection, no model download, and no new ML runtime dependency.

## Reproducibility identity

- Fixture source: `fixtures/phase5a_100k/`
- Generator version: `1.0.0`
- Fixed seed: `tracex-phase5a-prep-100k-v1`
- Config SHA-256: `1adf1a3a41e7dee43ce54fed55b833092bf0a8c0985bd7698fd4a045adb522b3`
- Generator SHA-256: `8c16c6d9dd785c5f890182cf7d8ef8c641dda342f89b875b80a739e2fc085fa1`
- Fixture hash source: generated `datasets/phase5a_100k/fixture_manifest.json`; it records an exact SHA-256 and byte count for every generated artifact. Regenerate it with the documented command before any run and record its manifest hash in the run log.
- Feature-contract version: `Phase 5A additive feature contract v1`; frozen response schema remains `phase4.1-feature-v1`.

## Fixed splits and leakage controls

The generated evaluation truth (never ingested) defines time-contiguous, group-disjoint splits:

| Split | Group IDs | Rows |
| --- | --- | ---: |
| train/reference | `g-001` through `g-007` | 70,000 |
| validation | `g-008`, `g-009` | 15,000 |
| final holdout | `g-010`, `g-011`, `g-012` | 15,000 |

No group appears in more than one split. All validation times follow train/reference, and all final-holdout times follow validation. The fixture contains 82.5% benign-control rows and 17.5% review-pattern rows; this is an engineering scenario mix, not a real prevalence estimate. Missing prior history is explicit null prevout coverage, not an inferred relationship. Repeated endpoint observations model relays/NAT only and cannot yield wallet ownership, propagation, labels, or model features.

Leakage checklist:

- [ ] Do not ingest `evaluation_truth.json`.
- [ ] Do not include labels, scenario/group IDs, outcomes, future rows, raw addresses/scripts, IPs/endpoints, TXIDs, source/case IDs, locators, seed identities/reasons, or reviewer data in model inputs.
- [ ] Keep snapshot and graph-snapshot versions fixed inside each comparison.
- [ ] Use only features available at the end of each row's time window.
- [ ] Preserve null/missingness indicators; do not silently treat missing history as zero activity.
- [ ] Enforce group-disjoint and time-respecting split assignment before any score calculation.
- [ ] Compare any future candidate only against the Phase 4 rule-only ranking on identical records, snapshots, and review budget.

## Required reporting if model work is later authorized

Use the same final-holdout review budget for all comparators and report Precision@20, Precision@50, benign false positives per 1,000 windows, review workload, score stability across fixed reruns, CPU scoring p95 latency, and peak RSS. Define review workload as windows above the fixed review budget; define score stability as rank/score variation across reruns with identical seed and inputs. Fixed for when Phase 5B is explicitly authorized: `TRAINING_SEED=42` (arbitrary, fixed for reproducibility — every candidate fit and rerun uses this same value) and `TRAINING_BUDGET=600s CPU wall-clock per candidate fit, laptop-class single machine, no GPU` (matches the offline Linux-CPU-primary deployment posture; a candidate that cannot fit within this budget on the reference 100K snapshot is ineligible, not granted more time).

The current Phase 4 rule-only ranking is the required comparator, not a label source. Synthetic-only results are an engineering demonstration and do not prove real-world detection accuracy. The feature contract identifies current implementation gaps: no frozen exporter fields yet for transformed/robust/concentration value features, burstiness or rolling changes, graph degree/overlap, universal transaction count fields, or network-context aggregates. Do not claim those features are implemented or fabricate them from fixture truth.
