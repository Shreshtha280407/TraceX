# ADR-018: Phase 7 Part 6 final release decision — deterministic baseline retained, ML scoring deferred

Status: **accepted for this release** (2026-09-20). Owner: Shreshtha. Related:
ADR-016 (Decision 5: the ML-vs-rules-baseline selection policy), ADR-017
(Gate C release freeze), `docs/architecture/
phase-7-evaluation-and-model-governance.md` ("Part 6" section).

## Decision

The initial deployed TraceX product ships with the existing, transparent
Phase 5 deterministic rules baseline (`phase5-rules-baseline`,
`phase5_preliminary_rules_v1`) as the only relationship-scoring component.
No Logistic Regression, XGBoost, LightGBM, or other trained model is
implemented, trained, compared, installed, downloaded, benchmarked, or
selected as part of this decision. `configs/benchmarks/release-freeze.v1.json`
already reflects this — `final_relationship_ml_selected: false`, the sole
enabled `tracex-release-v1-baseline` configuration loads no artifact, and all
three ML candidates carry `status: "deferred"`. This ADR records why the
deferral is now final for this release rather than a placeholder pending
Part 6's own comparison run.

## Why the comparison itself is deferred, not just its outcome

ADR-016's Decision 5 fixed the *policy* for an ML-vs-rules comparison —
material holdout improvement required, Logistic Regression preferred within
tolerance, rules baseline retained absent a clear win — on the assumption
that Part 6 would run that comparison against real, independently labelled
case-level holdout data. No such dataset exists yet. The Phase 7 Part 1
synthetic case plan (`synthetic-case-plan.v1.json`) freezes evaluation
*splits*, not a real labelled relationship-outcome ground truth; using it to
train or score a supervised correlation model would evaluate the model
against its own synthetic construction, not against real investigative
signal. Running Logistic Regression/XGBoost/LightGBM against that plan would
produce numbers that look like a benchmark but are not evidence a
future reader could trust, exactly the "post-hoc rationalization dressed
up as evaluation" failure ADR-016 was written to prevent.

The team's decision is therefore to defer the comparison itself: ship the
already-shipped, already-tested, transparent deterministic baseline now, and
revisit ML relationship scoring only after the product is deployed and a
proper, independently labelled relationship dataset exists to evaluate
against. This is consistent with ADR-016 Decision 5's own fallback ("if no
ML candidate materially improves on the rules baseline, the rules baseline
is retained") — no candidate was found to improve on it, because none was
run, and the rules baseline is what remains.

## Constraints on any future ML relationship scorer

These constraints are permanent, not specific to a future dataset or model
family — they follow directly from `CLAUDE.md`'s "no automatic identity
merge, guilt conclusion" boundary and ADR-016 Decision 5's own explainability
requirement, restated here so a later phase cannot treat their absence from
this ADR as license to skip them:

- A future ML scorer may only **rank reviewable candidate links** for
  investigator review — the same `CorrelationSubmission`/candidate-link shape
  the rules baseline already produces. It must never auto-merge two entities
  into one identity, never bypass the existing review workflow, and never
  present or imply a probability of guilt or culpability.
- Selection still follows ADR-016 Decision 5's fixed policy: material,
  reproducible holdout improvement required; Logistic Regression preferred
  within tolerance over a boosted-tree candidate; the rules baseline retained
  if no candidate clears that bar.
- Any future evaluation requires its own new, versioned evaluation cycle
  (per ADR-017's "future expansion requires a new versioned manifest/
  evaluation cycle" rule) — it cannot mutate the frozen `gate-b-v1` evidence
  or this release's configuration in place.

## Phase 8 scope, unchanged by this decision

Phase 8 tests and deploys the frozen `tracex-release-v1-baseline`
configuration on a LAN/multi-laptop setup. It validates deployment, not
scoring: it does not change, retrain, or re-select the relationship-scoring
algorithm this ADR retains.

## Phase 7 completion

Phase 7 is complete for this release's scope: Parts 1-5's Gate B evidence and
Gate C's release freeze stand as recorded in ADR-017, and Part 6 closes the
phase with the deterministic baseline as the shipped configuration. ML
relationship scoring is documented future work, not falsely claimed as
evaluated or completed.

## Alternatives considered

- **Run Logistic Regression/XGBoost/LightGBM against the synthetic case plan
  anyway, to have *some* comparison on record.** Rejected: a comparison
  against data that was never built to represent real relationship-outcome
  ground truth would produce a number that looks like evidence without being
  evidence, and risks being cited later as if it were a real evaluation.
- **Leave Part 6 an open placeholder rather than an accepted ADR.** Rejected:
  an unrecorded, undecided placeholder is exactly the ambiguity ADR-016 was
  written to close for Parts 2-5; Part 6 deserves the same explicit,
  dated decision record, even though the decision is "not yet," not a
  selected winner.
