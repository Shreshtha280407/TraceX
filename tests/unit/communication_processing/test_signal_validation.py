"""Phase 5B communication signal validation at producer and sourcing seams."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import SourceLocator
from app.contracts.evidence import SourceType
from app.modules.communication_processing.aliases.normalize import comparison_key
from app.modules.communication_processing.aliases.transliteration import generate_candidates
from app.modules.communication_processing.audio.diarization_import import (
    diarization_segments_to_mentions,
)
from app.modules.communication_processing.audio.transcript_import import (
    transcript_segments_to_mentions,
)
from app.modules.communication_processing.errors import ProcessingError
from app.modules.communication_processing.models import (
    DiarizationSegmentInput,
    SocialExportInput,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.provenance import mention_to_observation
from app.modules.communication_processing.signal_validation import (
    AudioChunkScope,
    CommunicationSignalOutcome,
    validate_audio_signal,
    validate_chat_signal,
    validate_identifier_signal,
)
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    chat_message_to_mention,
)
from app.modules.communication_processing.worker import TRANSCRIPT_IMPORT_V1, process_job
from app.modules.graph.intelligence.sourcing import descriptor_from_observation
from tests.fixtures.communication_processing.builders import build_whatsapp_export
from tests.fixtures.communication_processing.factory import make_job


def _record(
    *,
    platform: str = "telegram",
    sender: str | None = "Alice",
    timestamp: datetime | None = datetime(2026, 1, 1, tzinfo=UTC),
    message_id: str | None = "m-1",
    locator: SourceLocator | None = None,
) -> ChatMessageRecord:
    return ChatMessageRecord(
        platform=platform,
        conversation_id="chat-1",
        message_id=message_id,
        sender=sender,
        participants=("Alice", "Bob"),
        timestamp_raw="2026-01-01T00:00:00Z" if timestamp is not None else None,
        timestamp_utc=timestamp,
        timestamp_source_timezone=None,
        text="synthetic private message body",
        reply_to=None,
        locator=locator or SourceLocator(message_id="m-1"),
    )


def test_valid_asr_segment_retains_safe_timing_chunk_and_backend_provenance() -> None:
    mention = transcript_segments_to_mentions(
        (
            TranscriptSegmentInput(
                start_ms=1_000,
                end_ms=1_500,
                text="synthetic transcript content",
                language_hint="HI",
                confidence=0.8,
                source_segment_id="seg-1",
            ),
        ),
        provenance_attributes={
            "asr_backend": "offline-command",
            "asr_backend_version": "1",
            "asr_model_version": "fixture",
            "asr_configuration_hash": "config-hash",
            "audio_profile": "rapid",
        },
        chunk_scope=AudioChunkScope(1_000, 2_000, manifest_id="manifest-1", chunk_id="chunk-1"),
    )[0]
    validation = mention.attributes["communication_signal_validation"]
    assert validation["outcome"] == "accepted"
    assert validation["correlation_ready"] is True
    assert mention.attributes["transcript_text_length"] == len("synthetic transcript content")
    assert "synthetic transcript content" not in str(mention.attributes)


@pytest.mark.parametrize(("start", "end"), [(-1, 20), (20, 10)])
def test_invalid_audio_times_are_rejected(start: int, end: int) -> None:
    # SourceLocator itself rejects these invalid ranges, so use the supplied
    # range validator's chunk boundary case for producer-visible rejection.
    result = validate_audio_signal(
        SourceLocator(time_start_ms=0, time_end_ms=10),
        signal_kind="transcript",
        chunk_scope=AudioChunkScope(start, end),
    )
    assert result.outcome is CommunicationSignalOutcome.REJECTED
    assert result.correlation_ready is False


def test_audio_outside_actual_supplied_chunk_is_rejected_before_publication() -> None:
    with pytest.raises(ProcessingError):
        transcript_segments_to_mentions(
            (
                TranscriptSegmentInput(
                    start_ms=900,
                    end_ms=1_100,
                    text="x",
                    language_hint="en",
                    confidence=0.5,
                    source_segment_id="s",
                ),
            ),
            chunk_scope=AudioChunkScope(1_000, 2_000),
        )


def test_diarization_turn_is_unresolved_and_chunk_scoped() -> None:
    mention = diarization_segments_to_mentions(
        (DiarizationSegmentInput(0, 500, "SPEAKER_00", 0.5, "turn-1"),),
        chunk_scope=AudioChunkScope(0, 1_000, manifest_id="m", chunk_id="c"),
    )[0]
    assert mention.entity_type_hint == "speaker_label_local"
    assert mention.attributes["speaker_identity_status"] == "source_local_unresolved"
    assert mention.attributes["communication_signal_validation"]["correlation_ready"] is True


def test_diarization_turn_crossing_a_supplied_chunk_is_rejected() -> None:
    with pytest.raises(ProcessingError):
        diarization_segments_to_mentions(
            (DiarizationSegmentInput(900, 1_100, "SPEAKER_00", 0.5, "turn-1"),),
            chunk_scope=AudioChunkScope(0, 1_000),
        )


@pytest.mark.parametrize("platform", ["whatsapp", "telegram", "instagram", "generic"])
def test_platform_message_metadata_is_provenance_rich_and_content_safe(platform: str) -> None:
    mention = chat_message_to_mention(_record(platform=platform))
    assert mention.attributes["platform"] == platform
    assert mention.attributes["message_text_sha256"]
    assert "synthetic private message body" not in str(mention.attributes)
    assert mention.attributes["communication_signal_validation"]["outcome"] == "accepted"


def test_missing_sender_or_unresolved_time_is_incomplete_not_correlation_ready() -> None:
    assert (
        validate_chat_signal(_record(sender=None)).outcome is CommunicationSignalOutcome.INCOMPLETE
    )
    result = validate_chat_signal(_record(timestamp=None))
    assert result.outcome is CommunicationSignalOutcome.INCOMPLETE
    assert result.correlation_ready is False


def test_identifier_normalization_is_deterministic_and_handle_is_platform_scoped() -> None:
    locator = SourceLocator(message_id="m-1")
    first, normalized_first = validate_identifier_signal(
        "  @Alice  ", identifier_type="handle", platform="telegram", locator=locator
    )
    second, normalized_second = validate_identifier_signal(
        "@ALICE", identifier_type="handle", platform="instagram", locator=locator
    )
    assert first.outcome is CommunicationSignalOutcome.ACCEPTED
    assert second.outcome is CommunicationSignalOutcome.ACCEPTED
    assert normalized_first == normalized_second == "@alice"
    assert first.extractor_identity == second.extractor_identity == {}
    missing_platform, _ = validate_identifier_signal(
        "@alice", identifier_type="handle", platform=None, locator=locator
    )
    assert missing_platform.outcome is CommunicationSignalOutcome.INCOMPLETE


def test_transliteration_comparison_is_symmetric_and_candidate_only() -> None:
    generated = generate_candidates("राहुल")
    candidate = generated.candidates[-1]
    left = comparison_key(candidate)
    right = comparison_key("  RAAHULA ")
    # Swapping comparison order cannot change deterministic normalized equality;
    # the result remains a candidate representation, never an identity claim.
    assert (left == right) is (right == left) is True
    assert generated.category == "deterministic_transliteration"


def test_incomplete_message_cannot_reach_phase5_sourcing() -> None:
    mention = chat_message_to_mention(_record(sender=None))
    observation = mention_to_observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        profile=TRANSCRIPT_IMPORT_V1,
        mention=mention,
        created_at=datetime.now(UTC),
    )
    assert descriptor_from_observation(observation) is None


def test_actual_worker_does_not_publish_identifiers_from_unresolved_chat_time() -> None:
    job = make_job(processor_name="whatsapp_export_v1", source_type=SourceType.WHATSAPP_CHAT)
    # The line has a valid export layout but impossible calendar values, so
    # it stays locatable while its time is explicitly incomplete.
    result = process_job(
        job,
        SocialExportInput(
            platform="whatsapp",
            data=build_whatsapp_export(["99/99/26, 10:00 - Alice: call 9876543210"]),
        ),
    )
    assert [item.observation_type for item in result.observations] == ["chat_message"]
    validation = result.observations[0].attributes["communication_signal_validation"]
    assert validation["correlation_ready"] is False
