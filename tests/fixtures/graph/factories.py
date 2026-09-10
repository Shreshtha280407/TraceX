"""Synthetic, referentially-consistent contract fixtures for graph tests.

Builds on `tests/fixtures/factories.py`'s generic `make_*` builders (each
independently random by default) to wire `case_id`/`evidence_id`/
`observation_id`/`entity_id` together, so a full evidence -> observation ->
entity -> event chain is internally consistent -- exactly the shape
`app/modules/graph/projection.py` expects to apply cleanly end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.contracts.entity import EntityV1
from app.contracts.event import EventV1
from app.contracts.evidence import EvidenceRecordV1
from app.contracts.observation import ObservationV1
from tests.fixtures.factories import (
    make_entity,
    make_event,
    make_evidence_record,
    make_observation,
)


@dataclass(frozen=True)
class LinkedGraphFixture:
    """A referentially-consistent evidence -> observation -> entity -> event chain, one case."""

    evidence: EvidenceRecordV1
    observation: ObservationV1
    entity: EntityV1
    event: EventV1


def make_linked_graph_fixture(**overrides: Any) -> LinkedGraphFixture:
    """Build one internally-consistent chain, all sharing a single `case_id`.

    Pass `case_id=...` to pin the case, or `entity_id=...` to pin the entity
    (and its event's sole participant) -- e.g. to deliberately reuse the
    same `entity_id` across two different cases in an isolation test.
    Otherwise every ID is freshly generated.
    """
    case_id = overrides.pop("case_id", uuid4())
    entity_id = overrides.pop("entity_id", uuid4())
    evidence = make_evidence_record(case_id=case_id)
    observation = make_observation(case_id=case_id, evidence_id=evidence.evidence_id)
    entity = make_entity(
        case_id=case_id,
        entity_id=entity_id,
        created_from_observation_ids=[observation.observation_id],
    )
    event = make_event(
        case_id=case_id,
        participant_entity_ids=[entity.entity_id],
        evidence_refs=[evidence.evidence_id],
    )
    return LinkedGraphFixture(
        evidence=evidence, observation=observation, entity=entity, event=event
    )
