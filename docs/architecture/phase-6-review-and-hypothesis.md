# Phase 6 Part 5: Reviewable Intelligence Integration and Final Release Gate

Owner: Shreshtha. Status: in progress until the mandatory Part E gates in
`docs/qa/test-results.md`'s dated entry for this task all pass; see
`docs/progress/mvp-progress.md` for the authoritative completion line.

This closes out Phase 6: it completes the human-review and
evidence-backed-hypothesis workflow the earlier Phase 6 parts left as
forward-compatible-only (`IntegrityEventKind.REVIEW_DECISION`/
`HYPOTHESIS_ACTION` existed in the enum with no producer), wires that
workflow into the existing integrity, graph, and authorization seams, and
runs the previously-deferred live Docker/PostgreSQL/Neo4j release gate.

## Final objective, as implemented

```text
reviewable correlation candidate (Nipun's Phase 5 scoring output)
  -> authorized human decision (this task)
  -> evidence references and audit record (already-persisted candidate + new decision row)
  -> safe integrity event (review_decision)

human-created evidence-backed hypothesis (this task, new)
  -> case-scoped source references (re-verified against canonical rows)
  -> reviewable graph projection (new Hypothesis node)
  -> safe integrity event (hypothesis_action, one per creation/review)
```

No automatic identity merge, guilt conclusion, relationship assertion, or
hypothesis acceptance exists anywhere in this task's code.

## Module layout

New files, all in `app/modules/graph/` (the existing candidate/hypothesis
read surface's home -- this task extends it rather than opening a parallel
module):

| File | Responsibility |
|---|---|
| `review_models.py` | `CandidateReviewOutcome`, `CandidateReviewDecisionRecord`/`Submission`, `candidate_review_view` (the read-time `needs_review`/`accepted_by_reviewer`/`rejected_by_reviewer` projection). |
| `review_repository.py` | `candidate_review_decisions` table (append-only), idempotent-record/conflict-reject pattern. |
| `hypothesis_models.py` | `HypothesisStatus`, `HypothesisCreateSubmission`/`ReviewSubmission`, `HypothesisRecord` (mutable), `HypothesisActionRecord` (append-only audit trail). |
| `hypothesis_repository.py` | `hypotheses` (mutable) + `hypothesis_actions` (append-only) tables; re-verifies every cited observation/candidate against its own canonical table before writing. |
| `review_service.py` | Orchestration only: durable write -> best-effort integrity event -> best-effort Neo4j projection. `api.py` stays thin. |
| `review_projection.py` | Safe, provenance-gated Neo4j writes for review decisions and hypotheses. |

Modified, additively: `app/modules/graph/api.py` (7 new routes),
`app/modules/graph/dependencies.py` (repository providers),
`app/modules/graph/integration_repository.py` (`get_candidate_link`, a
pure read addition), `app/modules/graph/models.py` (`GraphNodeKind
.HYPOTHESIS`, `GraphRelationshipKind.REFERENCES_CANDIDATE`),
`app/modules/graph/schema.py` (one constraint + one index for
`Hypothesis`), `app/modules/access_control/models.py`
(`CaseAction.HYPOTHESIS_PROPOSE` + role grants),
`app/modules/access_control/dependencies.py`
(`require_hypothesis_propose`), `app/modules/integrity/reconciliation.py`
(two new scan branches).

## Part A: candidate review decisions

**Effective status is computed, never stored as a mutation.** Nipun's
`correlation_candidate_links.status` (`PropositionStatus`) is never
written to by this task. A `candidate_review_decisions` row is a *separate*
additive fact; `candidate_review_view()` computes `needs_review` when no
decision row exists, else the decision's own outcome. This means the task
brief's three-state model (`needs_review` / `accepted_by_reviewer` /
`rejected_by_reviewer`) is a read-time projection, not a rewritten column
-- consistent with "never silently merge/overwrite a scored candidate."

**One decision per candidate, ever.** `CandidateReviewDecisionRecord
.idempotency_key` is `candidate-review:<case_id>:<candidate_link_id>` --
it never varies by outcome or reviewer. An exact repeat (same decision,
reviewer, rationale) replays the existing row; anything else under the
same key raises `ReviewConflictError` (rendered as `409`), never
overwriting. `candidate_review_decisions` is protected by the same
PostgreSQL append-only trigger function
(`tracex_reject_integrity_record_mutation`) Nipun's integrity tables use.

