# ADR-015: Phase 6 Part 5 review-decision and hypothesis workflow design

Status: accepted. Owner: Shreshtha. Date: 2026-09-15.

## Context

Phase 6 Parts 1-4 built the tamper-evident integrity foundation and left
`IntegrityEventKind.REVIEW_DECISION`/`HYPOTHESIS_ACTION` as forward-
compatible-only enum values with no producer, and Phase 5 left `Correlation`
candidates with no human decision workflow layered on top. This task
completes both, plus the final live release gate.

## Decisions

**1. Review status is a read-time projection, never a stored mutation.**
Nipun's `correlation_candidate_links.status` column is the frozen output of
Phase 5 scoring rules and is never written to by this task. A new,
additive `candidate_review_decisions` table records the human decision
separately; the caller-visible `needs_review`/`accepted_by_reviewer`/
`rejected_by_reviewer` status is computed at read time from "does a
decision row exist yet." Alternative considered: adding
`ACCEPTED`/`REJECTED_BY_REVIEWER` values to `PropositionStatus` and
updating the candidate row directly -- rejected because it would mix a
frozen Phase 5 scoring output with the introduces churn into a table two
already-shipped systems (analytics, motif detection) read the `status` of.

**2. `HYPOTHESIS_PROPOSE` is new; hypothesis review reuses `REVIEW_DECIDE`.**
Creating a hypothesis is closer to `EVIDENCE_WRITE`-style active
caseworking than to reviewing one, so it gets its own action, granted to
the same roles as `EVIDENCE_WRITE`. Deciding a hypothesis is the same
underlying human act as deciding a candidate -- reusing `REVIEW_DECIDE`
avoids a second near-identical permission and keeps "who can make an
authorized review decision" answerable in one place.

**3. Hypothesis lifecycle is two-state (`needs_review` ->
`accepted_by_reviewer`/`rejected_by_reviewer`), not the suggested
four-state one.** The task brief listed `draft -> proposed -> needs_review
-> accepted/rejected` as a *suggestion*, and specified exactly two write
routes (`POST /hypotheses`, `POST /hypotheses/{id}/review`). Adding
`draft`/`proposed` states with no dedicated transition route would mean
either overloading the create or review endpoint with an implicit state
machine, or leaving states unreachable. A hypothesis is therefore
`needs_review` from the moment it's durably created. A team member wanting
a private drafting UI should hold the statement client-side until ready to
submit -- this is flagged in `docs/qa/known-limitations.md` for review.

**4. Hypothesis IDs are deterministic and content-addressed, not
identity-addressed.** `hypothesis_id` is derived from
`(case_id, created_by, statement_commitment, rationale_commitment,
sorted(observation_ids), sorted(candidate_ids))`. This makes an exact
repeat idempotent (same ID, replays) while a genuinely different statement
or reference set is simply a new hypothesis -- there is no "one hypothesis
per author" or "one hypothesis per candidate" constraint to protect, unlike
a candidate review decision, which is capped at exactly one per candidate.

**5. `Hypothesis` is a new Neo4j node label; candidate review reuses
`Correlation`.** A review decision only ever adds properties to an
already-projected `Correlation` node (via `MATCH`, never `MERGE`, so a
not-yet-projected correlation is a safe no-op). A hypothesis is a
genuinely new kind of reviewable claim with no existing node to attach to,
so it gets its own label, its own `(case_id, hypothesis_id)` uniqueness
constraint, and reuses the existing `SUPPORTED_BY_OBSERVATION` relationship
kind (rather than inventing a hypothesis-specific one) since the
provenance semantic -- "this claim is supported by this observation" -- is
identical for both subject types. One new relationship kind,
`REFERENCES_CANDIDATE`, links a hypothesis to a candidate it cites, since
no existing relationship expressed "a review-only claim depends on
another review-only claim."

**6. Neo4j projection for this task's two write paths is best-effort and
inline, not outbox-backed.** Nipun's `graph_update_events` durable outbox
exists specifically for correlation projection; duplicating it for review
decisions and hypotheses would mean either extending a table/queue Nipun
owns (against the "prefer additive, ask before extending" module-boundary
norm) or building a second, parallel outbox mechanism (explicitly a
non-goal: "no new Kafka/Celery"). The chosen design instead treats the
Neo4j write exactly like the existing `review_decision`/`correlation_
completed` integrity-event write: best-effort, logged on failure, never
blocking or rolling back the already-committed PostgreSQL decision. The
consequence -- no automatic retry queue for a missed projection
specifically -- is accepted and documented rather than engineered around,
given the proportionate scope of this task; a future phase could extend
`graph_update_events`-style replay to cover this instead.

**7. Rationale text follows the exact discipline every other Phase 6
producer already established.** Raw `rationale`/`statement` text is
persisted only in the protected, case-scoped primary tables
(`candidate_review_decisions`, `hypotheses`, `hypothesis_actions`),
readable back only through this task's own protected API. The integrity
event, Neo4j projection, and reconciliation replay all carry
`*_commitment_sha256` only, mirroring
`docs/architecture/phase-6-integrity.md`'s "Why raw content never reaches
storage" section for structured/modality provenance.

## Consequences

Nipun's `correlation_candidate_links`/`correlation_records` schema and
semantics are completely unchanged. Reviewing a hypothesis and reviewing a
candidate share one permission, so a future distinct "hypothesis reviewer"
role (separate from a "candidate reviewer" role) would require a new
`CaseAction`, not a policy change to an existing one. The lack of an
outbox-backed Neo4j retry path means an operator who wants to guarantee
every accepted review/hypothesis eventually reaches Neo4j needs either a
periodic manual replay tool (not built in this task) or accepts that a
Neo4j outage at decision time can leave a durable, correctly-recorded
decision without its graph projection until someone notices and re-runs
the relevant write (idempotent, so safe to retry) once Neo4j recovers.
