# ADR-031: Hypothesis citation of a WP-2 entity-resolution candidate

Status: **accepted**. Gap-Closure follow-up.

## Context: a timing-accident gap, confirmed by reading both original ADRs

Investigated (a prior discovery-only session task, not this one) whether
`entity_resolution_candidates` (WP-2) was ever meant to feed `candidate_
links_table` (Phase 5's correlation pipeline) -- finding: no, they are
deliberately separate, different semantic claims (`CandidateLinkRecord`:
observation-to-observation, "these may describe a related event";
`EntityResolutionCandidateRecord`: entity-to-entity, "these may be the
same identity"), built from the same `retrieval.retrieve_candidates`
cascade but kept architecturally decoupled on purpose (ADR-020 Decision 2:
"no new matching logic was written... `graph.intelligence.scoring` is
never imported here"). That separation is correct and untouched by this
ADR.

The real gap: `HypothesisCreateSubmission.supporting_candidate_ids` could
only ever cite a Phase 5 correlation candidate, never a WP-2 entity-
resolution candidate -- live-confirmed this session by attempting exactly
that citation against a real, freshly-ingested Fulcrum case with 36 real
WP-2 candidates: `POST /hypotheses` returned `422 Unprocessable Entity`,
traced to `hypothesis_repository.create_hypothesis` verifying `supporting_
candidate_ids` against `candidate_links_table` only.

**Root cause, confirmed by date**: `ADR-015` ("Phase 6 Part 5 review-
decision and hypothesis workflow design"), dated **2026-09-15**, designed
the entire hypothesis citation mechanism -- at that date, `candidate_
links_table` was the *only* candidate concept in the codebase.
`ADR-020` ("Entity layer and resolution review", Gap-Closure WP-2, a later
follow-up) introduced `entity_resolution_candidates` as a second, genuinely
new candidate table. `phase-5-graph-intelligence.md` itself lists "entity
resolution... hypotheses" together in one "deferred" sentence -- both were
explicitly out of Phase 5's own scope, built later as two separate,
sequential Gap-Closure items that never revisited each other. Nobody ever
came back to extend `HypothesisCreateSubmission`'s citation validation once
a second candidate table existed. A sequencing accident, not a deliberate
exclusion.

## Decision: a second, parallel citation field -- never merge the two tables

`HypothesisCreateSubmission` gains `supporting_entity_resolution_candidate_
ids: tuple[UUID, ...] = ()`, validated (uniqueness, at-least-one-reference)
identically to the existing `supporting_candidate_ids`.
`HypothesisRepository.create_hypothesis` gains a matching, optional
parameter, verified with the same case-scoped `count()`/`in_()` shape
already used twice in that function -- this time against `entity_
repository.entity_resolution_candidates_table`. `HypothesisRecord`/
`safe_metadata()` carry the new list through unchanged in shape. The
deterministic `hypothesis_id` hash (ADR-015 Decision 4) folds in
`sorted(supporting_entity_resolution_candidate_ids)` as a fifth hash input,
alongside the existing four -- an otherwise-identical hypothesis with vs.
without this field gets two different, both-idempotent IDs.

A hypothesis can now honestly cite either kind of candidate, or both at
once, each clearly labeled by which field holds it. `candidate_links_
table`, `entity_resolution_candidates_table`, `pipeline.py`, and
`scoring.py` are all completely untouched -- this ADR only ever adds a
second read-and-reference path on top of two already-existing, already-
separate tables.

## Migration: additive, backfilled

`migrations/versions/7a8b9c0d1e2f_hypothesis_entity_resolution_citation.py`
adds `hypotheses.supporting_entity_resolution_candidate_ids` (JSONB, `NOT
NULL`, `server_default='[]'`) -- the `server_default` backfills the real
hypothesis rows already present in this environment (18, confirmed via
direct query before/after the migration) with an empty array; every future
insert supplies an explicit value, matching `supporting_candidate_ids`'s
own contract exactly. `downgrade()` drops the column.

## Neo4j projection: explicitly skipped, not silently

WP-2 has zero Neo4j footprint by design (confirmed: `taxonomy.py`'s
`REFERENCES_CANDIDATE` relationship is typed `(CASE, CORRELATION)` --
it can only ever point at a Phase 5 `Correlation` node; nothing in
`entity_service.py`/`entity_repository.py` ever touches `graph_
repository`). `review_service._best_effort_project_hypothesis`'s
existing candidate-projection loop stays scoped to `supporting_candidate_
ids` only, with an explicit comment explaining why `supporting_entity_
resolution_candidate_ids` is never iterated there -- inventing a new
`Entity`/`EntityResolutionCandidate` Neo4j node kind just to serve this
one citation path is out of scope; a hypothesis citing only an entity-
resolution candidate (no observations, no correlation candidates) is
still durably, correctly recorded in PostgreSQL, just without a Neo4j
graph node for the citation edge -- the same documented, accepted
"best-effort, no outbox" limitation ADR-015 Decision 6 already established
for the pre-existing Neo4j write path, extended to one more case, not a
new precedent.

## Scope boundary

`pipeline.py`, `scoring.py`, `candidate_links_table`, the frozen
correlation pipeline, and `release-freeze.v1.json`: untouched. Neither
candidate table is merged, extended, or renamed.

## Alternatives considered

- **Merge `entity_resolution_candidates` into `candidate_links_table`**
  (or vice versa) so one citation field serves both. Rejected: this is
  exactly the conflation ADR-020 Decision 2 went out of its way to avoid
  -- an identity claim and an event/relationship claim are different
  things, and Phase 5's table is Gate-C/`scoring.py`-adjacent in a way
  WP-2's deliberately is not.
- **A single generic `supporting_claim_ids` field with a type
  discriminator** instead of two named fields. Rejected: bigger surface
  change than the actual gap needs: two clearly-named fields, mirroring
  the codebase's own established "each kind of reference is its own
  field, verified against its own table" pattern (`supporting_observation_
  ids` vs. `supporting_candidate_ids` already worked this way before this
  ADR), not a new abstraction.
- **Inventing a new Neo4j node kind for entity-resolution candidates** to
  give this citation type full parity with the correlation-candidate
  citation path. Rejected as disproportionate: WP-2's PostgreSQL-only
  boundary is itself a deliberate, repeatedly-reconfirmed design choice
  (ADR-020, ADR-027, ADR-029, ADR-030 all note it); this ADR respects that
  boundary rather than being the first thing to cross it.