**Authorization runs before lookup.** `POST
.../candidates/{candidate_id}/review` depends on `require_review_decision`
(`CaseAction.REVIEW_DECIDE`, already defined and role-provisioned by
Aditya's Phase 2 access-control matrix, unmodified here) -- the same
generic 401/403 every other case-scoped route returns, before
`review_service.py` ever looks up the candidate. A missing candidate is a
plain `404` (checked after authorization, matching every existing
case-scoped detail route), never an existence leak.

**Rationale is protected-database-only.** `CandidateReviewDecisionSubmission
.rationale` is accepted and stored raw in `candidate_review_decisions`
(readable back only through this same protected API). Every other
surface -- the `review_decision` integrity event
(`CandidateReviewDecisionRecord.to_integrity_submission`), the Neo4j
projection (`project_candidate_review_decision`), and reconciliation --
carries only `rationale_commitment_sha256` (a SHA-256 of `{"rationale":
value}`), or nothing when no rationale was given.

## Part B: evidence-backed hypotheses

**Simplified two-state lifecycle, not the suggested four-state one.** The
task brief's *suggested* lifecycle was `draft -> proposed -> needs_review
-> accepted_by_reviewer/rejected_by_reviewer`. This implementation only
ever creates a hypothesis directly at `needs_review` and moves it to
`accepted_by_reviewer`/`rejected_by_reviewer` -- there is no `draft`/
`proposed` state or transition route, because the required minimal route
surface (`POST /hypotheses`, `POST /hypotheses/{id}/review`) has no
dedicated endpoint for a draft-to-proposed transition. This is a
deliberate Phase 6 Part 5 scoping decision (see
`docs/qa/known-limitations.md`), not an oversight -- a caller who wants a
private draft process should keep the statement client-side until ready to
submit.

**Every reference is re-verified, never trusted.** `HypothesisRepository
.create_hypothesis` re-reads `supporting_observation_ids` against
`worker_observations` and `supporting_candidate_ids` against
`correlation_candidate_links`, both scoped to `case_id` -- exactly Nipun's
`GraphCorrelationIntegrationRepository.submit()` precedent for observation
references. A missing or cross-case reference raises
`HypothesisValidationError` (`422`) before any write.

**Deterministic, content-addressed hypothesis ID.** `hypothesis_id =
deterministic_uuid("phase_6_hypothesis", case_id, created_by,
statement_commitment, rationale_commitment, sorted(observation_ids),
sorted(candidate_ids))`. An exact repeat of the same author/content/
references maps to the same ID and replays (no duplicate row, no duplicate
`hypothesis_action` integrity event); a genuinely different statement or
reference set is a new hypothesis, never a conflict -- unlike a review
decision, proposing a hypothesis has no "one per X" constraint to protect.

**One review action per hypothesis, same conflict discipline as
candidates.** `HypothesisRecord.review_idempotency_key` is
`hypothesis-action:<case_id>:<hypothesis_id>:reviewed` -- fixed regardless
of outcome, so a second review attempt with a different decision/reviewer/
rationale raises `HypothesisConflictError` (`409`) rather than overwriting.

**`hypothesis_actions` is the append-only audit trail; `hypotheses` is
not.** Mirrors the existing split between mutable `correlation_records`
and the append-only `integrity_events` log: `hypotheses.status`/
`decided_at`/`decided_by`/`updated_at` change exactly once (on the single
allowed review decision), so that table cannot be append-only. Every
creation or review decision additionally writes one immutable
`hypothesis_actions` row (protected by the same PostgreSQL trigger), which
is also the exact source `HypothesisActionRecord.to_integrity_submission`
and the reconciliation scan build the `hypothesis_action` integrity leaf
from.

**Permissions: one new, one reused.** `CaseAction.HYPOTHESIS_PROPOSE` is a
new, additive action granted to the same roles as `EVIDENCE_WRITE`
(`CASE_OWNER`, `CASE_MANAGER`, `INVESTIGATOR`) -- the active-caseworker
roles. Reviewing a hypothesis reuses the existing `CaseAction.REVIEW_DECIDE`
(granted to `CASE_OWNER`, `CASE_MANAGER`, `REVIEWER`) -- one "authorized
human review decision" concept applied to two subject types, matching the
task brief's "existing/additive review permission" wording for Part A and
"existing/additive hypothesis permissions" (plural: one new, one reused)
for Part B.

## Part C: safe graph projection

**Reused `Correlation`, new `Hypothesis`.** A review decision never needs a
new node label: `project_candidate_review_decision` `MATCH`es (never
`MERGE`s) the existing `Correlation` node for `(case_id, projection_key)`
and sets `review_status`/`reviewed_at`/
`reviewer_rationale_commitment_sha256`/`candidate_review_decision_id`. Using
`MATCH` instead of `MERGE` is the whole safety property: if the correlation
itself was never projected (its own Evidence -> Observation provenance
wasn't available when Nipun's handler ran), this is a safe, silent no-op
rather than a bare, provenance-less Correlation node.

A hypothesis is a genuinely new kind of node, so `GraphNodeKind.HYPOTHESIS`
is a new, additive label with its own composite `(case_id, hypothesis_id)`
uniqueness constraint (`schema.py`, following the exact pattern every other
label uses). `project_hypothesis` uses the identical provenance gate
`project_correlation_context` does: it `UNWIND`s the caller-resolved
`(evidence_id, observation_id)` pairs, requires every one of them to match
a real `Evidence-[:YIELDED_OBSERVATION]->Observation` chain in this case,
and only then `MERGE`s the `Hypothesis` node and its
`SUPPORTED_BY_OBSERVATION` edges -- reusing that existing relationship
kind rather than inventing a hypothesis-specific one, since "this node is
supported by this observation" means the same thing for both subject
types. A hypothesis citing a reviewed candidate additionally gets a new
`REFERENCES_CANDIDATE` edge to the `Correlation` node, written via a
second `MATCH`-both-sides call (`project_hypothesis_candidate_reference`)
-- never fabricating either side.

**`kind: "hypothesis"` makes the fact/candidate/hypothesis distinction
queryable on the node itself**, mirroring `Correlation`'s own
`candidate_only: True` marker -- a `Hypothesis` node can never be mistaken
for a projected `Observation`/`Event` fact or a `Correlation` candidate by
a query that doesn't already know the label.

**No raw content reaches Neo4j.** Every projection property is an ID, an
enum value, a timestamp, or a `*_commitment_sha256` -- never
`statement`/`rationale` text (see `hypothesis_models.py::safe_metadata`,
which `_hypothesis_properties` in `review_projection.py` is built from).

**Best-effort, not outbox-backed, and that is a documented limitation.**
Unlike Nipun's `graph_update_events` durable outbox for correlations, a
review decision's or hypothesis's Neo4j projection runs synchronously,
inline, immediately after the durable PostgreSQL write, wrapped in the
same "log and continue" contract `graph.intelligence.pipeline
._record_correlation_integrity_event_safely` already established for
integrity events. If Neo4j is down at that moment, the durable decision/
hypothesis still succeeds (never rolled back) but there is currently no
automatic retry queue for the projection specifically -- see
`docs/qa/known-limitations.md`. Building a second outbox exclusively for
this would duplicate Nipun's existing mechanism and was judged out of this
task's proportionate scope (the non-goals list explicitly excludes new
Kafka/Celery-style infrastructure); a future phase could extend
`graph_update_events`-style replay to cover this instead of inventing a
parallel one.

## Part D: integrity and reconciliation closure

Both new leaf kinds go through the **existing, unmodified**
`IntegrityService.record_integrity_event` facade -- exactly as
`docs/architecture/phase-6-integrity.md`'s "Handoff to later branches"
section instructed. No new integrity schema, table, or Merkle/signing code
was added; `IntegrityEventKind.REVIEW_DECISION`/`HYPOTHESIS_ACTION` already
existed as forward-compatible enum values before this task.

`app/modules/integrity/reconciliation.py::IntegrityReconciliationService
._durable_submissions` gained two more bounded scan branches (after the
existing evidence/correlation/structured/modality ones, sharing the same
`limit`/`remaining` budget): `candidate_review_decisions` rows replay
directly via `CandidateReviewDecisionRecord.to_integrity_submission()`;
`hypothesis_actions` rows are joined against their parent `hypotheses` row
(re-hydrated the same way `hypothesis_repository.py` does) and replay via
`HypothesisActionRecord.to_integrity_submission(hypothesis=...)`. Both
follow the established rule: only an already-persisted safe row is ever
replayed, never a raw source payload.

## Non-goals confirmed absent

No automatic entity merge or identity resolution, no face/biometric/plate
work, no guilt/conclusion scoring, no ML training, no change to the frozen
Phase 5 rules baseline (`app/modules/graph/intelligence/scoring.py` is
untouched), no blockchain anchoring, no external KMS/HSM, no production/
private-data evaluation, no GPU benchmarking, no LAN deployment work, no
frontend.

## Known limitations

See `docs/qa/known-limitations.md`'s Phase 6 Part 5 section for the full
list: the simplified two-state hypothesis lifecycle, the non-outbox-backed
Neo4j projection for this task's two write paths specifically, and the
scope of what "review" means (a human accept/reject judgement on an
already-scored candidate or an already-authored hypothesis -- never a
re-scoring, re-ranking, or entity-resolution action).
