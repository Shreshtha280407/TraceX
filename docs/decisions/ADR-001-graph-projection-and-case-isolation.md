# ADR-001: Graph Projection and Case Isolation

- Status: Accepted (Phase 1 graph foundation)
- Owner: Shreshtha
- Date: 2026-09-10

## Context

`app/modules/graph/` needs to project the frozen `EvidenceRecordV1` / `ObservationV1` / `EntityV1` / `EventV1` contracts into Neo4j so later phases (entity resolution, cross-modal correlation, the hypothesis engine) have a graph to build on. Several decisions had no single obviously-correct answer and are recorded here so later contributors know what was deliberate.

## Decision 1: Case identity is always compound, never a bare domain ID

**Decision**: Every non-`Case` label (`Evidence`, `Observation`, `Entity`, `Event`) gets a Neo4j uniqueness constraint over `(case_id, <domain>_id)`. `Case.case_id` alone gets a bare uniqueness constraint, since `Case` *is* the case-isolation anchor.

**Why**: CLAUDE.md's case-isolation rule ("every retrieval, query, or aggregation must stay scoped to a single `case_id`") has to hold even when two cases legitimately reuse the same-looking value -- the same phone number as two different `Entity.canonical_label`s, or (in principle) the same UUID. A bare uniqueness constraint on `entity_id` alone would make that a hard database error the moment two cases' entities collide, or worse, force an application-level workaround. Making the compound key the actual database-level uniqueness constraint means case isolation cannot silently regress in a future change to `projection.py` -- Neo4j itself rejects a `MERGE` that would violate it. `tests/integration/graph/test_case_isolation.py` exercises this directly: the same `entity_id` value projected into two different cases produces two distinct nodes.

## Decision 2: Composite uniqueness constraints, not Node Key constraints

**Decision**: Use `CREATE CONSTRAINT ... FOR (n:Entity) REQUIRE (n.case_id, n.entity_id) IS UNIQUE` (composite uniqueness), not `IS NODE KEY`.

**Why**: `compose.yaml` pins `neo4j:5.25-community`. Node Key constraints (which add an existence guarantee on top of uniqueness) and property existence constraints are Neo4j **Enterprise**-only; composite/multi-property *uniqueness* constraints are supported in Community Edition. Choosing `IS NODE KEY` would work in development against an Enterprise image but fail outright against the Community image this project actually ships with. Verified directly: `uv run python -m app.modules.graph.schema apply` followed by `verify` against the Compose Neo4j container (see `docs/qa/test-results.md`) -- all 5 composite/bare uniqueness constraints and 6 indexes applied and verified successfully.

## Decision 3: Dependency ordering is enforced atomically, and is all-or-nothing per node

**Decision**: `project_observation` requires `evidence_id` to already exist as an `Evidence` node in the same case; `project_event` requires every `participant_entity_ids` entry to already exist as an `Entity` node in the same case. Both checks are embedded in the same Cypher statement that performs the write (a leading `MATCH`, or a `WHERE matched_count = requested_count` gate, that causes every later clause to execute zero times when the dependency is missing) rather than a separate "check, then write" round trip. When any dependency is missing, **nothing is written at all** -- not even the `Event`/`Observation` node itself -- and the caller gets a typed `DEFERRED` result naming exactly what's missing.

**Why**: CLAUDE.md is explicit that a missing referenced entity must produce "a clear safe domain error or an explicit deferred projection result" and must never "create a fake partial entity silently." Doing the check and the write in one atomic statement removes a TOCTOU race window a two-step check-then-write would have. Making it all-or-nothing per node (rather than, say, creating the `Event` node with only the participants that do exist) avoids a queryable half-formed `Event` that looks complete to a caller who doesn't know to check for missing participants -- `get_event_with_participants` should never need a caveat about whether its participant list is total or partial.

**Open question for team review**: this makes the natural projection order "entities before the events that reference them" a hard requirement, not just a convention. If a later phase's orchestrator naturally produces events and entities out of that order (e.g. streaming worker output), it will need to retry deferred events rather than treat a single pass as complete.

## Decision 4: `SUPPORTS` is schema-defined but not populated in this phase

**Decision**: `GraphRelationshipKind.SUPPORTS` and its required-property contract (`case_id`, `evidence_id`, `observation_id`, `source_locator`, `assertion_kind`) are documented in `docs/architecture/graph-taxonomy-v1.md`, but `project_event()` does not create any `SUPPORTS` edges.

**Why**: `SUPPORTS` requires knowing which specific `Observation`(s) an `Event` was built from. Neither `ObservationV1` nor `EventV1` carries that link in the frozen `V1` contracts (`ObservationV1` has no `event_id`, `EventV1.evidence_refs` cites evidence directly, not observations). Building an event from one or more observations is later-phase correlation/aggregation work. Inventing a field or an internal contract to carry that link now, without a concrete consumer, is exactly the "speculative scaffolding" CLAUDE.md rules out. The relationship is specified now so whichever later phase builds events from observations has an unambiguous schema to write into.

**Flagged for team review**: whoever builds that correlation step will need either a `V2` contract field or an explicit internal input to `project_event`/a new function to supply the observation link -- not decided here.

## Decision 5: `stable_identifiers` is the one open-ended field that is projected, as a JSON string

**Decision**: `EntityV1.stable_identifiers` (`dict[str, JsonValue]`) is serialized via `app.core.canonical.canonical_bytes` into a single `stable_identifiers_json` string property. `attributes` on `ObservationV1`/`EntityV1`/`EventV1` is never projected at all.

**Why**: Neo4j node properties cannot hold nested maps, so any dict-valued contract field has to be either flattened (fine for a small, fixed set of sub-fields like `SourceLocator`) or serialized as a string (necessary for something open-ended like `stable_identifiers`, whose keys vary per `entity_type`). `stable_identifiers` is the one open-ended field actually needed back out by graph consumers -- it's exactly what a future entity-resolution/candidate-scoring phase corroborates against (shared phone/account/device identifiers). `attributes` has no such near-term consumer and is explicitly named in CLAUDE.md as something that must not be blindly stored, so it's dropped entirely rather than serialized "just in case."

## Decision 6: No `Location` node label

**Decision**: A place is represented as an `Entity` node with `entity_type = "location"`, never a separate `Location` label.

**Why**: The task brief allows a `Location` label "only if necessary and fully justified," and explicitly prefers the `Entity`-typed representation to avoid two competing ways to represent the same kind of thing. Introducing a second representation would force every later phase (entity resolution, correlation, queries) to reconcile both instead of one. `ObservationV1.location`/`EventV1.location` (the loosely-structured value describing a place *mentioned in* that observation/event) is stored as flattened properties on the `Observation`/`Event` node itself -- it's evidence about a place, not a resolved graph entity.
