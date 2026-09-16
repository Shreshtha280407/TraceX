"""Phase 7 Part 4 audio/social-benchmark adapter tests.

Every test here uses a `Fake*Engine` or the real, unmodified, deterministic
`social/identifiers.py` extractor -- no real faster-whisper/pyannote/
fastText model, GPU, or dataset is required. Every value used is
synthetic and invented for this test file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import SourceLocator
from app.modules.communication_processing.audio_social_benchmark_adapters import (
    FAKE_EXECUTION_FAILURE_FILENAME,
    FAKE_EXECUTION_FAILURE_TEXT,
    FAKE_UNREADABLE_FILENAME,
    AsrEngine,
    AudioBenchmarkSample,
    DeterministicSocialExtractionEngine,
    DiarizationBenchmarkSample,
    DiarizationEngine,
    FakeAsrEngine,
    FakeDiarizationEngine,
    FakeLanguageIdEngine,
    FakeSocialExtractionEngine,
    FakeVadEngine,
    LanguageIdEngine,
    SocialBenchmarkSample,
    SocialExtractionEngine,
    VadEngine,
    observation_for_social_mention,
    observations_for_asr_result,
    observations_for_diarization_result,
    run_asr_benchmark,
    run_diarization_benchmark,
    run_language_id_benchmark,
    run_social_extraction_benchmark,
    run_vad_benchmark,
)
from app.modules.communication_processing.audio_social_benchmark_metrics import (
    SpeakerTurn,
    TimeInterval,
)
from app.modules.communication_processing.models import (
    DiarizationSegmentInput,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.social.common import ChatMessageRecord
from app.modules.communication_processing.social.identifiers import extract_mentioned_identifiers
from app.modules.evaluation.models import BenchmarkRunStatus, SplitId

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_fake_asr_engine_produces_a_valid_succeeded_run() -> None:
    segment = TranscriptSegmentInput(
        start_ms=0,
        end_ms=1000,
        text="hello world",
        language_hint="en",
        confidence=0.9,
        source_segment_id="s1",
    )
    engine = FakeAsrEngine(fixed_segments=(segment,))
    sample = AudioBenchmarkSample(
        sample_id="a1",
        audio_bytes=b"synthetic",
        filename="a1.wav",
        total_duration_ms=1000,
        reference_text="hello world",
    )
    run = run_asr_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="faster-whisper-small",
        dataset_id="common_voice_indic",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["wer"] == 0.0
    assert run.metrics["cer"] == 0.0
    assert run.artifact_sha256 == "0" * 64
    assert run.hardware_profile == "fake-cpu"


def test_asr_benchmark_rejects_an_empty_sample_list() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        run_asr_benchmark(
            engine=FakeAsrEngine(),
            samples=[],
            candidate_id="faster-whisper-small",
            dataset_id="common_voice_indic",
            split_id=SplitId.DEVELOPMENT,
            inference_config_hash="a" * 64,
        )


def test_asr_benchmark_categorizes_a_marker_failure_safely() -> None:
    engine = FakeAsrEngine()
    sample = AudioBenchmarkSample(
        sample_id="bad", audio_bytes=b"x", filename=FAKE_UNREADABLE_FILENAME, total_duration_ms=1000
    )
    run = run_asr_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="faster-whisper-small",
        dataset_id="common_voice_indic",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_unreadable"] == 1
    assert run.artifact_sha256 is None


def test_asr_benchmark_fails_safely_when_every_sample_fails() -> None:
    engine = FakeAsrEngine()
    samples = [
        AudioBenchmarkSample(
            sample_id="bad1",
            audio_bytes=b"x",
            filename=FAKE_UNREADABLE_FILENAME,
            total_duration_ms=1000,
        ),
        AudioBenchmarkSample(
            sample_id="bad2",
            audio_bytes=b"x",
            filename=FAKE_EXECUTION_FAILURE_FILENAME,
            total_duration_ms=1000,
        ),
    ]
    run = run_asr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id="faster-whisper-small",
        dataset_id="common_voice_indic",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="a" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_success_count"] == 0


def test_fake_vad_engine_produces_a_valid_succeeded_run() -> None:
    engine = FakeVadEngine(fixed_intervals=(TimeInterval(0, 1000),))
    sample = AudioBenchmarkSample(
        sample_id="v1",
        audio_bytes=b"x",
        filename="v1.wav",
        total_duration_ms=2000,
        ground_truth_speech_intervals=(TimeInterval(0, 1000),),
    )
    run = run_vad_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="silero-vad-v6",
        dataset_id="ami_meeting_corpus",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="b" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["voice_activity_detection_accuracy"] == 1.0


def test_vad_metric_is_unavailable_without_ground_truth_rather_than_fabricated() -> None:
    engine = FakeVadEngine(fixed_intervals=(TimeInterval(0, 1000),))
    sample = AudioBenchmarkSample(
        sample_id="v1", audio_bytes=b"x", filename="v1.wav", total_duration_ms=2000
    )
    run = run_vad_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="silero-vad-v6",
        dataset_id="ami_meeting_corpus",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="b" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["voice_activity_detection_accuracy"] is None


def test_fake_diarization_engine_produces_a_valid_succeeded_run() -> None:
    segment = DiarizationSegmentInput(
        start_ms=0, end_ms=1000, speaker_label="speaker_0", confidence=0.9, source_segment_id="d1"
    )
    engine = FakeDiarizationEngine(fixed_segments=(segment,))
    sample = DiarizationBenchmarkSample(
        sample_id="d1",
        audio_bytes=b"x",
        filename="d1.wav",
        total_duration_ms=1000,
        ground_truth_turns=(SpeakerTurn("spk_A", 0, 1000),),
    )
    run = run_diarization_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="deterministic-diarization-fallback",
        dataset_id="ami_meeting_corpus",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="c" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["der"] == 0.0
    assert run.metrics["jer"] is None


def test_diarization_benchmark_categorizes_a_marker_failure_safely() -> None:
    engine = FakeDiarizationEngine()
    sample = DiarizationBenchmarkSample(
        sample_id="bad", audio_bytes=b"x", filename=FAKE_UNREADABLE_FILENAME, total_duration_ms=1000
    )
    run = run_diarization_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="deterministic-diarization-fallback",
        dataset_id="ami_meeting_corpus",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="c" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_unreadable"] == 1


def test_fake_language_id_engine_produces_a_valid_succeeded_run() -> None:
    engine = FakeLanguageIdEngine(fixed_language="hi")
    sample = AudioBenchmarkSample(
        sample_id="l1",
        audio_bytes=b"x",
        filename="l1.wav",
        total_duration_ms=1000,
        reference_language="hi",
    )
    run = run_language_id_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="fasttext-lid176",
        dataset_id="common_voice_indic",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="d" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["accuracy"] == 1.0


def test_language_id_accuracy_is_unavailable_without_a_reference_label() -> None:
    engine = FakeLanguageIdEngine(fixed_language="hi")
    sample = AudioBenchmarkSample(
        sample_id="l1", audio_bytes=b"x", filename="l1.wav", total_duration_ms=1000
    )
    run = run_language_id_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="fasttext-lid176",
        dataset_id="common_voice_indic",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="d" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["accuracy"] is None


def test_fake_social_extraction_engine_produces_a_valid_succeeded_run() -> None:
    engine = FakeSocialExtractionEngine(fixed_mentions=(("phone_number", "9876543210"),))
    sample = SocialBenchmarkSample(
        sample_id="m1",
        message_text="call me at 9876543210",
        expected_mentions=(("phone_number", "9876543210"),),
    )
    run = run_social_extraction_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="existing-deterministic-social-parsers",
        dataset_id="vast_social_text",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="e" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["extraction_f1"] == 1.0


def test_social_extraction_metrics_never_contain_the_raw_message_text() -> None:
    engine = FakeSocialExtractionEngine(fixed_mentions=(("phone_number", "9876543210"),))
    sample = SocialBenchmarkSample(
        sample_id="m1",
        message_text="call me at 9876543210",
        expected_mentions=(("phone_number", "9876543210"),),
    )
    run = run_social_extraction_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="existing-deterministic-social-parsers",
        dataset_id="vast_social_text",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="e" * 64,
        now=_NOW,
    )
    serialized = run.model_dump_json()
    assert "9876543210" not in serialized
    assert "call me at" not in serialized


def test_social_extraction_benchmark_categorizes_a_marker_failure_safely() -> None:
    engine = FakeSocialExtractionEngine()
    sample = SocialBenchmarkSample(sample_id="bad", message_text=FAKE_EXECUTION_FAILURE_TEXT)
    run = run_social_extraction_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="existing-deterministic-social-parsers",
        dataset_id="vast_social_text",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="e" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["sample_failure_count_execution_failure"] == 1


def test_deterministic_social_extraction_engine_reuses_the_real_extractor() -> None:
    engine = DeterministicSocialExtractionEngine(model_sha256="f" * 64)
    result = engine.extract("call me at 9876543210", message_id="m1")
    assert result.mentions == (("phone_number", "9876543210"),)
    assert result.model_sha256 == "f" * 64


def test_deterministic_social_extraction_engine_matches_the_real_extractor_directly() -> None:
    """The benchmark engine must not diverge from the exact production function."""
    record = ChatMessageRecord(
        platform="benchmark",
        conversation_id=None,
        message_id="m1",
        sender=None,
        participants=(),
        timestamp_raw=None,
        timestamp_utc=None,
        timestamp_source_timezone=None,
        text="call me at 9876543210",
        reply_to=None,
        locator=SourceLocator(json_path="$.messages.m1"),
    )
    direct_mentions = extract_mentioned_identifiers(record)
    engine = DeterministicSocialExtractionEngine(model_sha256="f" * 64)
    engine_result = engine.extract("call me at 9876543210", message_id="m1")
    assert [m.text.strip().lower() for m in direct_mentions] == [
        pair[1] for pair in engine_result.mentions
    ]


def test_real_social_engine_run_produces_a_valid_succeeded_run_with_no_lazy_import_needed() -> None:
    """`existing-deterministic-social-parsers` needs no optional package at all."""
    engine = DeterministicSocialExtractionEngine(model_sha256="f" * 64)
    sample = SocialBenchmarkSample(
        sample_id="m1",
        message_text="call me at 9876543210",
        expected_mentions=(("phone_number", "9876543210"),),
    )
    run = run_social_extraction_benchmark(
        engine=engine,
        samples=[sample],
        candidate_id="existing-deterministic-social-parsers",
        dataset_id="vast_social_text",
        split_id=SplitId.DEVELOPMENT,
        inference_config_hash="e" * 64,
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.artifact_sha256 == "f" * 64


def test_fake_engines_are_interchangeable_through_the_typed_protocols() -> None:
    asr: AsrEngine = FakeAsrEngine()
    vad: VadEngine = FakeVadEngine()
    diarization: DiarizationEngine = FakeDiarizationEngine()
    language_id: LanguageIdEngine = FakeLanguageIdEngine()
    social: SocialExtractionEngine = FakeSocialExtractionEngine()
    assert asr.transcribe(b"x", filename="a.wav").backend == "fake-cpu"
    assert vad.detect_speech(b"x", filename="a.wav", total_duration_ms=1000).backend == "fake-cpu"
    assert diarization.diarize(b"x", filename="a.wav").backend == "fake-cpu"
    assert language_id.identify(b"x", filename="a.wav").backend == "fake-cpu"
    assert social.extract("hello", message_id="m1").backend == "fake-cpu"


# --- Canonical observation/provenance compatibility, and speaker-label safety --


def test_accepted_asr_output_builds_a_valid_canonical_observation() -> None:
    segment = TranscriptSegmentInput(
        start_ms=0,
        end_ms=1000,
        text="hello world",
        language_hint="en",
        confidence=0.9,
        source_segment_id="s1",
    )
    observations = observations_for_asr_result(
        (segment,), case_id=uuid4(), evidence_id=uuid4(), created_at=_NOW
    )
    assert len(observations) == 1
    assert observations[0].observation_type == "transcript_segment"
    assert observations[0].extraction_confidence == 0.9


def test_accepted_diarization_output_never_becomes_a_cross_record_identity_link() -> None:
    segment = DiarizationSegmentInput(
        start_ms=0, end_ms=1000, speaker_label="speaker_0", confidence=0.9, source_segment_id="d1"
    )
    observations = observations_for_diarization_result(
        (segment,), case_id=uuid4(), evidence_id=uuid4(), created_at=_NOW
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation.observation_type == "diarization_speaker_turn"
    # A recording-local speaker label becomes only a
    # `speaker_label_local`-hinted entity mention -- production's own,
    # unmodified typing -- never a resolvable cross-record/cross-case
    # identity claim.
    assert observation.extracted_entities[0].entity_type_hint == "speaker_label_local"
    assert observation.extracted_entities[0].text == "speaker_0"


def test_accepted_social_mention_builds_a_valid_canonical_observation() -> None:
    record = ChatMessageRecord(
        platform="benchmark",
        conversation_id=None,
        message_id="m1",
        sender=None,
        participants=(),
        timestamp_raw=None,
        timestamp_utc=None,
        timestamp_source_timezone=None,
        text="call me at 9876543210",
        reply_to=None,
        locator=SourceLocator(json_path="$.messages.m1"),
    )
    mentions = extract_mentioned_identifiers(record)
    observation = observation_for_social_mention(
        mentions[0], case_id=uuid4(), evidence_id=uuid4(), created_at=_NOW
    )
    assert observation.observation_type == "phone_number"
