"""Gap-Closure WP-2: entity creation, candidate generation, and resolution review.

**Entity creation scope (documented simplification):** one entity per
*observation*, not per descriptor/role. `descriptors_from_observation`
can return more than one role-scoped descriptor for a two-party record
(a CDR call's caller and callee) -- this WP merges all of one
observation's descriptors into a single entity (union of identifiers/
aliases) rather than creating a separate entity per role. This keeps the
entity <-> observation mapping 1:1, which `retrieval.RetrievedCandidate`
(keyed by `left_observation_id`/`right_observation_id`, not descriptor id)
needs to be mapped back to entities unambiguously. Splitting a two-party
observation into two role-scoped entities is documented future work (see
`docs/qa/known-limitations.md`'s "WP-2" section) -- not attempted here to
avoid a lossy, ambiguous mapping.

**Candidate generation reuses the existing cascade unchanged**:
`graph.intelligence.sourcing.descriptors_from_observation` +
`graph.intelligence.retrieval.retrieve_candidates` -- the same functions
Phase 5's event-correlation pipeline already uses. No new matching logic,
no scoring model, and `graph.intelligence.scoring` (frozen, Gate C-bound)
is never imported here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.contracts.entity import EntityV1
from app.contracts.observation import ObservationV1
from app.core.ids import deterministic_uuid
from app.modules.graph.entity_models import (
    ENTITY_CANDIDATE_CONFIG_VERSION,
    EntityResolutionCandidateRecord,
)
from app.modules.graph.entity_repository import EntityRepository
from app.modules.graph.intelligence.retrieval import (
    apply_blocks,
    exact_identifier_blocks,
    lexical_blocks,
    merge_candidates,
    retrieve_candidates,
)
from app.modules.graph.intelligence.sourcing import descriptors_from_observation
from app.modules.graph.intelligence.vector_store import (
    PgvectorCandidateStore,
    vector_linked_candidates,
)


def _entity_type_and_label(
    identifiers: dict[str, str], aliases: tuple[str, ...]
) -> tuple[str, str]:
    if identifiers:
        kind, value = next(iter(identifiers.items()))
        return kind, value
    if aliases:
        return "alias", aliases[0]
    return "unknown", "unlabeled entity"


async def create_entities_for_case(
    repository: EntityRepository,
    case_id: UUID,
    observations: list[ObservationV1],
    *,
    now: datetime | None = None,
) -> dict[UUID, EntityV1]:
    """Create (or fetch) one entity per observation that carries retrieval-eligible signal.

    Returns `{observation_id: entity}` for every observation that produced
    at least one entity -- observations with no identity signal at all
    (e.g. no identifiers or aliases on any of their descriptors) produce
    no entity, matching `EntityV1.created_from_observation_ids`'s
    non-empty requirement: an entity with no real source signal would be
    an analyst assertion with no evidence behind it.
    """
    now = now or datetime.now(UTC)
    entities: dict[UUID, EntityV1] = {}
    for observation in observations:
        descriptors = descriptors_from_observation(observation)
        if not descriptors:
            continue
        merged_identifiers: dict[str, str] = {}
        merged_aliases: list[str] = []
        for descriptor in descriptors:
            for kind, value in descriptor.identifiers.items():
                merged_identifiers.setdefault(kind, value)
            for alias in descriptor.aliases:
                if alias not in merged_aliases:
                    merged_aliases.append(alias)
        if not merged_identifiers and not merged_aliases:
            continue

        entity_type, canonical_label = _entity_type_and_label(
            merged_identifiers, tuple(merged_aliases)
        )
        entity_id = deterministic_uuid(
            "gap_closure_wp2_entity", str(case_id), str(observation.observation_id)
        )
        entity, _is_new = await repository.get_or_create_entity_for_observation(
            entity_id=entity_id,
            case_id=case_id,
            source_observation_id=observation.observation_id,
            entity_type=entity_type,
            canonical_label=canonical_label,
            aliases=tuple(merged_aliases),
            stable_identifiers=merged_identifiers,
            created_at=now,
        )
        entities[observation.observation_id] = entity
    return entities


async def generate_entity_resolution_candidates(
    repository: EntityRepository,
    case_id: UUID,
    observations: list[ObservationV1],
    *,
    now: datetime | None = None,
    vector_store: PgvectorCandidateStore | None = None,
) -> tuple[EntityResolutionCandidateRecord, ...]:
    """Create entities (if needed), run the existing retrieval cascade, persist candidates.

    Idempotent: re-running against the same observations recreates the
    same entities (deterministic IDs) and upserts the same candidates
    (deduplicated by `(case_id, left_entity_id, right_entity_id,
    config_version)`) -- never a duplicate row, never a merge.

    `vector_store` (ADR-030, entity-resolution cascade v4) is `None` by
    default -- Tier 3 (pgvector retrieval) is then simply skipped, and
    every existing caller/test (none of which have a real Postgres
    connection to spare) is unaffected. When a real caller passes one
    (`intelligence_worker.py`'s `--resolve-entities`), Tier 3 runs as a
    genuine bounded nearest-neighbor query per descriptor via `vector_
    store.vector_linked_candidates`, merged with Tiers 1-2's blocked
    output via `retrieval.merge_candidates` -- never a second, competing
    all-pairs scan alongside it (`include_inline_vector=False`).
    """
    now = now or datetime.now(UTC)
    entities_by_observation = await create_entities_for_case(
        repository, case_id, observations, now=now
    )
    if len(entities_by_observation) < 2:
        return ()

    descriptors = [
        descriptor
        for observation in observations
        if observation.observation_id in entities_by_observation
        for descriptor in descriptors_from_observation(observation)
    ]
    if len(descriptors) < 2:
        return ()

    tier1_blocks = exact_identifier_blocks(descriptors)
    tier2_blocks = lexical_blocks(descriptors)
    blocked = retrieve_candidates(
        descriptors,
        exact_identifier_blocks=tier1_blocks,
        lexical_blocks=tier2_blocks,
        include_inline_vector=vector_store is None,
    )
    retrieved = blocked
    if vector_store is not None:
        # Tier 3 shares Tier 1-2's own reduced item set (`apply_blocks`,
        # the identical reduction `retrieve_candidates` applied
        # internally) -- never the raw, unblocked descriptor list, or a
        # descriptor already absorbed into a Tier-1/2 block would still
        # get its own separate vector query.
        reduced = apply_blocks(apply_blocks(descriptors, tier1_blocks), tier2_blocks)
        vector_candidates = await vector_linked_candidates(vector_store, reduced)
        retrieved = merge_candidates(blocked, vector_candidates)
    persisted: list[EntityResolutionCandidateRecord] = []
    for item in retrieved:
        left_entity = entities_by_observation.get(item.left_observation_id)
        right_entity = entities_by_observation.get(item.right_observation_id)
        if (
            left_entity is None
            or right_entity is None
            or left_entity.entity_id == right_entity.entity_id
        ):
            continue
        left_id, right_id = sorted((left_entity.entity_id, right_entity.entity_id), key=str)
        candidate = EntityResolutionCandidateRecord(
            entity_resolution_candidate_id=deterministic_uuid(
                "gap_closure_wp2_candidate",
                str(case_id),
                str(left_id),
                str(right_id),
                ENTITY_CANDIDATE_CONFIG_VERSION,
            ),
            case_id=case_id,
            left_entity_id=left_id,
            right_entity_id=right_id,
            reasons=item.reasons,
            identifier_types=item.identifier_types,
            vector_score=item.vector_score,
            contradiction_reasons=item.contradiction_reasons,
            supporting_observation_ids=item.supporting_observation_ids,
            config_version=ENTITY_CANDIDATE_CONFIG_VERSION,
            created_at=now,
        )
        stored, _is_new = await repository.upsert_candidate(candidate)
        persisted.append(stored)
    return tuple(persisted)


__all__ = ["create_entities_for_case", "generate_entity_resolution_candidates"]
