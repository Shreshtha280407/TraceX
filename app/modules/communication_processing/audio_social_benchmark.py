"""Phase 7 Part 4 audio/social-benchmark orchestration: the one entry point the CLI calls.

`run_benchmark` validates the requested dataset/candidate against Part 1's
frozen catalogue and this task's own Part 4 scope, checks that the
dataset's and candidate's licence/conditional status are cleared for a
real run, resolves local roots from explicit configuration only, checks
artifact availability *before* attempting to execute anything, dispatches
to the right adapter (`audio_social_benchmark_adapters.py`), and writes a
safe aggregate JSON result. It never downloads anything, never writes to
Neo4j/PostgreSQL/Redis/MinIO, and never fabricates a `succeeded` result
for a request whose local artifacts or licence clearance are missing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.modules.communication_processing.audio_social_benchmark_adapters import (
    AsrEngine,
    AudioBenchmarkSample,
    BenchmarkArtifactUnavailableError,
    DeterministicSocialExtractionEngine,
    DiarizationBenchmarkSample,
    DiarizationEngine,
    EngineError,
    LanguageIdEngine,
    SocialBenchmarkSample,
    VadEngine,
    run_asr_benchmark,
    run_diarization_benchmark,
    run_language_id_benchmark,
    run_social_extraction_benchmark,
    run_vad_benchmark,
)
from app.modules.communication_processing.audio_social_benchmark_metrics import TimeInterval
from app.modules.communication_processing.audio_social_benchmark_validation import (
    LicenseNotClearedError,
    benchmark_inference_config_hash,
    reject_private_local_paths,
    require_cleared_for_real_execution,
    resolve_benchmark_data_root,
    resolve_benchmark_output_root,
    resolve_model_cache_root,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)
from app.modules.communication_processing.models import TranscriptSegmentInput
from app.modules.evaluation.catalog import load_model_candidate_catalog
from app.modules.evaluation.manifest import load_dataset_manifest
from app.modules.evaluation.models import (
    BenchmarkRunStatus,
    BenchmarkRunV1,
    CandidateTask,
    DatasetManifestEntryV1,
    ModelCandidateV1,
    SplitId,
)

RUNTIME_ENVIRONMENT = "phase7-part4-audio-social-benchmark-cli-v1"

#: Every `local_path_placeholder` in the frozen manifest starts with this
#: repo-relative prefix; stripping it lets `TRACEX_BENCHMARK_DATA_ROOT`
#: point anywhere on a real machine while the manifest's own placeholder
#: stays a portable, dataset-relative suffix -- same convention every
#: other Phase 7 benchmark part building on this manifest uses.
_LOCAL_DATA_PREFIX = "local-data/"


def dataset_subdirectory(dataset: DatasetManifestEntryV1, data_root: Path) -> Path:
    placeholder = dataset.local_path_placeholder
    suffix = (
        placeholder[len(_LOCAL_DATA_PREFIX) :]
        if placeholder.startswith(_LOCAL_DATA_PREFIX)
        else placeholder
    )
    return data_root / suffix


@dataclass(frozen=True)
class VerifiedModelArtifact:
    """Verified-during-MacBook-pre-flight model metadata.

    Every field must come from an artifact Aditya's pre-flight actually
    inspected -- this module never invents a model name, version, or hash.
    """

    model_name: str
    model_version: str
    model_sha256: str


#: A manually staged Torch Hub *source snapshot* of `snakers4/silero-vad`
#: (a local checkout containing `hubconf.py` at its root) under this
#: subdirectory of `TRACEX_MODEL_CACHE_ROOT` -- never fetched by this code.
_SILERO_VAD_SNAPSHOT_SUBDIR = "silero-vad"
#: The snapshot's own pinned model-weight file, hashed against
#: `VerifiedModelArtifact.model_sha256` before anything is loaded. Best
#: effort: mirrors the real `snakers4/silero-vad` repository's published
#: layout, but this session cannot install/inspect it directly -- adjust
#: during MacBook pre-flight if the staged snapshot's real layout differs.
_SILERO_VAD_WEIGHT_RELATIVE_PATH = "files/silero_vad.jit"
#: fastText's own model file, staged directly under the model cache root.
_FASTTEXT_LID_MODEL_RELATIVE_PATH = "lid.176.ftz"
#: The faster-whisper transcription-stage model directory `fasttext-lid176`
#: is chained after (see `_build_real_language_id_engine`), plus its own
#: pinned weight file inside that directory, hashed the same way.
_LID_TRANSCRIPTION_STAGE_SUBDIR = "lid-transcription-stage"
_LID_TRANSCRIPTION_STAGE_WEIGHT_RELATIVE_PATH = "lid-transcription-stage/model.bin"

_SHA256_HEX_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def _resolve_within_model_cache_root(model_cache_root: Path, relative: str) -> Path:
    """Resolves `relative` under `model_cache_root`, rejecting any escape.

    A symlink or an unexpected layout could otherwise resolve outside the
    operator-configured cache root -- this benchmark must only ever read a
    locally staged artifact from exactly where it was configured, never
    anywhere else on the filesystem.
    """
    resolved_root = model_cache_root.resolve()
    resolved_path = (model_cache_root / relative).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise BenchmarkArtifactUnavailableError(
            f"configured model artifact path '{relative}' escapes the model cache root"
        )
    return resolved_path


def _require_valid_sha256_format(value: str, *, label: str) -> None:
    if not _SHA256_HEX_PATTERN.fullmatch(value):
        raise BenchmarkArtifactUnavailableError(
            f"{label} artifact SHA-256 is not a well-formed 64-character hex digest"
        )


def _verify_local_artifact_sha256(path: Path, *, expected_sha256: str, label: str) -> None:
    """Verifies a locally staged artifact file's real content hash before it is ever loaded.

    Never trusts a caller-declared hash alone: a missing file or a
    byte-for-byte mismatch against the actually-staged file both degrade to
    a safe `BenchmarkArtifactUnavailableError` -- never a silent download or
    a fabricated success. The exception message never includes `path`
    itself, only the safe `label` -- consistent with this harness never
    writing a local absolute path into a benchmark result.
    """
    _require_valid_sha256_format(expected_sha256, label=label)
    if not path.is_file():
        raise BenchmarkArtifactUnavailableError(
            f"expected local {label} artifact file was not found in the configured "
            "model cache -- this benchmark never downloads it automatically"
        )
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BenchmarkArtifactUnavailableError(
            f"local {label} artifact SHA-256 does not match the expected verified hash"
        )


def _build_real_asr_engine(
    *, model_cache_root: Path, artifact: VerifiedModelArtifact, variant: str
) -> AsrEngine:
    """Best-effort real faster-whisper wiring.

    This session cannot install or exercise `faster-whisper`, so the exact
    API called here is written from its documented public shape and may
    need a small adjustment once Aditya's MacBook pre-flight confirms the
    actually-installed version's API -- any mismatch degrades to a safe
    `BenchmarkArtifactUnavailableError`, never a crash or a fabricated
    result. See `docs/runbooks/local-development.md`.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "faster_whisper is not installed in this environment -- run the "
            "MacBook pre-flight steps in docs/runbooks/local-development.md "
            "before a real ASR benchmark run"
        ) from exc

    try:
        model_dir = model_cache_root / variant
        model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local faster-whisper engine with the "
            "configured model cache -- verify the installed faster_whisper API "
            "matches this adapter during MacBook pre-flight"
        ) from exc

    from app.modules.communication_processing.audio_social_benchmark_adapters import AsrEngineResult

    class _RealAsrEngine:
        def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrEngineResult:  # noqa: ARG002
            import io
            import math

            segments_iter, info = model.transcribe(io.BytesIO(audio_bytes))
            segments = tuple(
                TranscriptSegmentInput(
                    start_ms=int(segment.start * 1000),
                    end_ms=int(segment.end * 1000),
                    text=segment.text,
                    language_hint=info.language,
                    confidence=min(1.0, max(0.0, math.exp(segment.avg_logprob))),
                    source_segment_id=f"asr-{index}",
                )
                for index, segment in enumerate(segments_iter)
            )
            return AsrEngineResult(
                segments=segments,
                backend="cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealAsrEngine()


def _build_real_vad_engine(*, model_cache_root: Path, artifact: VerifiedModelArtifact) -> VadEngine:
    """Best-effort, fully offline real Silero VAD wiring.

    This never calls `torch.hub.load("snakers4/silero-vad", ...)` against
    GitHub or any other network source. It requires a Torch Hub *source
    snapshot* of `snakers4/silero-vad` staged manually -- never downloaded
    by this code -- under `<model_cache_root>/silero-vad/` (a plain local
    checkout containing `hubconf.py` at its root), and loads it with
    `torch.hub.load(..., source="local")`, which only ever reads local
    files and never contacts a network host. `artifact.model_version`
    records the exact staged source revision/tag, and `artifact.model_sha256`
    is verified directly against the snapshot's own pinned weight file
    (`files/silero_vad.jit`) before anything is loaded. A missing snapshot,
    a resolved path outside `model_cache_root`, or a hash mismatch all
    degrade to a safe `BenchmarkArtifactUnavailableError` -- never a silent
    download or a fabricated result. `torch` is declared in this project's
    optional `[project.optional-dependencies] audio-social-benchmark`
    extra specifically for this one candidate -- it is never a
    production/base dependency, never installed on this development
    machine, and never imported anywhere outside this lazy, guarded
    function. This session cannot install or exercise `torch`/Silero VAD,
    so the exact API called here is written from its documented public
    shape and may need a small adjustment once Aditya's MacBook pre-flight
    confirms the actually-staged snapshot's real layout. See
    `docs/runbooks/local-development.md`.
    """
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "torch is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a "
            "real VAD benchmark run"
        ) from exc

    snapshot_dir = _resolve_within_model_cache_root(model_cache_root, _SILERO_VAD_SNAPSHOT_SUBDIR)
    if not snapshot_dir.is_dir():
        raise BenchmarkArtifactUnavailableError(
            "no locally staged Silero VAD source snapshot found under the "
            "configured model cache root -- this benchmark never downloads it "
            "automatically; see docs/runbooks/local-development.md"
        )
    weight_path = _resolve_within_model_cache_root(
        model_cache_root, f"{_SILERO_VAD_SNAPSHOT_SUBDIR}/{_SILERO_VAD_WEIGHT_RELATIVE_PATH}"
    )
    _verify_local_artifact_sha256(
        weight_path, expected_sha256=artifact.model_sha256, label="Silero VAD"
    )

    try:
        model, utils = torch.hub.load(
            repo_or_dir=str(snapshot_dir), model="silero_vad", source="local", trust_repo=True
        )
        get_speech_timestamps = utils[0]
    except Exception as exc:  # noqa: BLE001 - any load failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not load the locally staged Silero VAD snapshot -- verify "
            "the installed torch version and staged snapshot layout match "
            "this adapter during MacBook pre-flight"
        ) from exc

    from app.modules.communication_processing.audio_social_benchmark_adapters import (
        VadEngineResult,
    )

    class _RealVadEngine:
        def detect_speech(
            self,
            audio_bytes: bytes,
            *,
            filename: str,
            total_duration_ms: int,  # noqa: ARG002
        ) -> VadEngineResult:
            import io
            import wave

            try:
                with wave.open(io.BytesIO(audio_bytes)) as wav_file:
                    sample_rate = wav_file.getframerate()
                    frames = wav_file.readframes(wav_file.getnframes())
            except (wave.Error, EOFError) as exc:
                raise EngineError("unreadable", "could not decode WAV audio") from exc

            try:
                waveform = torch.frombuffer(bytearray(frames), dtype=torch.int16).float() / 32768.0
                timestamps = get_speech_timestamps(waveform, model, sampling_rate=sample_rate)
            except Exception as exc:  # noqa: BLE001 - a single bad sample is a safe failure
                raise EngineError("execution_failure", "Silero VAD inference failed") from exc

            intervals = tuple(
                TimeInterval(
                    start_ms=int(timestamp["start"] / sample_rate * 1000),
                    end_ms=int(timestamp["end"] / sample_rate * 1000),
                )
                for timestamp in timestamps
            )
            return VadEngineResult(
                speech_intervals=intervals,
                backend="cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealVadEngine()


def _build_real_diarization_engine(
    *, model_cache_root: Path, artifact: VerifiedModelArtifact
) -> DiarizationEngine:
    """Best-effort real pyannote.audio wiring.

    `pyannote-community-local` is `selection_status: conditional` in the
    frozen catalogue -- its local-use terms are not yet accepted, so
    `require_cleared_for_real_execution` blocks a `SUCCEEDED` result for
    it regardless of whether this wiring succeeds. Written anyway so the
    real path is ready the moment that conditional status is lifted by an
    explicit team decision. This session cannot install or exercise
    `pyannote.audio`, so the exact API called here may need a small
    adjustment once Aditya's MacBook pre-flight confirms the
    actually-installed version's API.
    """
    try:
        from pyannote.audio import Pipeline  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "pyannote.audio is not installed in this environment -- run the "
            "MacBook pre-flight steps in docs/runbooks/local-development.md "
            "before a real diarization benchmark run"
        ) from exc

    try:
        pipeline = Pipeline.from_pretrained(str(model_cache_root))
    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local pyannote.audio pipeline with the "
            "configured model cache -- verify the installed pyannote.audio API "
            "matches this adapter during MacBook pre-flight, and that its "
            "local-use terms have actually been accepted"
        ) from exc

    from app.modules.communication_processing.audio_social_benchmark_adapters import (
        DiarizationEngineResult,
    )
    from app.modules.communication_processing.models import DiarizationSegmentInput

    class _RealDiarizationEngine:
        def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationEngineResult:  # noqa: ARG002
            import io

            annotation = pipeline(io.BytesIO(audio_bytes))
            segments = tuple(
                DiarizationSegmentInput(
                    start_ms=int(turn.start * 1000),
                    end_ms=int(turn.end * 1000),
                    speaker_label=str(speaker),
                    confidence=1.0,
                    source_segment_id=f"diar-{index}",
                )
                for index, (turn, _, speaker) in enumerate(annotation.itertracks(yield_label=True))
            )
            return DiarizationEngineResult(
                segments=segments,
                backend="cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealDiarizationEngine()


def _build_real_language_id_engine(
    *,
    model_cache_root: Path,
    artifact: VerifiedModelArtifact,
    transcription_stage_artifact: VerifiedModelArtifact,
) -> LanguageIdEngine:
    """Real fastText language-identification wiring, chained after a verified transcription stage.

    `fasttext-lid176`'s `lid.176` model classifies language *from text*,
    not raw audio -- there is no audio-native language-ID candidate in
    Part 1's frozen catalogue. Rather than silently substituting a
    *different* model's own built-in language detection (e.g. Whisper's),
    which would mislabel whose output is actually being measured under
    the `fasttext-lid176` candidate name, this wiring explicitly chains a
    transcription stage (`faster-whisper`, already declared in this same
    optional extra) purely as plumbing to produce text for fastText to
    classify. Only the language *classification* is attributed to this
    candidate's own name/version/hash -- the transcription stage is never
    itself benchmarked here (Part 4's ASR candidates cover that
    separately).

    Both `artifact` (the fastText `lid.176` model) and
    `transcription_stage_artifact` (the faster-whisper transcription
    stage it depends on) are required, verified references -- there is no
    default for either. `model_cache_root` must contain `lid.176.ftz` and
    a `lid-transcription-stage/` faster-whisper model directory; each is
    verified against its own caller-supplied SHA-256 before it is loaded.
    A missing artifact, an absent transcription-stage reference, or a
    hash mismatch on either one all degrade to a safe
    `BenchmarkArtifactUnavailableError` -- never a fabricated result. The
    transcript text produced internally to feed fastText is never
    returned, logged, or stored anywhere -- only the final language label
    leaves this function. See `docs/runbooks/local-development.md`.
    """
    try:
        import fasttext  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "fasttext is not installed in this environment -- run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md before a "
            "real language-identification benchmark run"
        ) from exc
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise BenchmarkArtifactUnavailableError(
            "faster_whisper is not installed in this environment -- it is "
            "required as this harness's transcription stage before fastText "
            "can classify a spoken language from audio; run the MacBook "
            "pre-flight steps in docs/runbooks/local-development.md"
        ) from exc

    lid_model_path = _resolve_within_model_cache_root(
        model_cache_root, _FASTTEXT_LID_MODEL_RELATIVE_PATH
    )
    _verify_local_artifact_sha256(
        lid_model_path, expected_sha256=artifact.model_sha256, label="fastText lid.176"
    )
    transcription_stage_dir = _resolve_within_model_cache_root(
        model_cache_root, _LID_TRANSCRIPTION_STAGE_SUBDIR
    )
    transcription_stage_weight_path = _resolve_within_model_cache_root(
        model_cache_root, _LID_TRANSCRIPTION_STAGE_WEIGHT_RELATIVE_PATH
    )
    _verify_local_artifact_sha256(
        transcription_stage_weight_path,
        expected_sha256=transcription_stage_artifact.model_sha256,
        label="language-ID transcription-stage",
    )

    try:
        lid_model = fasttext.load_model(str(lid_model_path))
    except Exception as exc:  # noqa: BLE001 - any load failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not load the local fastText language-id model -- verify the "
            "installed fasttext API matches this adapter during MacBook pre-flight"
        ) from exc

    try:
        transcription_stage = WhisperModel(
            str(transcription_stage_dir), device="cpu", compute_type="int8"
        )
    except Exception as exc:  # noqa: BLE001 - any construction failure is a safe "unavailable"
        raise BenchmarkArtifactUnavailableError(
            "could not construct the local faster-whisper transcription stage "
            "needed before fastText language classification -- verify the "
            "installed faster_whisper API matches this adapter during MacBook "
            "pre-flight"
        ) from exc

    from app.modules.communication_processing.audio_social_benchmark_adapters import (
        LanguageIdEngineResult,
    )

    class _RealLanguageIdEngine:
        def identify(self, audio_bytes: bytes, *, filename: str) -> LanguageIdEngineResult:  # noqa: ARG002
            import io

            try:
                segments_iter, _info = transcription_stage.transcribe(io.BytesIO(audio_bytes))
                transcript = " ".join(segment.text for segment in segments_iter).strip()
            except Exception as exc:  # noqa: BLE001 - a single bad sample is a safe failure
                raise EngineError("execution_failure", "transcription stage failed") from exc
            if not transcript:
                raise EngineError("unreadable", "transcription stage produced no text to classify")

            labels, _confidence = lid_model.predict(transcript.replace("\n", " "))
            predicted_language = labels[0].removeprefix("__label__")
            return LanguageIdEngineResult(
                predicted_language=predicted_language,
                backend="cpu",
                model_name=artifact.model_name,
                model_version=artifact.model_version,
                model_sha256=artifact.model_sha256,
            )

    return _RealLanguageIdEngine()


