"""End-to-end pipeline across every processor this module owns.

Scenario 31: this whole module needs no Docker, GPU, downloaded model, or
network access — every processor here operates purely on in-memory bytes
and typed dataclasses. This test exercises all seven processors in one
pass to prove that end to end, and lives in `tests/integration/` (rather
than `tests/unit/`) because it's a multi-component pipeline check, not a
single-unit test — but it needs no external service and therefore never
self-skips.
"""

from __future__ import annotations

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.modules.communication_processing.models import (
    AudioMetadataInput,
    DiarizationImportInput,
    DiarizationSegmentInput,
    SocialExportInput,
    TranscriptImportInput,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.worker import process_job
from tests.fixtures.communication_processing.builders import (
    build_generic_json_export,
    build_instagram_export,
    build_telegram_export,
    build_wav_bytes,
    build_whatsapp_export,
)
from tests.fixtures.communication_processing.factory import make_job


def test_every_processor_succeeds_end_to_end() -> None:
    cases: list[tuple[str, object]] = [
        (
            "audio_metadata_v1",
            AudioMetadataInput(filename="call.wav", data=build_wav_bytes(duration_seconds=1.5)),
        ),
        (
            "transcript_import_v1",
            TranscriptImportInput(
                segments=(
                    TranscriptSegmentInput(
                        start_ms=0,
                        end_ms=1200,
                        text="synthetic transcript segment",
                        language_hint="en",
                        confidence=0.72,
                        source_segment_id="t1",
                    ),
                )
            ),
        ),
        (
            "diarization_import_v1",
            DiarizationImportInput(
                segments=(
                    DiarizationSegmentInput(
                        start_ms=0,
                        end_ms=1200,
                        speaker_label="SPEAKER_00",
                        confidence=0.81,
                        source_segment_id="d1",
                    ),
                )
            ),
        ),
        (
            "whatsapp_export_v1",
            SocialExportInput(
                platform="whatsapp",
                data=build_whatsapp_export(["01/01/26, 10:00 - Alice: synthetic message"]),
            ),
        ),
        (
            "telegram_export_v1",
            SocialExportInput(
                platform="telegram",
                data=build_telegram_export(
                    messages=[
                        {
                            "id": 1,
                            "type": "message",
                            "date": "2026-01-01T10:00:00",
                            "date_unixtime": "1767261600",
                            "from": "Alice",
                            "text": "synthetic telegram message",
                        }
                    ]
                ),
            ),
        ),
        (
            "instagram_export_v1",
            SocialExportInput(
                platform="instagram",
                data=build_instagram_export(
                    messages=[
                        {
                            "sender_name": "alice",
                            "timestamp_ms": 1767261600000,
                            "content": "synthetic instagram message",
                        }
                    ]
                ),
            ),
        ),
        (
            "generic_social_json_v1",
            SocialExportInput(
                platform="generic_json",
                data=build_generic_json_export(
                    records=[
                        {
                            "message_id": "m1",
                            "timestamp": "2026-01-01T10:00:00Z",
                            "text": "synthetic generic record",
                        }
                    ]
                ),
            ),
        ),
    ]

    audio_processors = {"audio_metadata_v1", "transcript_import_v1", "diarization_import_v1"}

    for processor_name, payload in cases:
        source_type = SourceType.AUDIO if processor_name in audio_processors else SourceType.CHAT
        job = make_job(processor_name=processor_name, source_type=source_type)
        result = process_job(job, payload)  # type: ignore[arg-type]
        assert result.status == WorkerStatus.SUCCEEDED, (processor_name, result.error)
        assert result.observations, processor_name
        for observation in result.observations:
            assert observation.case_id == job.case_id
            assert observation.evidence_id == job.evidence_id
