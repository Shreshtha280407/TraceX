# ADR-020: Entity layer and resolution review (Gap-Closure WP-2)

Status: **accepted**. Closes gap register G2 (`EntityV1` never instantiated;
no entity tables/routes/resolution review).

## Context

`app/contracts/entity.py`'s `EntityV1` was frozen in Phase 1 but never
written by any code — Phase 5 only produces observation-level correlation
candidates (`CandidateLinkRecord`), never a resolved entity. There was no
entity table, no entity route, and no reviewable path for "these two
things might be the same identity."

## Decision 1: entity creation scope is one entity per observation

An entity is created from exactly one observation
(`entity_service.create_entities_for_case`), never automatically merged
with another, even when a later observation states an identical
identifier. This is a **documented simplification**, not the final shape:
`sourcing.descriptors_from_observation` can return more than one
role-scoped descriptor for a two-party record (a CDR call's caller and
callee); this WP merges all of one observation's descriptors into a
single entity (union of identifiers/aliases) rather than splitting by
role. This keeps the entity <-> observation mapping 1:1, which
`retrieval.RetrievedCandidate` (keyed by `left_observation_id`/
`right_observation_id`, not a descriptor id) needs to be mapped back to
entities unambiguously. Splitting a two-party observation into two
role-scoped entities (a real, useful improvement) is deferred — seeing the
ambiguity clearly here is more honest than a lossy, incorrect first
attempt at it.

## Decision 2: candidate generation reuses the existing cascade unchanged

`entity_service.generate_entity_resolution_candidates` calls the exact
same `graph.intelligence.sourcing.descriptors_from_observation` and
`graph.intelligence.retrieval.retrieve_candidates` functions Phase 5's
own event-correlation pipeline already uses — no new matching logic was
written, and `graph.intelligence.scoring` (frozen, Gate C-bound) is never
imported. A `RetrievedCandidate` between two observations becomes an
`EntityResolutionCandidateRecord` between their two entities, carrying the
same `reasons`/`identifier_types`/`vector_score`/`contradiction_reasons`
the cascade already computes. Nothing is merged by this step, ever — an
exact-identifier match produces a candidate with reason
`exact_identifier`, not a merged entity (see the test
`test_exact_identifier_produces_a_candidate_never_a_merge`).

## Decision 3: review decisions are reversible, unlike candidate review

`candidate_review_decisions` (Phase 6 Part 5's own precedent) allows
exactly one immutable decision per candidate — a deliberate choice for
observation-correlation review. Entity resolution needs the opposite
property: **a merge must be reversible**, since new evidence can show two
"verified same" entities are actually different people. So
`entity_review_decisions` allows more than one decision per candidate
pair over time; the *effective* state is always the most recent decision
(`entity_models.entity_resolution_review_view`), computed at read time —
the same "effective status from latest/only decision" pattern
`review_models.candidate_review_view` already established, just extended
to a history instead of a single row. `VERIFIED_SAME` and `REJECTED`
decisions and a later `SPLIT` are all retained, append-only (same
PostgreSQL trigger precedent `candidate_review_decisions`/
`integrity_events` already use) — nothing is ever deleted or edited, so a
rejected or split candidate is always visible in its own decision history,
never silently hidden from audit.

## Decision 4: entity-scoped routes resolve their case from the entity, not the URL

`GET /api/v1/entities/{id}` and `POST /api/v1/entities/{id}/resolution-review`
have no `case_id` in their path (the plan's own route shape). Authorization
still runs the identical `policy.authorize_case_action` decision every
other case-scoped route uses — `entity_api._authorize_entity_action` looks
the entity up first (a cross-case-by-ID lookup, `EntityRepository.
get_entity_by_id_any_case`), then authorizes against *that* entity's real
`case_id` before returning any content. A caller with no access gets the
same 403 an ordinary case-scoped route would; only whether the raw UUID
exists at all is distinguishable via 404-vs-403 — the same minor signal
`evidence_lifecycle`'s per-evidence routes already accept, not a new one.

## Alternatives considered

- **Score/rank entity-resolution candidates like Phase 5 scores
  correlations.** Rejected: `scoring.py` is frozen and Gate C-bound;
  building a second, parallel scoring model for entities is out of this
  WP's scope and not requested by the gap register (G2 only asks for
  candidate *generation* and reviewable *decisions*, not ranking).
- **One immutable decision per entity-resolution candidate**, matching
  `candidate_review_decisions` exactly. Rejected: an entity merge must be
  reversible (the plan's own explicit "unmerge works" test requirement);
  a single immutable decision cannot represent "verified, then later
  split."
- **Physically merge entity rows on `VERIFIED_SAME`.** Rejected: mutating
  or deleting an entity row on a decision would violate the evidence-first
  append-only pattern every other durable table in this codebase follows,
  and would make `SPLIT` require reconstructing a deleted row rather than
  simply outranking it with a newer decision.
