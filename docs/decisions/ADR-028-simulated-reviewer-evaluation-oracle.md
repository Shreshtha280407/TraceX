# ADR-028: Simulated-reviewer evaluation oracle for Fulcrum dev/validation

Status: **accepted**. Gap-Closure follow-up, evaluation-tooling-only --
touches no production code path in this repository.

## Context

`graph/intelligence/evaluation.py` (ADR-025) scores a case's real
entity-resolution candidates against a human-authored truth file.
`_predicted_labels` reads the system's own effective judgement for each
candidate strictly from its recorded `entity_review_decisions`: a
`verified_same` decision counts as "predicted same"; anything else --
including no decision at all -- counts as "predicted different"
(`entity_models.EntityReviewOutcome.VERIFIED_SAME` is documented as "never
automatic, never inferred").

Run for real against `case-fulcrum-dev`/`case-fulcrum-val` for the first
time (Gap-Closure follow-up, entity_resolution_truth.json now populated
with real UUIDs): both cases' real candidates had never been reviewed by
anyone, so every metric came back trivially uninformative --
`candidate_recall: 0.0`, `candidate_precision: null`, `false_link_rate:
null`, `false_merge_rate: 0.0`. Not a bug -- an honest reflection of "no
human has looked at these yet" -- but also not a usable signal for
measuring retrieval/ranking quality on Fulcrum's dev/validation splits,
which is exactly what these splits exist for.

## Decision: an external, evaluation-only oracle tool

`TraceX-Synthetic-Data/scripts/simulate_reviews_for_evaluation.py` (sibling
repo, not part of this codebase) applies ground-truth-driven review
decisions to a Fulcrum case's real candidates by calling TraceX's real,
unmodified `POST /api/v1/entities/{entity_id}/resolution-review` and
`GET /api/v1/cases/{case_id}/entity-candidates` endpoints -- the same
endpoints, same authorization (`CaseAction.REVIEW_DECIDE`), same
`EntityRepository.record_decision`/integrity-audit path a genuine human
reviewer's client would use. It is invoked, opt-in and off by default, via
`generate_entity_resolution_truth.py --simulate-reviews-for-evaluation`.

**Not a production feature.** This tool adds no endpoint, no new code
path, and no import anywhere in `app/`. It lives entirely in the sibling
synthetic-data repository and speaks to TraceX only as an external HTTP
client would. `entity_service.py`'s real resolution-review logic,
`retrieval.py`'s `identifier_star_edges` (ADR-027), and `scoring.py`
(frozen, release-freeze-gated) are all untouched by this change --
confirmed no file under `app/` was modified for this ADR.

## Exact-match oracle, never guessed

For each real candidate, its `(left_entity_id, right_entity_id)` pair is
looked up -- as an unordered pair, the same key convention
`evaluation.py::_pair_key` itself uses -- directly against the truth
file's own `entity_pairs`:

- Exact pair present, labeled `"same"` -> submits `verified_same`.
- Exact pair present, labeled `"different"` -> submits `rejected` (the
  real `EntityReviewOutcome` enum has no `verified_different` value;
  `evaluation.py::_predicted_labels` treats any non-`verified_same`
  decision as "predicted different", so `rejected` is the real-code
  equivalent).
- Exact pair **absent** from the truth file -- even when one of the two
  entities appears in some *other* truth pair -- is left completely
  unreviewed: no decision submitted, counted separately as
  `skipped_uncovered`, never defaulted to either verdict.

Every submitted decision's `rationale` carries a fixed, greppable marker
(`SIMULATED_REVIEWER_RATIONALE`) pointing back at this ADR, so the
resulting `entity_review_decisions` rows stay honestly distinguishable
from genuine human judgment in the database/audit trail, even though they
were written through the real endpoint.

## Scope boundary: Fulcrum dev/validation only, never Nightfall

This tool is invoked only from `generate_entity_resolution_truth.py`'s own
Fulcrum-case flow; it is never wired into `generate_operation_nightfall.py`
or any Nightfall-related script. Nightfall (ADR-016's one true holdout) is
evaluated with zero simulated review -- whatever the pipeline's real,
unassisted state produces, exactly as the real system would score it.

## Consequence: Fulcrum's Precision@K/Recall@K/false-link-rate are now a
## retrieval/ranking-quality measurement, not a human-review measurement

See `docs/qa/known-limitations.md`'s Phase 5 section for the explicit,
undisguised caveat this implies: these numbers say how well candidate
generation/ranking agrees with ground truth *given a perfect oracle
reviewer*, not how a real review workflow performs. That is the honest
scope of what a synthetic, pre-authored truth file can measure in the
first place -- a real human-review-quality measurement was never possible
from this data regardless.

## Alternatives considered

- **Writing `entity_review_decisions` rows directly** (bypassing the HTTP
  endpoint). Rejected: would skip real authorization, real audit-event
  recording, and the real `record_decision` code path -- exercising less
  of the actual system than necessary, for no benefit over calling the
  endpoint the same way a genuine reviewer's client does.
- **Living inside `app/modules/graph/intelligence/`** as a TraceX-side
  module. Rejected: even with a loud docstring, physically living inside
  this codebase's shipped package tree carries more risk of future
  accidental entanglement (an import creeping in, a route referencing it)
  than living entirely in the sibling repo and touching TraceX only
  through its real public API -- "not reachable from production" is true
  by construction there, not merely asserted in a comment.
- **Applying it to Nightfall too, for consistency.** Rejected outright,
  per this ADR's own scope boundary above -- Nightfall must be scored on
  its real, unassisted state; simulating review on a holdout would defeat
  the entire point of holding it out.
