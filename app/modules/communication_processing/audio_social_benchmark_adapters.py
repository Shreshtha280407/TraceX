"""Phase 7 Part 4 audio/social-benchmark adapters: ASR, VAD, diarization,
language identification, and social/chat structured extraction.

Reuses the exact existing canonical segment types this module's own
production code already validates and projects into observations --
`TranscriptSegmentInput`/`DiarizationSegmentInput`
(`audio/transcript_import.py`/`audio/diarization_import.py`) and
`RawMention` (`social/identifiers.py`) -- so an accepted benchmark result
can flow through the identical, unmodified
`transcript_segments_to_mentions`/`diarization_segments_to_mentions`/
`mention_to_observation` seam production ASR/diarization import already
uses, with no alternate observation format.

No real faster-whisper/pyannote/fastText model is downloaded, imported, or
run here. Unit tests exercise this module exclusively through the `Fake*`
engines below and never require a GPU, a model download, or a real
Common-Voice/AMI/VAST dataset.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from app.contracts.common import SourceLocator
from app.contracts.observation import ObservationV1
from app.modules.communication_processing.audio.diarization_import import (
    diarization_segments_to_mentions,
)
from app.modules.communication_processing.audio.transcript_import import (
    transcript_segments_to_mentions,
)
from app.modules.communication_processing.audio_social_benchmark_metrics import (
    SpeakerTurn,
    TimeInterval,
    character_error_rate,
    diarization_error_rate,
    median,
    peak_memory_mb,
    percentile,
    real_time_factor,
    social_extraction_prf,
    speaker_count_error,
    speaker_turn_quality,
    vad_precision_recall_f1,
    voice_activity_detection_accuracy,
    word_error_rate,
)
from app.modules.communication_processing.models import (
    DiarizationSegmentInput,
    ProcessorProfile,
    RawMention,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.provenance import mention_to_observation
from app.modules.communication_processing.social.common import ChatMessageRecord
from app.modules.communication_processing.social.identifiers import extract_mentioned_identifiers
from app.modules.evaluation.models import BenchmarkRunStatus, BenchmarkRunV1, CandidateTask, SplitId

RUNTIME_ENVIRONMENT = "phase7-part4-audio-social-benchmark-cli-v1"


class BenchmarkArtifactUnavailableError(Exception):
    """A local dataset/model artifact could not be found or loaded.

    Caught by `audio_social_benchmark.py` and converted into a safe
    `BenchmarkRunStatus.UNAVAILABLE` result -- never propagates to the CLI
    as an unhandled exception.
    """


class EngineError(Exception):
    """A named, safe per-sample engine failure -- never a raw third-party exception."""

    CATEGORIES = frozenset({"unreadable", "unsupported_format", "execution_failure"})

    def __init__(self, category: str, message: str) -> None:
        if category not in self.CATEGORIES:
            raise ValueError(f"unknown EngineError category '{category}'")
        super().__init__(message)
        self.category = category
        self.message = message


# --- Engine protocols and result wrappers --------------------------------


@dataclass(frozen=True)
class AsrEngineResult:
    segments: tuple[TranscriptSegmentInput, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class AsrEngine(Protocol):
    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrEngineResult: ...


@dataclass(frozen=True)
class VadEngineResult:
    speech_intervals: tuple[TimeInterval, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class VadEngine(Protocol):
    def detect_speech(
        self, audio_bytes: bytes, *, filename: str, total_duration_ms: int
    ) -> VadEngineResult: ...


@dataclass(frozen=True)
class DiarizationEngineResult:
    segments: tuple[DiarizationSegmentInput, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class DiarizationEngine(Protocol):
    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationEngineResult: ...


@dataclass(frozen=True)
class LanguageIdEngineResult:
    predicted_language: str
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class LanguageIdEngine(Protocol):
    def identify(self, audio_bytes: bytes, *, filename: str) -> LanguageIdEngineResult: ...


#: A social-extraction mention as a safe `(observation_type, normalized_value)`
#: pair -- never the raw message text, never a full `RawMention`.
SocialMention = tuple[str, str]


@dataclass(frozen=True)
class SocialExtractionEngineResult:
    mentions: tuple[SocialMention, ...]
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


@runtime_checkable
class SocialExtractionEngine(Protocol):
    def extract(self, message_text: str, *, message_id: str) -> SocialExtractionEngineResult: ...


# --- Fake engines (tests only) --------------------------------------------

#: Marker filenames a `Fake*Engine` (audio-based) recognizes to simulate a
#: per-sample failure -- never a real audio format signal.
FAKE_UNREADABLE_FILENAME = "__BENCHMARK_FAKE_UNREADABLE__.wav"
FAKE_UNSUPPORTED_FORMAT_FILENAME = "__BENCHMARK_FAKE_UNSUPPORTED__.wav"
FAKE_EXECUTION_FAILURE_FILENAME = "__BENCHMARK_FAKE_EXEC_FAIL__.wav"

#: Marker message text `FakeSocialExtractionEngine` recognizes similarly.
FAKE_UNREADABLE_TEXT = "__BENCHMARK_FAKE_UNREADABLE__"
FAKE_UNSUPPORTED_FORMAT_TEXT = "__BENCHMARK_FAKE_UNSUPPORTED__"
FAKE_EXECUTION_FAILURE_TEXT = "__BENCHMARK_FAKE_EXEC_FAIL__"


def _raise_for_marker_filename(filename: str) -> None:
    if filename == FAKE_UNREADABLE_FILENAME:
        raise EngineError("unreadable", "sample could not be decoded")
    if filename == FAKE_UNSUPPORTED_FORMAT_FILENAME:
        raise EngineError("unsupported_format", "sample format is not supported")
    if filename == FAKE_EXECUTION_FAILURE_FILENAME:
        raise EngineError("execution_failure", "engine execution failed")


@dataclass(frozen=True)
class FakeAsrEngine:
    """Deterministic, test-only: returns a fixed transcript for every call."""

    fixed_segments: tuple[TranscriptSegmentInput, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-asr"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrEngineResult:  # noqa: ARG002
        _raise_for_marker_filename(filename)
        return AsrEngineResult(
            segments=self.fixed_segments,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class FakeVadEngine:
    """Deterministic, test-only: returns fixed speech intervals for every call."""

    fixed_intervals: tuple[TimeInterval, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-vad"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def detect_speech(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        total_duration_ms: int,  # noqa: ARG002
    ) -> VadEngineResult:
        _raise_for_marker_filename(filename)
        return VadEngineResult(
            speech_intervals=self.fixed_intervals,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class FakeDiarizationEngine:
    """Deterministic, test-only: returns fixed speaker turns for every call."""

    fixed_segments: tuple[DiarizationSegmentInput, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-diarization"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationEngineResult:  # noqa: ARG002
        _raise_for_marker_filename(filename)
        return DiarizationEngineResult(
            segments=self.fixed_segments,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class FakeLanguageIdEngine:
    """Deterministic, test-only: returns a fixed predicted language for every call."""

    fixed_language: str = "hi"
    backend: str = "fake-cpu"
    model_name: str = "fake-lid"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def identify(self, audio_bytes: bytes, *, filename: str) -> LanguageIdEngineResult:  # noqa: ARG002
        _raise_for_marker_filename(filename)
        return LanguageIdEngineResult(
            predicted_language=self.fixed_language,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class FakeSocialExtractionEngine:
    """Deterministic, test-only: returns fixed mentions for every call."""

    fixed_mentions: tuple[SocialMention, ...] = ()
    backend: str = "fake-cpu"
    model_name: str = "fake-social-extraction"
    model_version: str = "fake-1"
    model_sha256: str = "0" * 64

    def extract(self, message_text: str, *, message_id: str) -> SocialExtractionEngineResult:  # noqa: ARG002
        if message_text == FAKE_UNREADABLE_TEXT:
            raise EngineError("unreadable", "sample could not be decoded")
        if message_text == FAKE_UNSUPPORTED_FORMAT_TEXT:
            raise EngineError("unsupported_format", "sample format is not supported")
        if message_text == FAKE_EXECUTION_FAILURE_TEXT:
            raise EngineError("execution_failure", "engine execution failed")
        return SocialExtractionEngineResult(
            mentions=self.fixed_mentions,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


# --- The real deterministic social-extraction engine (no lazy import) ----


@dataclass(frozen=True)
class DeterministicSocialExtractionEngine:
    """Wraps the existing, unmodified `social/identifiers.py::extract_mentioned_identifiers`.

    No lazy import guard needed -- this candidate is the project's own
    already-shipped deterministic code, not a downloaded model. `model_sha256`
    is the caller-supplied SHA-256 of the local benchmark input file (there
    is no model weight for a deterministic-rules baseline to hash instead --
    the same convention Phase 7 Part 2's CDR/finance baseline established).
    """

    model_sha256: str
    backend: str = "cpu"
    model_name: str = "existing-deterministic-social-parsers"
    model_version: str = "phase-1-4-baseline"

    def extract(self, message_text: str, *, message_id: str) -> SocialExtractionEngineResult:
        record = ChatMessageRecord(
            platform="benchmark",
            conversation_id=None,
            message_id=message_id,
            sender=None,
            participants=(),
            timestamp_raw=None,
            timestamp_utc=None,
            timestamp_source_timezone=None,
            text=message_text,
            reply_to=None,
            locator=SourceLocator(json_path=f"$.messages.{message_id}"),
        )
        try:
            mentions = extract_mentioned_identifiers(record)
        except Exception as exc:  # noqa: BLE001 - a malformed sample is a safe per-sample failure
            raise EngineError("execution_failure", "deterministic extraction failed") from exc
        pairs = tuple(
            (mention.observation_type, mention.text.strip().lower()) for mention in mentions
        )
        return SocialExtractionEngineResult(
            mentions=pairs,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


# --- Benchmark sample types -------------------------------------------------


@dataclass(frozen=True)
class AudioBenchmarkSample:
    """One recording for ASR/VAD/language-ID benchmarking.

    `audio_bytes` must be real (tiny, synthetic) WAV bytes in tests --
    never a real recording. Ground-truth fields are `None` whenever this
    dataset's own labels don't support that specific metric.
    """

    sample_id: str
    audio_bytes: bytes
    filename: str
    total_duration_ms: int
    reference_text: str | None = None
    reference_language: str | None = None
    ground_truth_speech_intervals: tuple[TimeInterval, ...] | None = None


@dataclass(frozen=True)
class DiarizationBenchmarkSample:
    sample_id: str
    audio_bytes: bytes
    filename: str
    total_duration_ms: int
    ground_truth_turns: tuple[SpeakerTurn, ...] | None = None


@dataclass(frozen=True)
class SocialBenchmarkSample:
    sample_id: str
    message_text: str
    expected_mentions: tuple[SocialMention, ...] | None = None


# --- Aggregation helpers ---------------------------------------------------


@dataclass
class _RunAccumulator:
    latencies_ms: list[float] = field(default_factory=list)
    success_count: int = 0
    failure_by_category: dict[str, int] = field(default_factory=dict)


def _record_failure(accumulator: _RunAccumulator, category: str) -> None:
    accumulator.failure_by_category[category] = accumulator.failure_by_category.get(category, 0) + 1


def _base_metrics(accumulator: _RunAccumulator, sample_count: int) -> dict[str, float | int | None]:
    metrics: dict[str, float | int | None] = {
        "latency_ms": median(accumulator.latencies_ms),
        "ram_mb": peak_memory_mb(),
        "sample_count": sample_count,
        "sample_success_count": accumulator.success_count,
        "sample_failure_count": sample_count - accumulator.success_count,
        "latency_p50_ms": percentile(accumulator.latencies_ms, 50.0),
        "latency_p95_ms": percentile(accumulator.latencies_ms, 95.0),
    }
    for category, count in accumulator.failure_by_category.items():
        metrics[f"sample_failure_count_{category}"] = count
    return metrics


def _new_run_id(dataset_id: str, candidate_id: str) -> str:
    return f"{dataset_id}-{candidate_id}-{uuid4().hex[:12]}"


def _failed_run(
    *,
    task: CandidateTask,
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    metrics: dict[str, float | int | None],
    sample_count: int,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=_new_run_id(dataset_id, candidate_id),
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task=task,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile="unavailable",
        inference_config_hash=inference_config_hash,
        artifact_sha256=None,
        metrics=metrics,
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.FAILED,
        failure_reason_safe=f"all {sample_count} sample(s) failed",
    )


def _succeeded_run(
    *,
    task: CandidateTask,
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    metrics: dict[str, float | int | None],
    backend: str,
    model_sha256: str | None,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=_new_run_id(dataset_id, candidate_id),
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task=task,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile=backend,
        inference_config_hash=inference_config_hash,
        artifact_sha256=model_sha256,
        metrics=metrics,
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.SUCCEEDED,
        failure_reason_safe=None,
    )


# --- ASR --------------------------------------------------------------


def run_asr_benchmark(
    *,
    engine: AsrEngine,
    samples: Sequence[AudioBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_asr_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    wer_values: list[float] = []
    cer_values: list[float] = []
    rtf_values: list[float] = []
    engine_result: AsrEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.transcribe(sample.audio_bytes, filename=sample.filename)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        accumulator.latencies_ms.append(elapsed_ms)
        accumulator.success_count += 1
        recognized_text = " ".join(segment.text for segment in engine_result.segments)
        if sample.reference_text is not None:
            wer = word_error_rate(sample.reference_text, recognized_text)
            cer = character_error_rate(sample.reference_text, recognized_text)
            if wer is not None:
                wer_values.append(wer)
            if cer is not None:
                cer_values.append(cer)
        rtf = real_time_factor(
            processing_time_ms=elapsed_ms, audio_duration_ms=sample.total_duration_ms
        )
        if rtf is not None:
            rtf_values.append(rtf)

    metrics: dict[str, float | int | None] = {
        "wer": median(wer_values),
        "cer": median(cer_values),
        "real_time_factor": median(rtf_values),
        "vram_mb": None,
        **_base_metrics(accumulator, len(samples)),
    }
    if accumulator.success_count == 0:
        return _failed_run(
            task=CandidateTask.ASR,
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            metrics=metrics,
            sample_count=len(samples),
            inference_config_hash=inference_config_hash,
            now=now,
        )
    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return _succeeded_run(
        task=CandidateTask.ASR,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        metrics=metrics,
        backend=engine_result.backend,
        model_sha256=engine_result.model_sha256,
        inference_config_hash=inference_config_hash,
        now=now,
    )


# --- VAD ----------------------------------------------------------------


def run_vad_benchmark(
    *,
    engine: VadEngine,
    samples: Sequence[AudioBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_vad_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    accuracy_values: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    engine_result: VadEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.detect_speech(
                sample.audio_bytes,
                filename=sample.filename,
                total_duration_ms=sample.total_duration_ms,
            )
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        if sample.ground_truth_speech_intervals is not None:
            accuracy = voice_activity_detection_accuracy(
                engine_result.speech_intervals,
                sample.ground_truth_speech_intervals,
                total_duration_ms=sample.total_duration_ms,
            )
            precision, recall, f1 = vad_precision_recall_f1(
                engine_result.speech_intervals,
                sample.ground_truth_speech_intervals,
                total_duration_ms=sample.total_duration_ms,
            )
            if accuracy is not None:
                accuracy_values.append(accuracy)
            if precision is not None:
                precisions.append(precision)
            if recall is not None:
                recalls.append(recall)
            if f1 is not None:
                f1s.append(f1)

    metrics: dict[str, float | int | None] = {
        "voice_activity_detection_accuracy": median(accuracy_values),
        "vad_precision": median(precisions),
        "vad_recall": median(recalls),
        "vad_f1": median(f1s),
        **_base_metrics(accumulator, len(samples)),
    }
    if accumulator.success_count == 0:
        return _failed_run(
            task=CandidateTask.VAD,
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            metrics=metrics,
            sample_count=len(samples),
            inference_config_hash=inference_config_hash,
            now=now,
        )
    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return _succeeded_run(
        task=CandidateTask.VAD,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        metrics=metrics,
        backend=engine_result.backend,
        model_sha256=engine_result.model_sha256,
        inference_config_hash=inference_config_hash,
        now=now,
    )


# --- Diarization ----------------------------------------------------------


def run_diarization_benchmark(
    *,
    engine: DiarizationEngine,
    samples: Sequence[DiarizationBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_diarization_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    der_values: list[float] = []
    turn_quality_values: list[float] = []
    speaker_count_errors: list[float] = []
    engine_result: DiarizationEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.diarize(sample.audio_bytes, filename=sample.filename)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        predicted_turns = tuple(
            SpeakerTurn(speaker_label=s.speaker_label, start_ms=s.start_ms, end_ms=s.end_ms)
            for s in engine_result.segments
        )
        if sample.ground_truth_turns is not None:
            der = diarization_error_rate(
                predicted_turns,
                sample.ground_truth_turns,
                total_duration_ms=sample.total_duration_ms,
            )
            quality = speaker_turn_quality(predicted_turns, sample.ground_truth_turns)
            count_error = speaker_count_error(predicted_turns, sample.ground_truth_turns)
            if der is not None:
                der_values.append(der)
            if quality is not None:
                turn_quality_values.append(quality)
            if count_error is not None:
                speaker_count_errors.append(float(count_error))

    metrics: dict[str, float | int | None] = {
        "der": median(der_values),
        "speaker_turn_quality": median(turn_quality_values),
        "speaker_count_error": median(speaker_count_errors),
        # Always None -- see audio_social_benchmark_metrics.jaccard_error_rate's docstring.
        "jer": None,
        "vram_mb": None,
        **_base_metrics(accumulator, len(samples)),
    }
    if accumulator.success_count == 0:
        return _failed_run(
            task=CandidateTask.DIARIZATION,
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            metrics=metrics,
            sample_count=len(samples),
            inference_config_hash=inference_config_hash,
            now=now,
        )
    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return _succeeded_run(
        task=CandidateTask.DIARIZATION,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        metrics=metrics,
        backend=engine_result.backend,
        model_sha256=engine_result.model_sha256,
        inference_config_hash=inference_config_hash,
        now=now,
    )


# --- Language identification ----------------------------------------------


def run_language_id_benchmark(
    *,
    engine: LanguageIdEngine,
    samples: Sequence[AudioBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_language_id_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    correct = 0
    total_with_reference = 0
    engine_result: LanguageIdEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.identify(sample.audio_bytes, filename=sample.filename)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        if sample.reference_language is not None:
            total_with_reference += 1
            if engine_result.predicted_language == sample.reference_language:
                correct += 1

    metrics: dict[str, float | int | None] = {
        "accuracy": (correct / total_with_reference) if total_with_reference > 0 else None,
        **_base_metrics(accumulator, len(samples)),
    }
    if accumulator.success_count == 0:
        return _failed_run(
            task=CandidateTask.LANGUAGE_IDENTIFICATION,
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            metrics=metrics,
            sample_count=len(samples),
            inference_config_hash=inference_config_hash,
            now=now,
        )
    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return _succeeded_run(
        task=CandidateTask.LANGUAGE_IDENTIFICATION,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        metrics=metrics,
        backend=engine_result.backend,
        model_sha256=engine_result.model_sha256,
        inference_config_hash=inference_config_hash,
        now=now,
    )


# --- Social/chat structured extraction ------------------------------------


def run_social_extraction_benchmark(
    *,
    engine: SocialExtractionEngine,
    samples: Sequence[SocialBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    if not samples:
        raise ValueError("run_social_extraction_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    accumulator = _RunAccumulator()
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    engine_result: SocialExtractionEngineResult | None = None

    for sample in samples:
        started = time.perf_counter()
        try:
            engine_result = engine.extract(sample.message_text, message_id=sample.sample_id)
        except EngineError as exc:
            _record_failure(accumulator, exc.category)
            continue
        accumulator.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        accumulator.success_count += 1
        if sample.expected_mentions is not None:
            precision, recall, f1 = social_extraction_prf(
                sample.expected_mentions, engine_result.mentions
            )
            if precision is not None:
                precisions.append(precision)
            if recall is not None:
                recalls.append(recall)
            if f1 is not None:
                f1s.append(f1)

    metrics: dict[str, float | int | None] = {
        "extraction_precision": median(precisions),
        "extraction_recall": median(recalls),
        "extraction_f1": median(f1s),
        # This harness's own local social-text fixtures are all Latin-script
        # (invented, synthetic message text) -- `language_handling_score`
        # needs a labelled non-Latin fixture to compute a real value; see
        # docs/qa/known-limitations.md.
        "language_handling_score": None,
        **_base_metrics(accumulator, len(samples)),
    }
    if accumulator.success_count == 0:
        return _failed_run(
            task=CandidateTask.SOCIAL_TEXT_EXTRACTION,
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            metrics=metrics,
            sample_count=len(samples),
            inference_config_hash=inference_config_hash,
            now=now,
        )
    assert engine_result is not None  # noqa: S101 - success_count > 0 guarantees at least one result
    return _succeeded_run(
        task=CandidateTask.SOCIAL_TEXT_EXTRACTION,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        metrics=metrics,
        backend=engine_result.backend,
        model_sha256=engine_result.model_sha256,
        inference_config_hash=inference_config_hash,
        now=now,
    )


# --- Canonical observation/provenance compatibility proof -----------------
#
# Not called anywhere in `run_*_benchmark` above -- a benchmark result is
# evaluation metadata, never primary evidence, and this module creates no
# new raw-evidence persistence path. These exist solely to prove (and be
# tested proving) that an accepted ASR/diarization/social result *could*
# flow through the exact same canonical `ObservationV1`/provenance seam
# production import/extraction already uses, with no alternate format.

_BENCHMARK_ASR_PROFILE = ProcessorProfile(
    name="phase7_part4_asr_benchmark",
    version="1.0.0",
    description="Phase 7 Part 4 ASR benchmark canonical-observation compatibility proof",
    observation_types=("transcript_segment",),
    confidence_rule="pass-through",
)
_BENCHMARK_DIARIZATION_PROFILE = ProcessorProfile(
    name="phase7_part4_diarization_benchmark",
    version="1.0.0",
    description="Phase 7 Part 4 diarization benchmark canonical-observation compatibility proof",
    observation_types=("diarization_speaker_turn",),
    confidence_rule="pass-through",
)
_BENCHMARK_SOCIAL_PROFILE = ProcessorProfile(
    name="phase7_part4_social_benchmark",
    version="1.0.0",
    description="Phase 7 Part 4 social-extraction benchmark canonical-observation proof",
    observation_types=("phone_number", "email_address", "url", "username_or_handle"),
    confidence_rule="pass-through",
)


def observations_for_asr_result(
    segments: tuple[TranscriptSegmentInput, ...],
    *,
    case_id: UUID,
    evidence_id: UUID,
    created_at: datetime,
) -> list[ObservationV1]:
    mentions = transcript_segments_to_mentions(segments)
    return [
        mention_to_observation(
            case_id=case_id,
            evidence_id=evidence_id,
            profile=_BENCHMARK_ASR_PROFILE,
            mention=mention,
            created_at=created_at,
            discriminator=str(index),
        )
        for index, mention in enumerate(mentions)
    ]


def observations_for_diarization_result(
    segments: tuple[DiarizationSegmentInput, ...],
    *,
    case_id: UUID,
    evidence_id: UUID,
    created_at: datetime,
) -> list[ObservationV1]:
    mentions = diarization_segments_to_mentions(segments)
    return [
        mention_to_observation(
            case_id=case_id,
            evidence_id=evidence_id,
            profile=_BENCHMARK_DIARIZATION_PROFILE,
            mention=mention,
            created_at=created_at,
            discriminator=str(index),
        )
        for index, mention in enumerate(mentions)
    ]


def observation_for_social_mention(
    mention: RawMention,
    *,
    case_id: UUID,
    evidence_id: UUID,
    created_at: datetime,
    discriminator: str = "",
) -> ObservationV1:
    return mention_to_observation(
        case_id=case_id,
        evidence_id=evidence_id,
        profile=_BENCHMARK_SOCIAL_PROFILE,
        mention=mention,
        created_at=created_at,
        discriminator=discriminator,
    )


__all__ = [
    "FAKE_EXECUTION_FAILURE_FILENAME",
    "FAKE_EXECUTION_FAILURE_TEXT",
    "FAKE_UNREADABLE_FILENAME",
    "FAKE_UNREADABLE_TEXT",
    "FAKE_UNSUPPORTED_FORMAT_FILENAME",
    "FAKE_UNSUPPORTED_FORMAT_TEXT",
    "RUNTIME_ENVIRONMENT",
    "AsrEngine",
    "AsrEngineResult",
    "AudioBenchmarkSample",
    "BenchmarkArtifactUnavailableError",
    "DeterministicSocialExtractionEngine",
    "DiarizationBenchmarkSample",
    "DiarizationEngine",
    "DiarizationEngineResult",
    "EngineError",
    "FakeAsrEngine",
    "FakeDiarizationEngine",
    "FakeLanguageIdEngine",
    "FakeSocialExtractionEngine",
    "FakeVadEngine",
    "LanguageIdEngine",
    "LanguageIdEngineResult",
    "SocialBenchmarkSample",
    "SocialExtractionEngine",
    "SocialExtractionEngineResult",
    "SocialMention",
    "VadEngine",
    "VadEngineResult",
    "observation_for_social_mention",
    "observations_for_asr_result",
    "observations_for_diarization_result",
    "run_asr_benchmark",
    "run_diarization_benchmark",
    "run_language_id_benchmark",
    "run_social_extraction_benchmark",
    "run_vad_benchmark",
]
