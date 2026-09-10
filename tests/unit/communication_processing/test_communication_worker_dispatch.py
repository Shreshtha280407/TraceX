"""Scenarios 10, 28, 29: worker dispatch, ASR/diarization deferral, safe error/deferred results."""

from __future__ import annotations

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerResultV1, WorkerStatus
from app.modules.communication_processing.models import (
    AudioMetadataInput,
    DiarizationImportInput,
    DiarizationSegmentInput,
    SocialExportInput,
    TranscriptImportInput,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.worker import process_job
from tests.fixtures.communication_processing.builders import build_wav_bytes, build_whatsapp_export
from tests.fixtures.communication_processing.factory import make_job


def test_audio_metadata_job_succeeds() -> None:
    job = make_job(processor_name="audio_metadata_v1", source_type=SourceType.AUDIO)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    assert isinstance(result, WorkerResultV1)
    assert result.status == WorkerStatus.SUCCEEDED
    assert result.observations


def test_transcript_import_job_succeeds() -> None:
    job = make_job(processor_name="transcript_import_v1", source_type=SourceType.AUDIO)
    segments = (
        TranscriptSegmentInput(
            start_ms=0,
            end_ms=500,
            text="hi",
            language_hint="en",
            confidence=0.5,
            source_segment_id="s1",
        ),
    )
    result = process_job(job, TranscriptImportInput(segments=segments))
    assert result.status == WorkerStatus.SUCCEEDED
    assert result.observations[0].observation_type == "transcript_segment"


def test_transcript_import_requested_with_only_raw_audio_defers() -> None:
    """Scenario 10: a transcript request given only raw audio must defer, never succeed."""
    job = make_job(processor_name="transcript_import_v1", source_type=SourceType.AUDIO)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    assert result.status == WorkerStatus.DEFERRED
    assert result.observations == []
    assert result.error is None
    assert result.checkpoint is not None
    assert "deferred_requires_asr" in result.checkpoint


def test_diarization_import_requested_with_only_raw_audio_defers() -> None:
    """Scenario 10: a request for diarization, given only raw audio, must defer -- never succeed."""
    job = make_job(processor_name="diarization_import_v1", source_type=SourceType.AUDIO)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    assert result.status == WorkerStatus.DEFERRED
    assert result.observations == []
    assert result.error is None
    assert result.checkpoint is not None
    assert "deferred_requires_diarization" in result.checkpoint


def test_diarization_import_job_succeeds() -> None:
    job = make_job(processor_name="diarization_import_v1", source_type=SourceType.AUDIO)
    segments = (
        DiarizationSegmentInput(
            start_ms=0,
            end_ms=500,
            speaker_label="SPEAKER_00",
            confidence=0.5,
            source_segment_id="s1",
        ),
    )
    result = process_job(job, DiarizationImportInput(segments=segments))
    assert result.status == WorkerStatus.SUCCEEDED


def test_whatsapp_job_succeeds() -> None:
    job = make_job(processor_name="whatsapp_export_v1", source_type=SourceType.CHAT)
    data = build_whatsapp_export(["01/01/26, 10:00 - Alice: hi"])
    result = process_job(job, SocialExportInput(platform="whatsapp", data=data))
    assert result.status == WorkerStatus.SUCCEEDED


def test_unknown_processor_name_fails_safely() -> None:
    job = make_job(processor_name="not_a_real_processor", source_type=SourceType.AUDIO)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_processor"


def test_wrong_source_type_fails_safely() -> None:
    job = make_job(processor_name="audio_metadata_v1", source_type=SourceType.CHAT)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "unsupported_source_type"


def test_wrong_input_payload_role_fails_safely() -> None:
    job = make_job(processor_name="whatsapp_export_v1", source_type=SourceType.CHAT)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=b"x"))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "input_role_mismatch"


def test_wrong_social_platform_fails_safely() -> None:
    job = make_job(processor_name="whatsapp_export_v1", source_type=SourceType.CHAT)
    result = process_job(job, SocialExportInput(platform="telegram", data=b"{}"))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert result.error.code == "input_role_mismatch"


def test_failed_result_error_is_safe_and_non_secret() -> None:
    job = make_job(processor_name="audio_metadata_v1", source_type=SourceType.AUDIO)
    secret_filename = "case-alice-9876543210.mp3"
    result = process_job(job, AudioMetadataInput(filename=secret_filename, data=b"junk"))
    assert result.status == WorkerStatus.FAILED
    assert result.error is not None
    assert secret_filename not in result.error.message
    assert "9876543210" not in result.error.message


def test_same_input_yields_same_observation_ids_across_runs() -> None:
    """Scenario 10 extra / determinism: identical normalized input -> identical IDs."""
    job = make_job(processor_name="audio_metadata_v1", source_type=SourceType.AUDIO)
    payload = AudioMetadataInput(filename="a.wav", data=build_wav_bytes())
    first = process_job(job, payload)
    second = process_job(job, payload)
    assert [o.observation_id for o in first.observations] == [
        o.observation_id for o in second.observations
    ]


def test_worker_result_round_trips_through_frozen_contract() -> None:
    job = make_job(processor_name="audio_metadata_v1", source_type=SourceType.AUDIO)
    result = process_job(job, AudioMetadataInput(filename="a.wav", data=build_wav_bytes()))
    reloaded = WorkerResultV1.model_validate_json(result.model_dump_json())
    assert reloaded == result
