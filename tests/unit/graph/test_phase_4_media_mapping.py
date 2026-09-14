"""Focused Phase 4 mapping tests using canonical, synthetic observations only."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import BoundingBoxNormalized, Extractor, SourceLocator, TimeWindow
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.mapping import MEDIA_MAPPING_VERSION, MappingStatus, map_observation
from app.modules.graph.models import MediaProjectionLineage
from app.modules.graph.projection import project_specialized_mapping

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_EXTRACTOR = Extractor(name="synthetic", version="1", config_hash="fixture", model_version="n/a")


def _observation(
    observation_type: str,
    *,
    locator: SourceLocator,
    attributes: dict[str, object] | None = None,
    entities: list[ExtractedEntityMention] | None = None,
    event_time: datetime | None = None,
    time_window: TimeWindow | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=uuid4(),
        evidence_id=uuid4(),
        observation_type=observation_type,
        attributes=attributes or {},  # type: ignore[arg-type]
        extracted_entities=entities or [],
        event_time=event_time,
        time_window=time_window,
        extraction_confidence=0.8,
        source_locator=locator,
        extractor=_EXTRACTOR,
        created_at=_NOW,
    )


class _Graph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def write(self, query: str, parameters: dict[str, object]) -> list[dict[str, object]]:
        self.calls.append((query, parameters))
        return [{"projection_id": parameters.get("projection_id", "claim")}]


def test_staged_visual_observation_projects_provenance_rich_sighting() -> None:
    observation = _observation(
        "object_detection",
        locator=SourceLocator(
            frame_number=24,
            time_start_ms=960,
            time_end_ms=1000,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4),
        ),
        attributes={"detected_label": "person"},
    )
    lineage = MediaProjectionLineage(
        chunk_id=uuid4(),
        manifest_id=uuid4(),
        manifest_hash="manifest-hash",
        processor_version="detector-v1",
        configuration_hash="config-hash",
        artifact_ids=(uuid4(),),
    )
    plan = map_observation(observation, lineage)

    assert plan.status is MappingStatus.APPLIED
    assert plan.mapping_version == MEDIA_MAPPING_VERSION
    assert plan.event is not None and plan.event.event_type == "sighting"
    assert plan.event.properties["temporal_precision"] == "source_relative_ms"
    assert plan.event.properties["source_frame_number"] == 24
    assert plan.claims[0].claim_type == "visual_label"
    assert plan.media_lineage == lineage
    assert map_observation(observation, lineage) == plan  # replay uses the same identity


def test_audio_segment_uses_exact_source_range_without_identity_resolution() -> None:
    observation = _observation(
        "diarization_speaker_turn",
        locator=SourceLocator(time_start_ms=100, time_end_ms=230, json_path="$.segments[0]"),
        entities=[
            ExtractedEntityMention(text="SPEAKER_00", entity_type_hint="speaker_label_local")
        ],
    )
    plan = map_observation(observation)

    assert plan.event is not None and plan.event.event_type == "speech_segment"
    assert plan.event.properties["source_time_start_ms"] == 100
    assert plan.event.properties["source_time_end_ms"] == 230
    assert plan.claims[0].claim_type == "speaker_label_local"
    assert "Entity" not in plan.claims[0].model_dump_json()


def test_chat_message_preserves_locator_and_timezone_metadata_without_relationship_claim() -> None:
    observation = _observation(
        "chat_message",
        locator=SourceLocator(json_path="$.messages[4]", message_id="m-4"),
        attributes={
            "platform": "telegram",
            "conversation_id": "channel-1",
            "sender": "@sender",
            "participants": ["@recipient"],
            "timestamp_source_timezone": "Asia/Kolkata",
        },
        event_time=datetime(2026, 1, 1, 4, 30, tzinfo=UTC),
    )
    plan = map_observation(observation)

    assert plan.event is not None and plan.event.event_type == "message"
    assert plan.event.properties["timestamp_source_timezone"] == "Asia/Kolkata"
    assert {claim.role for claim in plan.claims} == {"sender_candidate", "recipient_candidate_0"}
    assert all(claim.claim_type == "platform_handle_claim" for claim in plan.claims)


def test_explicit_meeting_candidate_stays_candidate_only_and_tracks_contradiction() -> None:
    supporting, contradictory = uuid4(), uuid4()
    observation = _observation(
        "meeting_candidate",
        locator=SourceLocator(json_path="$.candidates[0]"),
        attributes={
            "candidate_status": "candidate",
            "candidate_reason_category": "explicit_upstream_review_candidate",
            "contributing_observation_ids": [str(supporting)],
            "contradictory_observation_ids": [str(contradictory)],
        },
        time_window=TimeWindow(start=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    plan = map_observation(observation)

    assert plan.event is not None and plan.event.event_type == "meeting_candidate"
    assert plan.event.properties["candidate_only"] is True
    assert plan.event.properties["candidate_status"] == "candidate"
    assert plan.event.supporting_observation_ids == (supporting,)
    assert plan.event.contradictory_observation_ids == (contradictory,)


def test_missing_or_invalid_candidate_and_unknown_time_defer_safely() -> None:
    candidate = _observation(
        "meeting_candidate",
        locator=SourceLocator(json_path="$.candidate"),
        attributes={"candidate_status": "verified"},
    )
    sighting = _observation(
        "object_detection",
        locator=SourceLocator(frame_number=1),
        attributes={"detected_label": "car"},
    )

    assert map_observation(candidate).status is MappingStatus.DEFERRED
    plan = map_observation(sighting)
    assert plan.event is not None
    assert plan.event.properties["temporal_precision"] == "unknown"
    assert plan.event.properties["temporal_precision_insufficient"] is True


def test_reversed_locator_or_absolute_window_is_rejected_before_mapping() -> None:
    with pytest.raises(ValueError, match="time_end_ms"):
        SourceLocator(time_start_ms=20, time_end_ms=10)
    with pytest.raises(ValueError, match="time_window.end"):
        TimeWindow(start=datetime(2026, 1, 2, tzinfo=UTC), end=datetime(2026, 1, 1, tzinfo=UTC))


async def test_specialised_media_event_only_writes_safe_properties() -> None:
    observation = _observation(
        "transcript_segment",
        locator=SourceLocator(time_start_ms=0, time_end_ms=20, json_path="$.segments[0]"),
        attributes={"text": "raw transcript must not reach Neo4j", "language_hint": "en"},
    )
    graph = _Graph()
    result = await project_specialized_mapping(graph, observation, map_observation(observation))  # type: ignore[arg-type]

    assert result is not None
    properties = graph.calls[-1][1]["properties"]
    assert isinstance(properties, dict)
    assert "text" not in properties
    assert properties["source_locator_time_end_ms"] == 20


async def test_meeting_candidate_references_are_case_scoped_review_edges() -> None:
    reference = uuid4()
    observation = _observation(
        "meeting_candidate",
        locator=SourceLocator(json_path="$.candidate"),
        attributes={
            "candidate_status": "candidate",
            "candidate_reason_category": "explicit_upstream_review_candidate",
            "contributing_observation_ids": [str(reference)],
        },
    )
    graph = _Graph()
    await project_specialized_mapping(graph, observation, map_observation(observation))  # type: ignore[arg-type]

    event_query, event_params = graph.calls[0]
    reference_query, reference_params = graph.calls[1]
    assert "Evidence {case_id: $case_id, evidence_id: $evidence_id}" in event_query
    assert "Observation {case_id: $case_id, observation_id: $observation_id}" in event_query
    assert (
        "Observation {case_id: $case_id, observation_id: reference.observation_id}"
        in reference_query
    )
    assert event_params["case_id"] == str(observation.case_id)
    assert reference_params["case_id"] == str(observation.case_id)