def _unavailable_run(
    *,
    candidate: ModelCandidateV1,
    dataset: DatasetManifestEntryV1,
    split_id: SplitId,
    reason: str,
    now: datetime,
) -> BenchmarkRunV1:
    return BenchmarkRunV1(
        schema_version="v1",
        run_id=f"{dataset.dataset_id}-{candidate.candidate_id}-{uuid4().hex[:12]}",
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        task=candidate.task,
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile="unavailable",
        inference_config_hash="0" * 64,
        artifact_sha256=None,
        metrics={},
        started_at=now,
        completed_at=now,
        status=BenchmarkRunStatus.UNAVAILABLE,
        failure_reason_safe=reason,
    )


def run_benchmark(
    *,
    dataset_id: str,
    candidate_id: str,
    data_root: Path | None = None,
    model_cache_root: Path | None = None,
    output_root: Path | None = None,
    split_id: SplitId = SplitId.DEVELOPMENT,
    verified_model_artifact: VerifiedModelArtifact | None = None,
    verified_transcription_stage_artifact: VerifiedModelArtifact | None = None,
    now: datetime | None = None,
) -> BenchmarkRunV1:
    """Validate, resolve artifacts, run one benchmark, and write a safe result.

    Never raises for a missing local artifact or an unresolved licence/
    conditional status -- both become a returned
    `BenchmarkRunStatus.UNAVAILABLE` result. Still raises for a genuinely
    invalid request (`UnknownDatasetError`/`UnknownCandidateError`/
    `UnsupportedCombinationError`/`BenchmarkConfigError`): those are caller
    bugs, not benchmark outcomes.

    `verified_transcription_stage_artifact` is only meaningful for
    `fasttext-lid176` (`CandidateTask.LANGUAGE_IDENTIFICATION`), which
    chains a faster-whisper transcription stage in front of fastText's own
    classifier -- see `_build_real_language_id_engine`. Its name/version/
    SHA-256 are folded into `inference_config_hash` so a different
    transcription-stage dependency is detectable as a different
    configuration, since the frozen `BenchmarkRunV1` contract has only one
    `artifact_sha256` field (attributed to the primary candidate, fastText).
    """
    now = now or datetime.now(UTC)
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    dataset = validate_dataset_id(manifest, dataset_id)
    candidate = validate_candidate_id(catalog, candidate_id)
    validate_dataset_candidate_pair(dataset, candidate)

    resolved_data_root = resolve_benchmark_data_root(data_root)
    resolved_output_root = resolve_benchmark_output_root(output_root)
    resolved_model_cache_root = resolve_model_cache_root(model_cache_root)
    dataset_dir = dataset_subdirectory(dataset, resolved_data_root)

    inference_config: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "dataset_id": dataset.dataset_id,
        "task": candidate.task.value,
    }
    if verified_model_artifact is not None:
        inference_config["model_name"] = verified_model_artifact.model_name
        inference_config["model_version"] = verified_model_artifact.model_version
    if verified_transcription_stage_artifact is not None:
        inference_config["transcription_stage_model_name"] = (
            verified_transcription_stage_artifact.model_name
        )
        inference_config["transcription_stage_model_version"] = (
            verified_transcription_stage_artifact.model_version
        )
        inference_config["transcription_stage_model_sha256"] = (
            verified_transcription_stage_artifact.model_sha256
        )
    inference_config_hash = benchmark_inference_config_hash(inference_config)

    try:
        require_cleared_for_real_execution(dataset, candidate)
    except LicenseNotClearedError as exc:
        run = _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )
        _write_result(run, resolved_output_root)
        return run

    if not dataset_dir.is_dir():
        run = _unavailable_run(
            candidate=candidate,
            dataset=dataset,
            split_id=split_id,
            reason="configured dataset directory does not exist locally",
            now=now,
        )
        _write_result(run, resolved_output_root)
        return run

    try:
        if candidate.task == CandidateTask.ASR:
            run = _run_asr(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        elif candidate.task == CandidateTask.VAD:
            run = _run_vad(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        elif candidate.task == CandidateTask.DIARIZATION:
            run = _run_diarization(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                split_id=split_id,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        elif candidate.task == CandidateTask.LANGUAGE_IDENTIFICATION:
            run = _run_language_id(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                model_cache_root=resolved_model_cache_root,
                artifact=verified_model_artifact,
                transcription_stage_artifact=verified_transcription_stage_artifact,
                split_id=split_id,
                inference_config_hash=inference_config_hash,
                now=now,
            )
        else:
            run = _run_social_extraction(
                dataset=dataset,
                candidate=candidate,
                dataset_dir=dataset_dir,
                split_id=split_id,
                inference_config_hash=inference_config_hash,
                now=now,
            )
    except BenchmarkArtifactUnavailableError as exc:
        run = _unavailable_run(
            candidate=candidate, dataset=dataset, split_id=split_id, reason=str(exc), now=now
        )

    reject_private_local_paths(run.model_dump(mode="json"))
    _write_result(run, resolved_output_root)
    return run


def _require_artifact(
    artifact: VerifiedModelArtifact | None, *, task_name: str
) -> VerifiedModelArtifact:
    if artifact is None:
        raise BenchmarkArtifactUnavailableError(
            f"verified model artifact metadata (name/version/sha256) is required "
            f"for a real {task_name} benchmark run"
        )
    return artifact


def _load_audio_samples(dataset_dir: Path) -> list[AudioBenchmarkSample]:
    """Reads `dataset_dir/benchmark_manifest.jsonl` -- an invented local format.

    Not the real Common Voice/AMI annotation format (unverified in this
    environment) -- converting real annotations into this shape is
    deferred to Aditya's MacBook pre-flight. One JSON object per line:
    `{"sample_id", "audio_path" (relative), "total_duration_ms",
    "reference_text"?, "reference_language"?, "ground_truth_speech_ms"?
    (list of [start, end] pairs)}`.
    """
    manifest_path = dataset_dir / "benchmark_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no benchmark_manifest.jsonl found in the configured dataset directory"
        )
    samples: list[AudioBenchmarkSample] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        audio_path = (dataset_dir / record["audio_path"]).resolve()
        if not str(audio_path).startswith(str(dataset_dir.resolve())):
            raise BenchmarkArtifactUnavailableError(
                "benchmark manifest referenced a path outside the dataset directory"
            )
        ground_truth_raw = record.get("ground_truth_speech_ms")
        ground_truth = (
            tuple(TimeInterval(start_ms=pair[0], end_ms=pair[1]) for pair in ground_truth_raw)
            if ground_truth_raw is not None
            else None
        )
        samples.append(
            AudioBenchmarkSample(
                sample_id=record["sample_id"],
                audio_bytes=audio_path.read_bytes(),
                filename=audio_path.name,
                total_duration_ms=record["total_duration_ms"],
                reference_text=record.get("reference_text"),
                reference_language=record.get("reference_language"),
                ground_truth_speech_intervals=ground_truth,
            )
        )
    return samples


def _load_diarization_samples(dataset_dir: Path) -> list[DiarizationBenchmarkSample]:
    """Reads `dataset_dir/diarization_manifest.jsonl` -- an invented local format.

    One JSON object per line: `{"sample_id", "audio_path", "total_duration_ms",
    "ground_truth_turns"? (list of {"speaker_label","start_ms","end_ms"})}`.
    """
    from app.modules.communication_processing.audio_social_benchmark_metrics import SpeakerTurn

    manifest_path = dataset_dir / "diarization_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no diarization_manifest.jsonl found in the configured dataset directory"
        )
    samples: list[DiarizationBenchmarkSample] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        audio_path = (dataset_dir / record["audio_path"]).resolve()
        if not str(audio_path).startswith(str(dataset_dir.resolve())):
            raise BenchmarkArtifactUnavailableError(
                "benchmark manifest referenced a path outside the dataset directory"
            )
        ground_truth_raw = record.get("ground_truth_turns")
        ground_truth = (
            tuple(
                SpeakerTurn(
                    speaker_label=turn["speaker_label"],
                    start_ms=turn["start_ms"],
                    end_ms=turn["end_ms"],
                )
                for turn in ground_truth_raw
            )
            if ground_truth_raw is not None
            else None
        )
        samples.append(
            DiarizationBenchmarkSample(
                sample_id=record["sample_id"],
                audio_bytes=audio_path.read_bytes(),
                filename=audio_path.name,
                total_duration_ms=record["total_duration_ms"],
                ground_truth_turns=ground_truth,
            )
        )
    return samples


def _load_social_samples(dataset_dir: Path) -> tuple[list[SocialBenchmarkSample], str]:
    """Reads exactly one `*.jsonl` file in `dataset_dir` -- an invented local format.

    One JSON object per line: `{"sample_id", "message_text",
    "expected_mentions"? (list of [type, normalized_value] pairs)}`.
    Returns the samples plus that single input file's own SHA-256 (this
    candidate is a deterministic-rules baseline with no model weight --
    the input file's hash stands in as "the artifact this run measured",
    the same convention Phase 7 Part 2's CDR/finance baseline established).
    """
    candidates = sorted(dataset_dir.glob("*.jsonl"))
    if len(candidates) != 1:
        raise BenchmarkArtifactUnavailableError(
            "expected exactly one .jsonl input file in the dataset directory and "
            "found zero or more than one"
        )
    manifest_path = candidates[0]
    data = manifest_path.read_bytes()
    samples: list[SocialBenchmarkSample] = []
    for line in data.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        expected_raw = record.get("expected_mentions")
        expected = (
            tuple((pair[0], pair[1]) for pair in expected_raw) if expected_raw is not None else None
        )
        samples.append(
            SocialBenchmarkSample(
                sample_id=record["sample_id"],
                message_text=record["message_text"],
                expected_mentions=expected,
            )
        )
    return samples, hashlib.sha256(data).hexdigest()


def _run_asr(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact | None,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = _load_audio_samples(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    verified = _require_artifact(artifact, task_name="ASR")
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError("a model cache root is required for a real ASR run")
    variant = "small" if candidate.candidate_id.endswith("small") else "medium"
    engine = _build_real_asr_engine(
        model_cache_root=model_cache_root, artifact=verified, variant=variant
    )
    return run_asr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_vad(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact | None,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = _load_audio_samples(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    verified = _require_artifact(artifact, task_name="VAD")
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError("a model cache root is required for a real VAD run")
    engine = _build_real_vad_engine(model_cache_root=model_cache_root, artifact=verified)
    return run_vad_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_diarization(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact | None,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = _load_diarization_samples(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    verified = _require_artifact(artifact, task_name="diarization")
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError(
            "a model cache root is required for a real diarization run"
        )
    engine = _build_real_diarization_engine(model_cache_root=model_cache_root, artifact=verified)
    return run_diarization_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_language_id(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    model_cache_root: Path | None,
    artifact: VerifiedModelArtifact | None,
    transcription_stage_artifact: VerifiedModelArtifact | None,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples = _load_audio_samples(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    verified = _require_artifact(artifact, task_name="language-identification")
    verified_transcription_stage = _require_artifact(
        transcription_stage_artifact, task_name="language-identification transcription stage"
    )
    if model_cache_root is None:
        raise BenchmarkArtifactUnavailableError(
            "a model cache root is required for a real language-identification run"
        )
    engine = _build_real_language_id_engine(
        model_cache_root=model_cache_root,
        artifact=verified,
        transcription_stage_artifact=verified_transcription_stage,
    )
    return run_language_id_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _run_social_extraction(
    *,
    dataset: DatasetManifestEntryV1,
    candidate: ModelCandidateV1,
    dataset_dir: Path,
    split_id: SplitId,
    inference_config_hash: str,
    now: datetime,
) -> BenchmarkRunV1:
    samples, input_file_sha256 = _load_social_samples(dataset_dir)
    if not samples:
        raise BenchmarkArtifactUnavailableError("benchmark manifest named zero usable samples")
    engine = DeterministicSocialExtractionEngine(model_sha256=input_file_sha256)
    return run_social_extraction_benchmark(
        engine=engine,
        samples=samples,
        candidate_id=candidate.candidate_id,
        dataset_id=dataset.dataset_id,
        split_id=split_id,
        inference_config_hash=inference_config_hash,
        now=now,
    )


def _write_result(run: BenchmarkRunV1, output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    result_path = output_root / f"{run.run_id}.json"
    result_path.write_text(run.model_dump_json(indent=2), encoding="utf-8")


__all__ = [
    "VerifiedModelArtifact",
    "dataset_subdirectory",
    "run_benchmark",
]
