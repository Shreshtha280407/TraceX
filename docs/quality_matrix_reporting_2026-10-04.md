# Quality-matrix reporting correction — 2026-10-04

This records the reporting correction before the [recipient-history/deployment
follow-up](recipient_history_and_deployment_2026-10-04.md). Its historical default
policy description and 36-test measurement are retained, not relabelled as newer
deployment/quality evidence.

Scoped follow-up to owner-committed `c70fcb4`. The starting tree was clean; no
applicable AGENTS.md was found. This changes evaluation reporting, not product
features, fitted weights, scoring semantics or case eligibility. No commit/push,
large workload, existing final-holdout evaluation or destructive cleanup was
performed for this correction.

## Bug and corrected behavior

The former aggregator omitted null AP/P@100/AP-delta values from minima and
substituted zero for missing benign false-positive counts. A measurable dataset
could therefore hide an unevaluable dataset and produce apparently passing gates.

The `quality-matrix-v2` summary is fail-closed:

- Missing, invalid or non-finite required measurements produce **INCOMPLETE**.
  The affected matrix-wide extremum is null, not a measured-subset minimum/maximum.
- Gate booleans are false for **NOT EVALUABLE** or **NOT APPLICABLE**. Human-readable
  statuses and dataset/report-specific reasons are retained in `gate_details` and
  `metric_coverage`, including through sanitized compact export.
- AP, AP comparison and recall are not evaluable when there are no positive labels.
  Missing labelled task results are reported rather than crashing budget checks.
  An AP delta cannot establish non-regression if candidate AP is unmeasurable.
- P@100 is not applicable only when the known labelled population is below 100.
  An unknown count cannot justify exclusion. Mixed populations record every
  exclusion and require measurements from every eligible dataset. If none can
  exercise P@100, its gate is not a pass.
- Missing reviewer-budget metrics or benign-control counts are not silently
  discarded. Valid measured zeroes remain zeroes. Different recorded reviewer
  capacities and mixed frozen candidate releases remain rejected.

The output is saved before the aggregate CLI returns **2** for incomplete
measurements, **1** for failed/unexercised threshold gates, or **0** only for
complete, passing automated threshold checks. `quality_gates_passed` is always a
boolean. A passing threshold summary is **not** a production promotion or a
representative-label, benign-FP or calibration-applicability decision.

Previously published compact results remain historical and were not rewritten.
Re-aggregate saved per-dataset reports with the corrected command before relying
on a previous matrix-level pass. This does not require refitting or reopening
raw final holdouts.

## Verification actually run

**36 passed, 0 failed, 0 skipped in 4.47 s**: 28 metadata-only reporting regression
checks, the existing four-backend/transfer/stress checks on 64-row fixtures, and
the existing sanitized compact-export/strict-time-gate check. One existing
Starlette/httpx deprecation warning remains. This is not a full-suite or quality
acceptance claim.

```sh
uv run pytest tests/unit/test_quality_matrix_reporting.py tests/unit/test_quality_wiring.py tests/unit/test_integrated_boundaries.py::test_strict_under_1800_gate_and_compact_results_are_underlying_sanitized_data -q --junitxml=var/NEW_QUALITY_REPORTING_TESTS.xml
uv run ruff check scripts/quality_matrix.py scripts/export_review_package.py tests/unit/test_quality_matrix_reporting.py tests/unit/test_quality_wiring.py
uv run python -m scripts.quality_matrix aggregate --help
git diff --check
```

Ruff, CLI help and diff checks passed. Retained JUnit:
`var/quality-matrix-reporting-20261004-01/tests.xml`.
Regression coverage includes the reported passing-plus-unmeasurable reproduction,
null/absent AP/P@100/comparison/reviewer/benign metrics, no labels/no positives,
small/mixed/unknown populations, non-finite and out-of-range values, legitimate
zeroes, boolean gates, all three CLI exit codes, immutable output paths and
compact-export preservation of dataset-specific reasons. Numerical values in
metadata-only tests are test inputs, not measured model-quality results.

## Product limitations remain unchanged

The default product still uses **`anomaly-stack-v2`** with retrospective burst
scoring. Adding HGB/XGBoost/LightGBM/hybrid candidate machinery does not deploy
their quality; explicit validation-only selection, a pinned frozen artifact,
installation and case eligibility are required. No production candidate was
promoted by this correction.

**Benign merchant false positives have not been shown to decrease.** Structured
explanations and benign alternatives assist investigator interpretation; they
do not establish improved precision. That requires applicable labelled evaluation
of the selected product procedure. The MacBook 3M/time and representative
generalization gates remain NOT RUN; no high-accuracy guarantee is made for
arbitrary uploads.
