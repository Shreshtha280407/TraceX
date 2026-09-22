# ADR-021: Graph taxonomy alignment (Gap-Closure WP-3)

Status: **accepted**. Closes gap register G10 (typed labels/relationships
not enforced); documents G11 (pgvector holds hashed-token vectors, not
semantic embeddings) and G12 (fixed 900s hot window, not Redis-backed) as
deliberate, unchanged behavior.

## Context

`docs/architecture/graph-taxonomy-v1.md` documents a generic node-kind
model (`Entity`, `Event`, ...) with free-string `entity_type`/`event_type`
properties rather than a typed label per taxonomy value, and
`GraphRelationshipKind` lacked the plan's `CONTRADICTED_BY`/
`POSSIBLY_SAME_AS`/`CANDIDATE_ASSOCIATION`/review-overlay vocabulary. No
validator enforced which node kinds a relationship kind may legally
connect.

## Decision 1: keep generic node kinds; add a validator, not new labels

Renaming `Entity`/`Event` into per-taxonomy-value Neo4j labels (e.g. a
`Phone` label instead of `Entity {entity_type: "phone"}`) would touch every
existing node/edge identity key across `projection.py`, `queries.py`, and
every live query in the codebase — an explicit blocker this pack's own
rules name ("any projection change that alters existing node/edge identity
keys"). Instead, `app/modules/graph/taxonomy.py` adds a **validator**,
additive and separate from the frozen contracts:

- **Relationship node-kind combinations are strictly enforced.**
  `ALLOWED_RELATIONSHIP_NODE_KINDS` names the exact `(from_kind, to_kind)`
  pair for every `GraphRelationshipKind`, existing and new;
  `validate_relationship_combination` returns `False` for anything else.
  `GraphNodeKind`/`GraphRelationshipKind` are this module's own closed
  enums, so there is no legitimate unknown combination to tolerate here.
- **`entity_type`/`event_type` recommendations are advisory only.**
  `EntityV1`'s own docstring is explicit that this is deliberate ("the
  detailed entity/relationship taxonomy is owned by a later phase" —
  locking it to an enum "would force every contributor to edit this frozen
  contract"). `RECOMMENDED_ENTITY_TYPES` is sourced from the only real,
  evidence-backed vocabulary that exists today —
  `graph.intelligence.retrieval._IDENTIFIER_TYPES` (`phone`, `email`,
  `vehicle_registration`, `device_id`, `account`, `platform_handle`) plus
  `entity_service._entity_type_and_label`'s own `alias`/`unknown`
  fallbacks. `is_recommended_entity_type` exists only so a caller can log
  a safe warning for an unrecognized value — the plan's own "unknown types
  route to the documented `unknown` escape hatch with a safe warning" — it
  never rejects a valid `EntityV1`. `RECOMMENDED_EVENT_TYPES` is left
  **empty**, deliberately: no `EventV1` is instantiated anywhere in this
  codebase yet, so there is no real vocabulary to source one from without
  inventing it.

## Decision 2: three new relationship kinds, each backed by a real record

`GraphRelationshipKind` gains `POSSIBLY_SAME_AS`, `CANDIDATE_ASSOCIATION`,
and `CONTRADICTED_BY` (additive enum values). Per this pack's own rule
("no fabricated edges"), each is added only where a real durable record
already backs it:

- **`POSSIBLY_SAME_AS`** (`Entity -> Entity`): WP-2's
  `entity_resolution_candidates`. Projected by the new, additive
  `app/modules/graph/entity_projection.py::project_entity_resolution_candidate`
  — `MATCH`es (never `MERGE`s) both entities first, so it can never create
  an `Entity` node as a side effect, and `DEFER`s (never crashes or
  fabricates an edge) if either entity hasn't been projected yet, matching
  `project_event`'s existing "all dependencies must exist first" rule. The
  edge carries `reasons`/`identifier_types`/`effective_status`/
  `config_version` — the same review-overlay information
  `entity_resolution_review_view` already computes at read time, now also
  visible directly on the graph edge.
- **`CONTRADICTED_BY`** (`Entity -> Entity`): the same candidate record's
  own `contradiction_reasons` field, projected as a second edge only when
  that tuple is non-empty.
- **`CANDIDATE_ASSOCIATION`** (`Observation -> Observation`): reserved for
  Phase 5's existing `correlation_candidate_links`
  (`CandidateLinkRecord`) — a real, already-durable record. **Documented
  here, not yet wired into `integration_projector.py`** — actually
  projecting it means touching Phase 5's existing, Gate C-adjacent
  projection code, which is out of this WP's scope and risks the exact
  blocker this pack warns about (altering existing projection behavior);
  recorded as a known limitation instead of attempted narrowly and
  riskily.
- **`PART_OF_THREAD` was not added.** No durable "thread" concept exists
  anywhere in this codebase today (searched: no `thread_id` or equivalent
  field on any communication/chat contract or model). Adding it would mean
  fabricating an edge with no record behind it — exactly what this pack's
  own rule forbids. If a future phase adds a real thread concept, this
  kind can be added then, backed by that record.
- **Review overlays (`VERIFIED`/`REJECTED`/`NEEDS_MORE_EVIDENCE`)** are
  not separate relationship kinds — they are properties *on* the
  `POSSIBLY_SAME_AS` edge (`effective_status`, sourced from WP-2's own
  `EntityReviewOutcome` values: `verified_same`/`rejected`/`split`,
  computed the same way `entity_resolution_review_view` already computes
  it for the HTTP read path). A separate relationship *type* per review
  outcome would mean rewriting the edge kind on every decision, which
  conflicts with WP-2's own append-only, reversible-by-a-new-decision
  design (ADR-020).

## Decision 3: G11 (pgvector) and G12 (hot window) are documented, not changed

Both remain exactly as they are:

- **G11**: `graph.intelligence.vector_store` stores `hashed_token_vector`
  output (a deterministic, non-learned embedding), not a semantic model's
  output. Replacing it with a real embedding model is a materially
  different, larger change (a new dependency, a new candidate-catalogue
  entry, a new Gate B benchmark cycle) than this WP's taxonomy-alignment
  scope.
- **G12**: `graph.intelligence.correlation.HOT_WINDOW_SECONDS = 900` is a
  fixed module constant, not Redis-backed. Making it Redis-backed and/or
  case-configurable is an operational-tuning change, not a taxonomy one.

Both are recorded as open questions in `docs/qa/known-limitations.md`'s
"WP-3" section, per this pack's own instruction to record them as "open"
rather than silently leave them undocumented.

## Alternatives considered

- **A strict, enum-backed `entity_type`.** Rejected: would require
  breaking `EntityV1`'s frozen V1 contract, explicitly forbidden by this
  pack's own non-negotiable rules.
- **Project `CANDIDATE_ASSOCIATION` now, reusing `integration_projector.py`
  directly.** Rejected: too large a change for this WP given the
  contract-conflict/blocker risk around Phase 5's existing, Gate
  C-adjacent projection path; documented as an explicit follow-up instead.
- **Invent a `PART_OF_THREAD` edge from `message_id`/platform-handle
  proximity.** Rejected outright: would be exactly the kind of fabricated
  edge this pack's own rules forbid — a real thread concept doesn't exist
  in any contract or model today.
