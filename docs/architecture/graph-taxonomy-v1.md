# Graph Taxonomy v1 (proposed)

Owner: Shreshtha. Status: **proposed, versioned taxonomy** for the graph layer built in `app/modules/graph/`. `EntityMention`/`MENTIONS` (below) were added in Phase 2.5 (`docs/architecture/graph-projection.md`); everything else in this document is unchanged from Phase 1.

**This document does not modify any frozen `V1` contract.** `EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, and the worker envelopes in `app/contracts/` are unchanged. Everything below is an internal graph-projection scheme that consumes those contracts; `entity_type` / `event_type` / `observation_type` remain plain, unconstrained strings at the contract level exactly as documented in `docs/architecture/phase-1-decisions.md` — the taxonomy values recommended here are conventions for graph consumers, not new validation rules on the contracts.

## Purpose

Defines the node labels, relationship types, and property rules that `app/modules/graph/projection.py` writes into Neo4j, and the safety rules every later phase (entity resolution, cross-modal correlation, candidate scoring, hypothesis engine) must keep respecting when it extends this graph.

## Node labels

Five core labels, matching the four canonical contracts plus their common case anchor:

| Label | Sourced from | Identity |
|---|---|---|
| `Case` | `case_id` present on every contract | `case_id` (globally unique) |
| `Evidence` | `EvidenceRecordV1` | `(case_id, evidence_id)` |
| `Observation` | `ObservationV1` | `(case_id, observation_id)` |
| `Entity` | `EntityV1` | `(case_id, entity_id)` |
| `Event` | `EventV1` | `(case_id, event_id)` |
| `EntityMention` | `ObservationV1.extracted_entities[i]` | `(case_id, mention_id)` |

`EntityMention` (Phase 2.5) is **not** an `Entity`. It is a raw, evidence-local mention exactly as it appeared in one observation — never resolved, never merged with another mention, never itself a claim about a real-world person/organization/location. `mention_id` is derived deterministically from `(case_id, observation_id, ordinal, normalized text)` (`app.core.ids.deterministic_uuid`), not from any content-similarity heuristic. See `docs/architecture/graph-projection.md` for the full design and the identity-safety reasoning.

No `Location` label is introduced in this phase. A location is represented as an `Entity` node with `entity_type = "location"` (see recommended entity taxonomy below) so there is exactly one way to represent "a place" in the graph, instead of two competing representations (a `Location` node vs. a `location`-typed `Entity`) that later entity-resolution/correlation code would have to reconcile. `ObservationV1.location` / `EventV1.location` (the loosely-structured `Location` value from `app/contracts/common.py`) is stored as plain properties on the `Observation`/`Event` node that carries it — it is evidence about a place mentioned in that observation/event, not itself a resolved graph entity.

**Identity is always case-scoped.** Nothing in this schema gives `Evidence`, `Observation`, `Entity`, or `Event` a global uniqueness guarantee on the domain ID alone — two different cases may reuse the same `evidence_id`-shaped value, the same phone number as an `Entity.canonical_label`, or coincidentally the same UUID space, and they must never collide into the same node. `Case.case_id` is the one label allowed a bare (non-compound) uniqueness constraint, since it is the case-isolation anchor itself.

## Core relationships

| Relationship | Direction | Required properties | Meaning |
|---|---|---|---|
| `HAS_EVIDENCE` | `(:Case)-[:HAS_EVIDENCE]->(:Evidence)` | none beyond graph structure | This evidence was ingested under this case. |
| `YIELDED_OBSERVATION` | `(:Evidence)-[:YIELDED_OBSERVATION]->(:Observation)` | none beyond graph structure | This observation was extracted from this evidence. |
| `HAS_OBSERVATION` | `(:Case)-[:HAS_OBSERVATION]->(:Observation)` | none beyond graph structure | Direct case-scoping shortcut for observations (avoids a 2-hop traversal through `Evidence` for every case-scoped observation query). |
| `HAS_ENTITY` | `(:Case)-[:HAS_ENTITY]->(:Entity)` | none beyond graph structure | This entity was resolved within this case. |
| `HAS_EVENT` | `(:Case)-[:HAS_EVENT]->(:Event)` | none beyond graph structure | This event was recorded within this case. |
| `SUPPORTS` | `(:Observation)-[:SUPPORTS]->(:Event)` | `case_id`, `evidence_id`, `observation_id`, `source_locator`, `assertion_kind` | An observation directly supports (is evidence for) an event having happened. |
| `HAS_PARTICIPANT` | `(:Event)-[:HAS_PARTICIPANT]->(:Entity)` | none beyond graph structure | This entity took part in this event. |
| `MENTIONS` | `(:Observation)-[:MENTIONS]->(:EntityMention)` | `ordinal` | This observation's `extracted_entities[ordinal]` is this mention. |

`assertion_kind` on `SUPPORTS` is `"fact"` (`AssertionKind.FACT`) only in this phase — a direct, faithful projection of a canonical observation, not a derived or hypothesized link. Inference/hypothesis assertion kinds are explicitly not created yet (later-phase hypothesis engine work).

### Why `SUPPORTS` is schema-defined but not populated by Phase 1 projection

`SUPPORTS` requires an `observation_id` **and** an `event_id` on the same edge — i.e. it requires knowing which specific observation(s) an event was built from. Neither `ObservationV1` nor `EventV1` carries that link in the frozen `V1` contracts: `ObservationV1` has no `event_id`/`supports_event_id` field, and `EventV1` has no `observation_ids` field (`EventV1.evidence_refs` cites evidence directly, not observations). Building an event out of one or more observations is later-phase correlation/aggregation work, not something Phase 1's direct canonical projection can derive on its own.

Rather than inventing a speculative field or an unrequested internal contract to carry that link now, `project_event()` in this phase does **not** create `SUPPORTS` edges. The relationship's label, direction, and required-property contract are fixed here so that whichever later phase builds events from observations has an unambiguous schema to write into — see `docs/qa/known-limitations.md` and the completion report's "decisions requiring team review" section.

### Why no direct `(:Entity)-[:KNOWS|CALLED|MET|SENT_MONEY_TO]->(:Entity)` edges

Every relationship two entities have in TraceX is mediated by a time-bounded `Event` (`(:Event)-[:HAS_PARTICIPANT]->(:Entity)`, at least twice for a two-party event). This is the graph-level enforcement of `EventV1`'s own invariant (`event_time` or `time_window` is mandatory — see `app/contracts/event.py`): a call, meeting, transaction, sighting, or message is an occurrence with a "when", not an unexplained standing association between two people. `project_event()` and `queries.py` never construct or expose a direct entity-to-entity edge type; this is enforced by the fixed relationship vocabulary in `GraphRelationshipKind` (`app/modules/graph/models.py`) — there is no entity-to-entity kind in that enum to accidentally use.

## Recommended entity taxonomy (`EntityV1.entity_type` values)

These are recommended string values for graph consumers, not a new validation rule on the frozen `EntityV1.entity_type` field (which stays a free-form string per `docs/architecture/phase-1-decisions.md`):

```text
person
organisation
phone_number
device_identifier
subscriber_identifier
financial_account
vehicle
location
address
online_handle
email_address
document_identifier
unknown
```

`unknown` is the deliberate escape hatch for an entity whose type cannot yet be determined from available observations — analogous to `SourceType.OTHER` on `EvidenceRecordV1`.

## Event taxonomy (`EventV1.event_type` values)

```text
call
message
financial_transaction
vehicle_sighting
person_sighting
meeting
document_mention
social_interaction
intel_reported_activity
```

Every one of these is, by construction, time-bounded: `EventV1` requires `event_time` or `time_window` (enforced by `EventV1._validate_time_bounded`), because an event represents something that happened at (or during) a specific time — never a standing, timeless fact about a relationship between entities. A recommended type describing something with no natural time bound does not belong in this list; it belongs as an `Entity`/`Observation` attribute instead.

## Identity and relationship safety

These rules bind every later phase that extends this graph, not just Phase 1:

- **Alias is not proof of identity.** `EntityV1.aliases` records names/labels an entity has been observed under; it never implies two `Entity` nodes sharing an alias are the same person.
- **Fuzzy name matching is not identity proof.** A high string-similarity score between two `canonical_label`/alias values is a *candidate signal* for a future review workflow, never a basis for automatic merge.
- **Transliteration similarity is not identity proof.** The same reasoning applies across scripts/languages — similarity is a candidate signal only.
- **Corroboration produces a reviewable candidate, not an automatic merge.** Shared phone numbers, accounts, devices, vehicles, contacts, timing, or location across two `Entity` nodes may justify a future *candidate identity link* for human review — it must never silently merge the two nodes or their observation histories.
- **Only a human-reviewed decision may promote a candidate identity relationship.** No code path in this phase (or implied by this phase) merges entities automatically. `ReviewStatus` (`unreviewed` / `confirmed` / `disputed` / `rejected`, from `app/contracts/common.py`) exists specifically so a merge or event confirmation is always an explicit, auditable analyst action.
- **An `EntityMention` is never merged with another mention, across observations or otherwise.** Two mentions with identical-looking `display_label` text — even within the same case — get distinct `mention_id`s and remain distinct nodes; nothing in `project_observation_mentions` compares one mention's content against another's. Promoting a set of mentions into a single resolved `Entity` is exactly the same kind of explicit, later-phase, human-reviewable operation as identity merging above — not something this phase performs automatically.
- **No criminality, guilt, or "suspect" conclusion is inferred from graph structure.** Node/edge existence, degree, or connectivity in this graph is a record of what evidence says was observed — never a computed guilt or suspicion score. `ObservationV1.extraction_confidence` and `EventV1.confidence` are extraction/statement-quality measures only (see `docs/architecture/contracts.md`), and nothing in this module recasts them as anything else.

## What later Shreshtha phases add on top of this foundation

Phase 2.5 (`docs/architecture/graph-projection.md`) wired `project_evidence`/`project_observation`/`project_observation_mentions` to a real, durable pipeline for the first time — `project_entity`/`project_event` remain unwired (nothing in this repository constructs a real `EntityV1`/`EventV1` yet). Still not built here:

- Candidate identity links (fuzzy/transliteration/corroboration-based), stored as their own reviewable relationship type, distinct from `HAS_*`/`SUPPORTS`/`HAS_PARTICIPANT`.
- The human review workflow that promotes a candidate link (or a set of `EntityMention`s) to a confirmed identity relationship/resolved `Entity`.
- Cross-modal correlation and candidate scoring.
- The hypothesis engine and any inference/hypothesis `assertion_kind` values on `SUPPORTS` (or a new relationship type) beyond `"fact"`.
- Graph analytics (centrality, community detection, motifs) — explicitly out of scope for this phase and the next.
