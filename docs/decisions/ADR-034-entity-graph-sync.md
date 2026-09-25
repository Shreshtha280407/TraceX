# ADR-034: Wiring WP-2 entities and candidates into Neo4j

Status: **accepted**. Gap-Closure follow-up.

## Context: two fully-correct, fully-tested functions, zero real callers

`project_entity` (`app/modules/graph/projection.py`, ADR-020 Decision 2) and
`project_entity_resolution_candidate` (`app/modules/graph/entity_projection.py`,
ADR-021 Decision 2) MERGE a real `EntityV1` into an `:Entity` node and MATCH
two already-projected `Entity` nodes to MERGE a `POSSIBLY_SAME_AS` (plus
`CONTRADICTED_BY`, when applicable) edge between them. Both were implemented
correctly and unit-tested from day one, and both were exercised end-to-end
against a live Neo4j instance in `tests/integration/graph/
test_projection_and_queries.py` -- using synthetic fixture data, never real
Postgres rows.

Nothing in the running system ever called either function for real data.
`entity_service.create_entities_for_case`/`generate_entity_resolution_
candidates` (WP-2) write real entities and candidates to Postgres only, by
their own documented design (`entity_service.py`'s module docstring, ADR-020
Decision 2: "no new matching logic... `graph.intelligence.scoring` is never
imported here" -- and, less explicitly but just as deliberately, no Neo4j
import either). `docs/qa/known-limitations.md` had already named this twice
before it was ever fixed for the *creation* side ("entity creation and
candidate generation now have a live caller", closed by `intelligence_
worker.py --resolve-entities`) -- but that fix only gave WP-2 a *Postgres*
caller. The same "built and tested in isolation, nothing real ever calls it"
situation still applied one layer further in, to the Neo4j projection
functions themselves.

**Found live, not by this repo's own test suite** (same discovery method as
every prior instance of this exact category of gap in this codebase): Phase
3 frontend verification against a real case (`case-fulcrum-dev-07863ba4`,
72 real Postgres entities, 36 real Postgres candidates, 700 "succeeded"
`graph_projection_jobs` rows) rendered a completely empty Investigation
Workspace graph. Direct Neo4j inspection confirmed zero `Entity`- or
`Event`-labeled nodes existed anywhere in the entire dev database, for any
case -- the "succeeded" projection jobs were all `project_evidence`/
`project_observation`/`project_specialized_mapping` work (a separate,
already-wired outbox scoped to observation-derived projection only, per
`docs/architecture/graph-projection.md`), never entity or candidate
projection.

## Decision: a small composition module, not a new outbox

New module `app/modules/graph/entity_graph_sync.py` provides three
functions -- `project_case_entities`, `project_case_candidates`, and
`sync_case_entities_and_candidates` -- that do nothing but call the two
already-correct projection functions in the right order over real Postgres
data. No new Cypher, no change to `projection.py`/`entity_projection.py`,
no new database table.

**Entities before candidates, always.** `project_entity_resolution_
candidate` only `MATCH`es (never `MERGE`s) its two entities -- calling it
first would defer every candidate, every time. `sync_case_entities_and_
candidates` enforces this ordering so no caller can get it backwards.

**A candidate's effective status is computed the same way the review
endpoint displays it.** `project_case_candidates` calls `entity_repository.
list_decisions` + `entity_resolution_review_view` per candidate -- the exact
function `GET /cases/{id}/entity-candidates` already uses -- so the Neo4j
edge's `effective_status` property and the review-listing view can never
disagree.

**Two real callers, matching two different real needs:**

1. `intelligence_worker.resolve_entities_once` (`--resolve-entities`,
   batch). After its existing Postgres work, fetches the case's full,
   current entity and candidate lists (not just what this pass created) and
   syncs all of them. This means re-running `--resolve-entities` against a
   case processed before this ADR also backfills it -- no separate
   backfill script needed, matching this repo's own precedent of reusing an
   idempotent CLI for backfill (`ResolveEntitiesSummary` gains
   `projected_entity_count`/`applied_candidate_count`/
   `deferred_candidate_count` so an operator can see the real counts,
   including a normal, non-zero `deferred_candidate_count` when a candidate
   references an entity this pass didn't include).
2. `entity_api.submit_entity_resolution_review` (interactive, per-decision).
   After a decision durably commits to Postgres, best-effort re-projects
   that one candidate's edge with its new effective status --
   `_project_decision_to_graph_safely` mirrors `_record_integrity_event_
   safely`'s exact contract: Neo4j being unreachable, or the candidate's
   entities not yet being in Neo4j (a normal `DEFERRED`, not an error),
   never blocks or reverses an already-committed decision. This mirrors a
   pre-existing precedent in this codebase: `review_service.
   _best_effort_project_hypothesis` already re-projects a hypothesis's
   correlation-candidate citations the same best-effort way (see ADR-031).

## `project_event`: still out of scope, and why that's correct here

`EventV1` is not fabricated to give this fix broader visible impact. No
service anywhere in this codebase constructs a real `EventV1` yet (see
`docs/architecture/graph-taxonomy-v1.md`) -- `TemporalEvent` (the
observation-mapping-derived label the existing outbox already projects) is
a deliberately distinct schema, not an `Event` with real `HAS_PARTICIPANT`
edges. Wiring `project_event` here would mean inventing what an "event"
is for this system -- a design decision no ADR has made -- not connecting
two already-designed pieces. Left unwired, honestly, matching this file's
own standard for every other genuinely-unbuilt capability.

## Live verification

Rebuilt the `api`/`intelligence-worker` images and re-ran `--resolve-
entities` against `case-fulcrum-dev-07863ba4` (real, previously-processed
case: 72 Postgres entities). Output: `entities: 72 candidates: 66
projected_entities: 72 applied_candidates: 66 deferred_candidates: 0`.
Direct Neo4j query confirmed 72 real `:Entity` nodes and 66 real
`POSSIBLY_SAME_AS` edges for this case -- zero before this fix, for any
case, ever. Reopened the Investigation Workspace in a real browser: the
graph now renders all 72 real entities (each its own singleton cluster --
see the caveat below), and `SYN-PER-FULD-A03` correctly shows "This entity
has an unresolved candidate bridge -- review it" in its detail panel, a
real `POSSIBLY_SAME_AS` edge with no shared cluster to make it trivially
true.

**A second, genuinely separate bug surfaced by this being the first time
real relationship data ever existed**: 18 real entity pairs in this case
have *two* distinct `entity_resolution_candidates` rows each (different
`config_version`s from separate retrieval-cascade runs), so Neo4j
legitimately holds two distinct `POSSIBLY_SAME_AS` relationships between
the same two `Entity` nodes. `GraphSnapshotRelationshipView` (`schemas.py`)
had no per-relationship identifier -- only `(kind, from_id, to_id)`, which
collided for these 18 pairs -- and the frontend's `NetworkGraph` keyed its
rendered `<line>` elements the same way, producing real React "duplicate
key" warnings the instant real data existed to trigger them. Fixed by
adding `relationship_id: UUID | None` to `GraphSnapshotRelationshipView`
(populated from `rel.entity_resolution_candidate_id` for `POSSIBLY_SAME_AS`/
`CONTRADICTED_BY`, `null` for `HAS_PARTICIPANT`, which has no such property)
and threading it into the frontend's edge key. Verified live: re-clicked
the exact entity that previously produced 18 console warnings -- zero
console errors after the fix.

## Scope boundary and an honestly-named remaining gap

`HAS_PARTICIPANT` edges (which `Event` nodes carry to their participant
`Entity` nodes) still do not exist anywhere, because `Event` itself still
doesn't -- see "`project_event`: still out of scope" above. This means the
graph snapshot's cluster/bridge detection (client-side Union-Find over
`HAS_PARTICIPANT`, in the frontend's `graphLayout.ts`) has no
`HAS_PARTICIPANT` edges to cluster on for any case yet, even after this
ADR: every entity now renders as its own singleton cluster, and a
`POSSIBLY_SAME_AS` edge is trivially "cross-cluster" by that definition.
The frontend's bridge/cluster concept becomes real and non-trivial only
once a future phase gives `project_event` a real `EventV1` source -- this
ADR closes the entity/candidate half of the gap, not that one.

`entity_service.py`/`entity_repository.py` remain untouched and Postgres-
only, exactly as their own module docstrings require -- this ADR's new
module composes them from outside, never edits their internal scope.
