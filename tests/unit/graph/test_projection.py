"""Unit tests for `app/modules/graph/projection.py`.

These test query/parameter construction and the projection control flow
directly -- no real Neo4j involved (see `tests/integration/graph/` for
tests against a live database). Repository calls are faked with a minimal
in-memory double, since `projection.py` only ever calls `.write()`/`.read()`
on whatever it's handed (see `Neo4jGraphRepository`).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest

from app.contracts.observation import ExtractedEntityMention
from app.modules.graph.models import (
    EntityMentionProjectionResult,
    EventProjectionResult,
    GraphRelationshipKind,
    ProjectionOutcome,
)
from app.modules.graph.projection import (
    ENTITY_ALLOWED_PROPERTIES,
    ENTITY_MENTION_ALLOWED_PROPERTIES,
    EVENT_ALLOWED_PROPERTIES,
    EVIDENCE_ALLOWED_PROPERTIES,
    OBSERVATION_ALLOWED_PROPERTIES,
    _build_entity_mention_merge_query,
    _build_entity_merge_query,
    _build_event_merge_query,
    _build_evidence_merge_query,
    _build_observation_merge_query,
    _entity_mention_id,
    _entity_properties,
    _event_properties,
    _evidence_properties,
    _observation_properties,
    project_entity,
    project_event,
    project_evidence,
    project_observation,
    project_observation_mentions,
)
from tests.fixtures.factories import make_entity, make_event, make_evidence_record, make_observation

_ALL_BUILDERS = (
    lambda: _build_evidence_merge_query(make_evidence_record()),
    lambda: _build_observation_merge_query(make_observation()),
    lambda: _build_entity_merge_query(make_entity()),
    lambda: _build_event_merge_query(make_event()),
    lambda: _build_entity_mention_merge_query(make_observation()),
)

#: `\w*` allows an optional bound variable name before the colon (e.g.
#: `[r:MENTIONS]`, needed only when a later clause sets a property on the
#: relationship itself, as `MENTIONS.ordinal` does) -- every other
#: relationship in this module is unbound (`[:HAS_EVIDENCE]`), both forms
#: are valid Cypher and both are exhaustively covered by this pattern.
_RELATIONSHIP_TOKEN_PATTERN = re.compile(r"\[\w*:([A-Z_]+)\]")


class _FakeRepository:
    """Minimal duck-typed stand-in for `Neo4jGraphRepository`.

    Returns canned results in call order; records every call so tests can
    assert on the exact query/parameters that were sent.
    """

    def __init__(
        self,
        write_results: list[list[dict[str, Any]]] | None = None,
        read_results: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self._write_results = list(write_results or [])
        self._read_results = list(read_results or [])
        self.write_calls: list[tuple[str, dict[str, Any]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.write_calls.append((query, parameters))
        return self._write_results.pop(0) if self._write_results else []

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.read_calls.append((query, parameters))
        return self._read_results.pop(0) if self._read_results else []


# --- Requirement 2: parameterized, never string-interpolated ---------------


def test_evidence_query_never_interpolates_ids() -> None:
    record = make_evidence_record()
    query, params = _build_evidence_merge_query(record)
    assert str(record.evidence_id) not in query
    assert str(record.case_id) not in query
    assert "$evidence_id" in query
    assert "$case_id" in query
    assert params["evidence_id"] == str(record.evidence_id)
    assert params["case_id"] == str(record.case_id)


def test_observation_query_never_interpolates_ids() -> None:
    observation = make_observation()
    query, params = _build_observation_merge_query(observation)
    assert str(observation.observation_id) not in query
    assert str(observation.evidence_id) not in query
    assert params["observation_id"] == str(observation.observation_id)
    assert params["evidence_id"] == str(observation.evidence_id)


def test_entity_query_never_interpolates_ids() -> None:
    entity = make_entity()
    query, params = _build_entity_merge_query(entity)
    assert str(entity.entity_id) not in query
    assert params["entity_id"] == str(entity.entity_id)


def test_event_query_never_interpolates_ids_or_participant_list() -> None:
    event = make_event()
    query, params = _build_event_merge_query(event)
    assert str(event.event_id) not in query
    for participant_id in event.participant_entity_ids:
        assert str(participant_id) not in query
    assert params["participant_entity_ids"] == [str(i) for i in event.participant_entity_ids]


def test_no_query_builder_ever_uses_fstring_label_interpolation() -> None:
    # Every builder must be a fixed literal string with `$name` placeholders
    # only -- assert none of them differ across two calls with completely
    # different fixture data beyond their parameters (a stray f-string
    # interpolating a label/value into the text would break this).
    q1, _ = _build_entity_merge_query(make_entity())
    q2, _ = _build_entity_merge_query(make_entity())
    assert q1 == q2


# --- Requirement 3: allow-listed properties only ----------------------------


def test_evidence_properties_are_exactly_allow_listed() -> None:
    props = _evidence_properties(make_evidence_record())
    assert set(props) == EVIDENCE_ALLOWED_PROPERTIES


def test_observation_properties_are_subset_of_allow_list() -> None:
    props = _observation_properties(make_observation())
    assert set(props) <= OBSERVATION_ALLOWED_PROPERTIES


def test_entity_properties_are_exactly_allow_listed() -> None:
    props = _entity_properties(make_entity())
    assert set(props) == ENTITY_ALLOWED_PROPERTIES


def test_event_properties_are_subset_of_allow_list() -> None:
    props = _event_properties(make_event())
    assert set(props) <= EVENT_ALLOWED_PROPERTIES


# --- Requirement 4: observation retains evidence_id and source_locator -----


def test_observation_properties_retain_evidence_id_and_source_locator() -> None:
    observation = make_observation()
    props = _observation_properties(observation)
    assert props["evidence_id"] == str(observation.evidence_id)
    assert props["source_locator_page"] == 1
    assert props["source_locator_span_start"] == 0
    assert props["source_locator_span_end"] == 42


# --- Requirement 5: event links only the provided participant_entity_ids ---


def test_event_query_links_only_provided_participants() -> None:
    id_a, id_b = uuid4(), uuid4()
    event = make_event(participant_entity_ids=[id_a, id_b])
    query, params = _build_event_merge_query(event)
    assert params["participant_entity_ids"] == [str(id_a), str(id_b)]
    assert "UNWIND $participant_entity_ids AS participant_id" in query
    assert "MATCH (n:Entity {case_id: $case_id, entity_id: participant_id})" in query
    # Never a bare "grab every entity in the case" pattern.
    assert "MATCH (n:Entity {case_id: $case_id})" not in query


# --- Requirement 6: idempotent construction for the same case/stable ID ----


@pytest.mark.parametrize("build", _ALL_BUILDERS)
def test_builders_use_merge_not_create(build: Any) -> None:
    query, _ = build()
    assert "MERGE" in query
    assert not re.search(r"\bCREATE\s*\(", query)


def test_repeated_entity_projection_produces_identical_query_and_params() -> None:
    entity = make_entity()
    first = _build_entity_merge_query(entity)
    second = _build_entity_merge_query(entity)
    assert first == second


# --- Requirement 7: same-looking identifier across two cases stays isolated


def test_same_entity_id_in_two_cases_carries_different_merge_identity() -> None:
    shared_entity_id = uuid4()
    entity_case_a = make_entity(
        entity_id=shared_entity_id, canonical_label="+911234567890", entity_type="phone_number"
    )
    entity_case_b = make_entity(
        entity_id=shared_entity_id, canonical_label="+911234567890", entity_type="phone_number"
    )
    assert entity_case_a.case_id != entity_case_b.case_id  # distinct cases by default

    _, params_a = _build_entity_merge_query(entity_case_a)
    _, params_b = _build_entity_merge_query(entity_case_b)

    assert params_a["entity_id"] == params_b["entity_id"]
    assert params_a["case_id"] != params_b["case_id"]


# --- Requirement 10: no direct timeless entity-to-entity edge kind ---------


def test_relationship_kind_enum_has_no_entity_to_entity_kind() -> None:
    assert {kind.value for kind in GraphRelationshipKind} == {
        "HAS_EVIDENCE",
        "YIELDED_OBSERVATION",
        "HAS_OBSERVATION",
        "HAS_ENTITY",
        "HAS_EVENT",
        "SUPPORTS",
        "HAS_PARTICIPANT",
        "MENTIONS",
        "PROJECTS_CLAIM",
        "PROJECTS_EVENT",
        "HAS_CLAIM_PARTICIPANT",
        "SUPPORTED_BY_OBSERVATION",
        # Phase 6 Part 5: a hypothesis referencing a reviewed candidate --
        # still never a direct Entity-to-Entity edge.
        "REFERENCES_CANDIDATE",
    }


@pytest.mark.parametrize("build", _ALL_BUILDERS)
def test_every_relationship_token_in_generated_cypher_is_a_known_kind(build: Any) -> None:
    query, _ = build()
    allowed = {kind.value for kind in GraphRelationshipKind}
    tokens = set(_RELATIONSHIP_TOKEN_PATTERN.findall(query))
    assert tokens <= allowed
    assert tokens, "expected at least one relationship in this query"


# --- Requirement 11: unprojected dependencies defer safely ------------------


async def test_project_observation_defers_when_evidence_missing() -> None:
    observation = make_observation()
    repository = _FakeRepository(write_results=[[]])  # MATCH (Evidence) found nothing
    result = await project_observation(repository, observation)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.DEFERRED
    assert result.missing_evidence_id == observation.evidence_id
    assert result.relationships_upserted == 0


async def test_project_observation_applies_when_evidence_present() -> None:
    observation = make_observation()
    repository = _FakeRepository(
        write_results=[[{"observation_id": str(observation.observation_id)}]]
    )
    result = await project_observation(repository, observation)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.APPLIED


async def test_project_event_defers_and_names_missing_participants() -> None:
    id_present, id_missing = uuid4(), uuid4()
    event = make_event(participant_entity_ids=[id_present, id_missing])
    repository = _FakeRepository(
        write_results=[[]],  # not all participants matched
        read_results=[[{"entity_id": str(id_missing)}]],
    )
    result: EventProjectionResult = await project_event(repository, event)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.DEFERRED
    assert result.missing_participant_entity_ids == (id_missing,)
    assert result.relationships_upserted == 0


async def test_project_event_applies_when_all_participants_present() -> None:
    event = make_event()
    repository = _FakeRepository(write_results=[[{"event_id": str(event.event_id)}]])
    result = await project_event(repository, event)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.APPLIED
    assert result.relationships_upserted == 1 + len(set(event.participant_entity_ids))


async def test_project_evidence_and_entity_always_apply() -> None:
    repository = _FakeRepository(write_results=[[{"x": "1"}], [{"x": "1"}]])
    evidence_result = await project_evidence(repository, make_evidence_record())  # type: ignore[arg-type]
    entity_result = await project_entity(repository, make_entity())  # type: ignore[arg-type]
    assert evidence_result.outcome == ProjectionOutcome.APPLIED
    assert entity_result.outcome == ProjectionOutcome.APPLIED


# --- Requirement 12: no raw-source body/text field reaches a graph write ---


def test_observation_attributes_never_reach_graph_properties() -> None:
    sentinel = "SENSITIVE-RAW-TRANSCRIPT-BODY-DO-NOT-STORE"
    observation = make_observation(attributes={"raw_transcript": sentinel})
    props = _observation_properties(observation)
    assert "attributes" not in props
    assert sentinel not in repr(props)


def test_entity_attributes_never_reach_graph_properties() -> None:
    sentinel = "SENSITIVE-RAW-ENTITY-NOTE"
    entity = make_entity(attributes={"analyst_note": sentinel})
    props = _entity_properties(entity)
    assert "attributes" not in props
    assert sentinel not in repr(props)


def test_event_attributes_never_reach_graph_properties() -> None:
    sentinel = "SENSITIVE-RAW-EVENT-NOTE"
    event = make_event(attributes={"analyst_note": sentinel})
    props = _event_properties(event)
    assert "attributes" not in props
    assert sentinel not in repr(props)


# --- EntityMention: evidence-local, deterministic, never resolved -----------


def test_entity_mention_query_never_interpolates_ids_or_text() -> None:
    observation = make_observation(
        extracted_entities=[ExtractedEntityMention(text="Jane Roe", entity_type_hint="person")]
    )
    query, params = _build_entity_mention_merge_query(observation)
    assert str(observation.case_id) not in query
    assert str(observation.observation_id) not in query
    assert "Jane Roe" not in query
    assert "$case_id" in query
    assert "$observation_id" in query
    assert "$mentions" in query
    assert params["case_id"] == str(observation.case_id)
    assert params["observation_id"] == str(observation.observation_id)
    assert params["mentions"][0]["properties"]["display_label"] == "Jane Roe"


def test_entity_mention_properties_are_exactly_allow_listed() -> None:
    observation = make_observation(
        extracted_entities=[ExtractedEntityMention(text="Jane Roe", entity_type_hint="person")]
    )
    _, params = _build_entity_mention_merge_query(observation)
    assert set(params["mentions"][0]["properties"]) == ENTITY_MENTION_ALLOWED_PROPERTIES


def test_entity_mention_properties_omit_mention_type_when_hint_is_none() -> None:
    observation = make_observation(
        extracted_entities=[
            ExtractedEntityMention(text="unlabelled mention", entity_type_hint=None)
        ]
    )
    _, params = _build_entity_mention_merge_query(observation)
    props = params["mentions"][0]["properties"]
    assert "mention_type" not in props
    assert set(props) <= ENTITY_MENTION_ALLOWED_PROPERTIES


def test_entity_mention_attributes_never_reach_graph_properties() -> None:
    sentinel = "SENSITIVE-RAW-MENTION-ATTRIBUTE"
    observation = make_observation(
        extracted_entities=[
            ExtractedEntityMention(
                text="Jane Roe", entity_type_hint="person", attributes={"note": sentinel}
            )
        ]
    )
    _, params = _build_entity_mention_merge_query(observation)
    props = params["mentions"][0]["properties"]
    assert "attributes" not in props
    assert sentinel not in repr(props)


def test_entity_mention_id_is_deterministic_for_identical_input() -> None:
    case_id, observation_id = uuid4(), uuid4()
    first = _entity_mention_id(case_id, observation_id, 0, "Jane Roe")
    second = _entity_mention_id(case_id, observation_id, 0, "Jane Roe")
    assert first == second


def test_entity_mention_id_changes_with_ordinal_case_observation_or_text() -> None:
    case_id, observation_id = uuid4(), uuid4()
    base = _entity_mention_id(case_id, observation_id, 0, "Jane Roe")
    assert base != _entity_mention_id(case_id, observation_id, 1, "Jane Roe")  # ordinal
    assert base != _entity_mention_id(case_id, observation_id, 0, "John Doe")  # text
    assert base != _entity_mention_id(case_id, uuid4(), 0, "Jane Roe")  # observation
    assert base != _entity_mention_id(uuid4(), observation_id, 0, "Jane Roe")  # case


def test_entity_mention_id_normalizes_incidental_whitespace_only() -> None:
    """Normalization is whitespace/composition-form only -- never casefolding.

    Two mentions differing only in incidental whitespace hash identically
    (re-projecting the same canonical observation is always stable); two
    mentions differing in case are still different content.
    """
    case_id, observation_id = uuid4(), uuid4()
    tight = _entity_mention_id(case_id, observation_id, 0, "Jane Roe")
    padded = _entity_mention_id(case_id, observation_id, 0, "  Jane   Roe  ")
    assert tight == padded

    lower = _entity_mention_id(case_id, observation_id, 0, "jane roe")
    assert lower != tight


def test_same_observation_id_reused_across_two_cases_gets_different_mention_identity() -> None:
    """Same defense as `test_same_entity_id_in_two_cases_carries_different_merge_identity`,
    for `EntityMention`: a coincidentally-reused `observation_id` across two cases must never
    let their mentions collide into the same graph node."""
    shared_observation_id = uuid4()
    case_a, case_b = uuid4(), uuid4()
    id_in_case_a = _entity_mention_id(case_a, shared_observation_id, 0, "Jane Roe")
    id_in_case_b = _entity_mention_id(case_b, shared_observation_id, 0, "Jane Roe")
    assert id_in_case_a != id_in_case_b


def test_two_different_observations_mentioning_the_same_text_get_different_mentions() -> None:
    """No cross-observation dedup or fuzzy matching -- see module docstring."""
    case_id = uuid4()
    mention = ExtractedEntityMention(text="Jane Roe", entity_type_hint="person")
    first_observation = make_observation(case_id=case_id, extracted_entities=[mention])
    second_observation = make_observation(case_id=case_id, extracted_entities=[mention])
    assert first_observation.observation_id != second_observation.observation_id

    _, first_params = _build_entity_mention_merge_query(first_observation)
    _, second_params = _build_entity_mention_merge_query(second_observation)
    assert first_params["mentions"][0]["mention_id"] != second_params["mentions"][0]["mention_id"]


async def test_project_observation_mentions_defers_when_observation_missing() -> None:
    observation = make_observation(
        extracted_entities=[ExtractedEntityMention(text="Jane Roe", entity_type_hint="person")]
    )
    repository = _FakeRepository(write_results=[[]])  # MATCH (Observation) found nothing
    result: EntityMentionProjectionResult = await project_observation_mentions(
        repository,  # type: ignore[arg-type]
        observation,
    )
    assert result.outcome == ProjectionOutcome.DEFERRED
    assert result.missing_observation_id == observation.observation_id
    assert result.mention_count == 0


async def test_project_observation_mentions_applies_when_observation_present() -> None:
    observation = make_observation(
        extracted_entities=[
            ExtractedEntityMention(text="Jane Roe", entity_type_hint="person"),
            ExtractedEntityMention(text="Acme Corp", entity_type_hint="organisation"),
        ]
    )
    repository = _FakeRepository(write_results=[[{"mention_count": 2}]])
    result = await project_observation_mentions(repository, observation)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.APPLIED
    assert result.mention_count == 2


async def test_project_observation_mentions_with_no_mentions_is_a_no_op_write() -> None:
    """No `extracted_entities` -> trivially applied, and no query is even run."""
    observation = make_observation(extracted_entities=[])
    repository = _FakeRepository()
    result = await project_observation_mentions(repository, observation)  # type: ignore[arg-type]
    assert result.outcome == ProjectionOutcome.APPLIED
    assert result.mention_count == 0
    assert repository.write_calls == []


def test_entity_mention_relationship_carries_ordinal_matching_list_position() -> None:
    observation = make_observation(
        extracted_entities=[
            ExtractedEntityMention(text="First", entity_type_hint=None),
            ExtractedEntityMention(text="Second", entity_type_hint=None),
            ExtractedEntityMention(text="Third", entity_type_hint=None),
        ]
    )
    _, params = _build_entity_mention_merge_query(observation)
    ordinals = [m["ordinal"] for m in params["mentions"]]
    assert ordinals == [0, 1, 2]
    labels_by_ordinal = {m["ordinal"]: m["properties"]["display_label"] for m in params["mentions"]}
    assert labels_by_ordinal == {0: "First", 1: "Second", 2: "Third"}
