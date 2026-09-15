"""Safe, deterministic Phase 6 visual/communication provenance tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from app.contracts.common import BoundingBoxNormalized, Extractor, SourceLocator
from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.media_orchestration import MediaChunkPublication
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from app.modules.integrity.modality_provenance import (
    CommunicationObservationIntegrityProvenanceV1,
    VisualObservationIntegrityProvenanceV1,
    build_communication_observation_provenance,
    build_visual_observation_provenance,
)
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import make_observation_batch_submission

NOW = datetime(2026, 9, 15, tzinfo=UTC)
EVIDENCE_SHA = "a" * 64


def _observation(
    *,
    case_id: UUID,
    evidence_id: UUID,
    observation_type: str,
    attributes: dict[str, JsonValue],
    locator: SourceLocator,
    text: str = "synthetic-safe-value",
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type=observation_type,
        extracted_entities=[ExtractedEntityMention(text=text, entity_type_hint="synthetic")],
        attributes=attributes,
        extraction_confidence=1.0,
        source_locator=locator,
        extractor=Extractor(
            name="synthetic-extractor",
            version="1",
            config_hash="synthetic-config",
            model_version="n/a",
        ),
        created_at=NOW,
    )


def _publication(observation: ObservationV1) -> MediaChunkPublication:
    submission = make_observation_batch_submission(
        job_id=uuid4(),
        case_id=observation.case_id,
        evidence_id=observation.evidence_id,
        observations=[observation],
        submitted_at=NOW,
    )
    return MediaChunkPublication(
        manifest_id=uuid4(),
        manifest_hash="b" * 64,
        chunk_id=uuid4(),
        chunk_index=0,
        batch=submission,
        checkpoint_id=uuid4(),
        completed_at=NOW,
    )


def test_visual_projection_is_safe_idempotent_and_uses_one_visual_leaf() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    raw_ocr = "plate AB12CD3456 and image pixels are protected"
    observation = _observation(
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="ocr_text_mention",
        text=raw_ocr,
        locator=SourceLocator(
            time_start_ms=100,
            time_end_ms=200,
            frame_number=3,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.2, y_max=0.2),
        ),
        attributes={
            "detected_label": "vehicle",
            "local_track_id": "local-track-private",
            "track_lifecycle_conditions": ["ended_unmatched"],
            "visual_signal_validation": {"outcome": "accepted", "correlation_ready": True},
        },
    )
    projection = build_visual_observation_provenance(
        observation=observation,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.VIDEO,
        publication=_publication(observation),
    )
    assert projection is not None
    assert projection.idempotency_key == (
        f"visual-provenance:{case_id}:{observation.observation_id}:visual_provenance.v1"
    )
    submission = projection.to_integrity_submission(source_created_at=NOW)
    assert submission.event_kind is IntegrityEventKind.OBSERVATION_PUBLISHED
    assert submission.subject_type == "visual_observation_provenance"
    assert submission.subject_id == str(observation.observation_id)
    payload = projection.canonical_metadata()
    for forbidden in (raw_ocr, "local-track-private", "AB12CD3456", "face"):
        assert forbidden not in str(payload)
    assert projection.text_content_commitment_sha256
    assert projection.local_track_commitment_sha256
    assert projection.local_track_lifecycle_condition == "ended_unmatched"
    assert "identity" not in str(payload)


def test_visual_rejects_missing_persisted_scope_or_non_accepted_validation() -> None:
    observation = _observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        observation_type="video_detection",
        locator=SourceLocator(time_start_ms=0, time_end_ms=100, frame_number=0),
        attributes={"visual_signal_validation": {"outcome": "accepted", "correlation_ready": True}},
    )
    assert (
        build_visual_observation_provenance(
            observation=observation,
            evidence_sha256=EVIDENCE_SHA,
            source_type=SourceType.VIDEO,
            publication=None,
        )
        is None
    )
    rejected = observation.model_copy(
        update={
            "attributes": {
                "visual_signal_validation": {"outcome": "rejected", "correlation_ready": False}
            }
        }
    )
    assert (
        build_visual_observation_provenance(
            observation=rejected,
            evidence_sha256=EVIDENCE_SHA,
            source_type=SourceType.VIDEO,
            publication=_publication(rejected),
        )
        is None
    )


def test_visual_locator_change_changes_commitment_without_changing_retry_key() -> None:
    case_id, evidence_id, observation_id = uuid4(), uuid4(), uuid4()
    first = _observation(
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="video_detection",
        locator=SourceLocator(time_start_ms=0, time_end_ms=100, frame_number=0),
        attributes={"visual_signal_validation": {"outcome": "accepted", "correlation_ready": True}},
    ).model_copy(update={"observation_id": observation_id})
    changed = first.model_copy(
        update={"source_locator": SourceLocator(time_start_ms=0, time_end_ms=101, frame_number=0)}
    )
    one = build_visual_observation_provenance(
        observation=first,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.VIDEO,
        publication=_publication(first),
    )
    two = build_visual_observation_provenance(
        observation=changed,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.VIDEO,
        publication=_publication(changed),
    )
    assert one and two
    assert one.idempotency_key == two.idempotency_key
    assert one.canonical_payload_sha256 != two.canonical_payload_sha256


def test_communication_projection_commits_audio_and_social_values_without_storing_them() -> None:
    case_id, evidence_id = uuid4(), uuid4()
    audio = _observation(
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="diarization_turn",
        text="speaker-local-7",
        locator=SourceLocator(time_start_ms=100, time_end_ms=200),
        attributes={
            "transcript_text_sha256": "c" * 64,
            "language_hint": "en",
            "communication_signal_validation": {"outcome": "accepted", "correlation_ready": True},
        },
    )
    social = _observation(
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type="username_or_handle",
        text="@private_handle",
        locator=SourceLocator(message_id="message-private", json_path="$.messages[0]"),
        attributes={
            "platform": "telegram",
            "message_id": "message-private",
            "sender": "@private_handle",
            "normalized_identifier": "@private_handle",
            "message_text_sha256": "d" * 64,
            "communication_signal_validation": {"outcome": "accepted", "correlation_ready": True},
        },
    )
    audio_projection = build_communication_observation_provenance(
        observation=audio,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.AUDIO,
        publication=_publication(audio),
    )
    social_projection = build_communication_observation_provenance(
        observation=social,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.TELEGRAM_CHAT,
        publication=None,
    )
    assert audio_projection and social_projection
    assert audio_projection.source_family.value == "audio"
    assert social_projection.source_family.value == "social"
    assert social_projection.idempotency_key == (
        f"communication-provenance:{case_id}:{social.observation_id}:communication_provenance.v1"
    )
    for raw in ("speaker-local-7", "@private_handle", "message-private"):
        assert raw not in str(audio_projection.canonical_metadata())
        assert raw not in str(social_projection.canonical_metadata())
    assert audio_projection.local_speaker_label_commitment_sha256
    assert social_projection.participant_commitment_sha256
    assert social_projection.message_identifier_commitment_sha256
    assert social_projection.to_integrity_submission(source_created_at=NOW).subject_type == (
        "communication_observation_provenance"
    )


def test_communication_requires_accepted_validation_and_audio_chunk_scope() -> None:
    observation = _observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        observation_type="transcript_segment",
        locator=SourceLocator(time_start_ms=0, time_end_ms=100),
        attributes={
            "communication_signal_validation": {"outcome": "accepted", "correlation_ready": True}
        },
    )
    assert (
        build_communication_observation_provenance(
            observation=observation,
            evidence_sha256=EVIDENCE_SHA,
            source_type=SourceType.AUDIO,
            publication=None,
        )
        is None
    )
    imported = build_communication_observation_provenance(
        observation=observation,
        evidence_sha256=EVIDENCE_SHA,
        source_type=SourceType.AUDIO_TRANSCRIPT,
        publication=None,
    )
    assert imported is not None
    assert imported.persisted_manifest_id is None
    assert imported.persisted_chunk_id is None
    incomplete = observation.model_copy(
        update={
            "attributes": {
                "communication_signal_validation": {
                    "outcome": "incomplete",
                    "correlation_ready": False,
                }
            }
        }
    )
    assert (
        build_communication_observation_provenance(
            observation=incomplete,
            evidence_sha256=EVIDENCE_SHA,
            source_type=SourceType.CHAT,
            publication=None,
        )
        is None
    )


def test_modality_projection_models_are_immutable() -> None:
    assert VisualObservationIntegrityProvenanceV1.model_config.get("frozen") is True
    assert CommunicationObservationIntegrityProvenanceV1.model_config.get("frozen") is True


class _RecordingIntegrity:
    def __init__(self) -> None:
        self.generic: list[IntegrityEventSubmission] = []
        self.modality: list[object] = []

    async def record_integrity_event(self, submission: IntegrityEventSubmission) -> None:
        self.generic.append(submission)

    async def record_modality_observation_provenance(
        self, projection: object, *, source_created_at: datetime
    ) -> None:
        assert source_created_at == NOW
        self.modality.append(projection)


async def test_accepted_social_batch_adds_one_leaf_without_duplicate_lifecycle_event() -> None:
    repository = FakeEvidenceLifecycleRepository()
    recorder = _RecordingIntegrity()
    service = EvidenceLifecycleService(
        repository=repository,  # type: ignore[arg-type]
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=1024,
        integrity_recorder=recorder,  # type: ignore[arg-type]
    )
    case_id = uuid4()
    upload = await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uuid4(),
        source_type=SourceType.CHAT,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=b"{}", content_type="application/json"),
        idempotency_key=None,
        context=UploadContext(now=NOW, request_id="req-modality-provenance"),
    )
    claim = await service.claim_job(
        processor_name="generic_social_json_v1",
        processor_version="1.0.0",
        context=UploadContext(now=NOW, request_id="req-modality-provenance"),
    )
    assert claim.job and claim.claim_token
    observation = _observation(
        case_id=case_id,
        evidence_id=upload.evidence.evidence_id,
        observation_type="chat_message",
        locator=SourceLocator(message_id="synthetic-message", json_path="$.messages[0]"),
        attributes={
            "platform": "telegram",
            "message_id": "synthetic-message",
            "sender": "synthetic-handle",
            "communication_signal_validation": {"outcome": "accepted", "correlation_ready": True},
        },
    )
    submission = make_observation_batch_submission(
        job_id=claim.job.job_id,
        case_id=case_id,
        evidence_id=upload.evidence.evidence_id,
        observations=[observation],
        submitted_at=NOW,
    )
    context = UploadContext(now=NOW, request_id="req-modality-provenance")
    await service.submit_observation_batch(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        submission=submission,
        context=context,
    )
    await service.submit_observation_batch(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        submission=submission,
        context=context,
    )
    observation_events = [
        event
        for event in recorder.generic
        if event.event_kind is IntegrityEventKind.OBSERVATION_PUBLISHED
    ]
    assert len(observation_events) == 1
    assert len(recorder.modality) == 1
