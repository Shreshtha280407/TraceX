"""A replaceable local speech-to-text adapter interface -- no real ASR runs today.

## Why there's an interface but no real implementation

Section 5.2 of this module's Phase 3 brief asks for "a replaceable local
ASR adapter... prefer real locally-available/cached ASR where the
repository already supports one... never fetch multi-gigabyte models
automatically during tests/CI/Docker build/API startup". This repository's
own `tests/unit/communication_processing/test_module_safety.py` already
enforces, as a deliberate architectural boundary set in an earlier phase,
that nothing under `app/modules/communication_processing/` may import
`numpy`, `torch`, `transformers`, `whisper`, `faster_whisper`, `pyannote`,
`speechbrain`, `librosa`, or `sklearn` -- every real local ASR toolkit
practical for this project. There is no ML-free, pure-stdlib local ASR
implementation to fall back to; ASR is inherently a model-inference task.

Reversing that import ban was judged out of scope for this task (it is
this module's *own* pre-existing boundary, not another owner's, but
changing it would be a significant architectural decision belonging to
the whole team, not a unilateral Phase-3 addition -- flagged in
`docs/architecture/phase-3-decisions.md` for team review). So this phase
implements the *adapter boundary* the brief asks for -- a typed,
documented, testable `Protocol` -- with exactly one production
implementation: `UnavailableAsrAdapter`, which always reports
`AsrAdapterState.UNAVAILABLE` and never fabricates a transcript. This is
not new behavior: `worker.py`'s existing `AudioRoutingDecision.
DEFERRED_REQUIRES_ASR` path (see `audio/routing.py`) already produces
exactly this outcome for raw audio today. This module gives that existing
outcome a documented, typed shape, so a future phase can add a real
adapter behind this same interface without changing `worker.py`'s
dispatch contract.

A second, explicitly test-only adapter exists under
`tests/fixtures/communication_processing/asr_fixture_adapter.py` -- never
imported by `worker.py` or anything else under `app/` -- to prove the
`Protocol` is actually implementable and exercisable in tests without
conflating a fixture transcript with a genuine ASR result (see that
module's docstring, and Scenario 7 in `docs/qa/test-matrix.md`).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from app.core.canonical import canonical_sha256
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import TranscriptSegmentInput


class AsrAdapterState(StrEnum):
    """Whether an `AsrAdapter` can actually transcribe right now.

    `UNAVAILABLE` is the only value the production adapter in this phase
    ever reports -- see this module's docstring. `READY` exists so a
    future real adapter (or the test-only fixture adapter) has a state to
    report once transcription is genuinely possible.
    """

    UNAVAILABLE = "unavailable"
    READY = "ready"


@dataclass(frozen=True)
class AsrTranscriptionResult:
    """What a `READY` adapter's `transcribe` call returns.

    `segments` are plain `TranscriptSegmentInput`s -- the exact same typed
    shape `audio/transcript_import.py` already validates today, so a
    future real adapter's output flows through the *same*, already-tested
    `validate_transcript_segments`/`transcript_segments_to_mentions`
    pipeline unchanged, rather than a second, parallel one.
    """

    segments: tuple[TranscriptSegmentInput, ...]
    model_name: str
    model_version: str
    config_hash: str
    backend_name: str = "unknown"
    backend_version: str = "unknown"
    model_identity_hash: str = ""


@runtime_checkable
class AsrAdapter(Protocol):
    """The interface any local ASR adapter -- real or fixture -- must implement."""

    @property
    def state(self) -> AsrAdapterState: ...

    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrTranscriptionResult:
        """Transcribe one audio file. Callers must check `state` first.

        Never called by `worker.py` today -- `audio/routing.py` already
        defers before reaching any adapter. Implementations must raise
        rather than fabricate a result if called while not `READY`.
        """
        ...


class UnavailableAsrAdapter:
    """The only production `AsrAdapter` in this phase: always defers.

    No local ASR model is bundled, cached, or auto-downloaded, per
    CLAUDE.md's Phase 1 non-goals and this module's docstring above.
    """

    @property
    def state(self) -> AsrAdapterState:
        return AsrAdapterState.UNAVAILABLE

    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrTranscriptionResult:
        raise ProcessingError(
            ErrorCode.ASR_ADAPTER_UNAVAILABLE,
            "no local ASR adapter is available in this phase",
        )


class LocalCommandAsrAdapter:
    """Invoke an operator-provisioned offline ASR executable.

    The fixed command contract is ``--model <path> --input <temporary-wav>
    --language <hint>``.  The executable must emit JSON on stdout with
    timestamped ``segments``.  No model is downloaded and neither command
    output nor filesystem paths are logged or persisted.
    """

    def __init__(
        self,
        *,
        command: Path | None,
        model_path: Path | None,
        language: str,
        timeout_seconds: float,
    ) -> None:
        self._command = command
        self._model_path = model_path
        self._language = language
        self._timeout_seconds = timeout_seconds

    @property
    def state(self) -> AsrAdapterState:
        if (
            self._command is not None
            and self._model_path is not None
            and self._command.is_file()
            and self._model_path.exists()
        ):
            return AsrAdapterState.READY
        return AsrAdapterState.UNAVAILABLE

    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrTranscriptionResult:  # noqa: ARG002
        if self.state is not AsrAdapterState.READY:
            raise ProcessingError(
                ErrorCode.ASR_ADAPTER_UNAVAILABLE,
                "configured local ASR executable or model is unavailable",
            )
        assert self._command is not None and self._model_path is not None
        with tempfile.TemporaryDirectory(prefix="tracex-asr-") as directory:
            input_path = Path(directory) / "input.wav"
            input_path.write_bytes(audio_bytes)
            try:
                completed = subprocess.run(  # noqa: S603 - fixed argv, no shell interpolation
                    [
                        str(self._command),
                        "--model",
                        str(self._model_path),
                        "--input",
                        str(input_path),
                        "--language",
                        self._language,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProcessingError(
                    ErrorCode.ASR_BACKEND_UNAVAILABLE,
                    "local ASR backend did not complete",
                    retryable=True,
                ) from exc
        if completed.returncode != 0:
            raise ProcessingError(
                ErrorCode.ASR_BACKEND_UNAVAILABLE,
                "local ASR backend rejected the audio",
                retryable=True,
            )
        try:
            payload = json.loads(completed.stdout)
            raw_segments = payload["segments"]
            if not isinstance(raw_segments, list):
                raise TypeError
            segments = tuple(
                TranscriptSegmentInput(
                    start_ms=_required_int(item, "start_ms"),
                    end_ms=_required_int(item, "end_ms"),
                    text=_required_str(item, "text"),
                    language_hint=_optional_str(item, "language_hint") or self._language,
                    confidence=_required_float(item, "confidence"),
                    source_segment_id=_optional_str(item, "source_segment_id") or f"asr-{index}",
                )
                for index, item in enumerate(raw_segments)
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProcessingError(
                ErrorCode.ASR_BACKEND_INVALID_OUTPUT,
                "local ASR backend returned invalid segment metadata",
            ) from exc
        config_hash = canonical_sha256(
            {
                "backend": "local_command_asr.v1",
                "language": self._language,
                "model_path_identity": str(self._model_path),
            }
        )
        return AsrTranscriptionResult(
            segments=segments,
            model_name=_optional_str(payload, "model_name") or "local_command_asr",
            model_version=_optional_str(payload, "model_version") or "unknown",
            config_hash=config_hash,
            backend_name="local_command_asr",
            backend_version="1",
            model_identity_hash=canonical_sha256({"model_path": str(self._model_path)}),
        )


def _required_int(item: object, key: str) -> int:
    if (
        not isinstance(item, dict)
        or not isinstance(item.get(key), int)
        or isinstance(item[key], bool)
    ):
        raise TypeError
    return cast(int, item[key])


def _required_float(item: object, key: str) -> float:
    if not isinstance(item, dict) or not isinstance(item.get(key), int | float):
        raise TypeError
    value = float(item[key])
    if not 0.0 <= value <= 1.0:
        raise ValueError
    return value


def _required_str(item: object, key: str) -> str:
    if not isinstance(item, dict) or not isinstance(item.get(key), str):
        raise TypeError
    return cast(str, item[key])


def _optional_str(item: object, key: str) -> str | None:
    if not isinstance(item, dict):
        raise TypeError
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError
    return value


__all__ = [
    "AsrAdapter",
    "AsrAdapterState",
    "AsrTranscriptionResult",
    "LocalCommandAsrAdapter",
    "UnavailableAsrAdapter",
]
