"""A replaceable local speaker-diarization adapter interface -- no real diarization runs today.

Mirrors `audio/asr_adapter.py`'s exact rationale: a real local diarization
toolkit (e.g. `pyannote`, `speechbrain`) is exactly what this module's own
`tests/unit/communication_processing/test_module_safety.py` already bans
as a deliberate, pre-existing architectural boundary, and there is no
ML-free fallback for diarization. See that module's docstring for the
full reasoning and the team-review flag in
`docs/architecture/phase-3-decisions.md`.

`UnavailableDiarizationAdapter` is the only production implementation --
always defers, never fabricates speaker turns. `worker.py`'s existing
`AudioRoutingDecision.DEFERRED_REQUIRES_DIARIZATION` path (see
`audio/routing.py`) already produces exactly this outcome for raw audio
today; this module gives it a documented, typed shape.
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
from app.modules.communication_processing.models import DiarizationSegmentInput

#: Recorded on every `diarization_speaker_turn` observation (see
#: `diarization_import.py::diarization_segments_to_mentions`) so a segment
#: that arrived as an external import is never indistinguishable from one
#: a local model genuinely derived -- see this module's brief, section 5.3.
SEGMENT_SOURCE_METADATA_SUPPLIED = "metadata_supplied"
#: Not yet ever produced in this phase: no `DiarizationAdapter` reports
#: `READY` today (see `UnavailableDiarizationAdapter`), so no diarization
#: segment is ever `model_derived` yet. Reserved for the future adapter
#: this `Protocol` exists to support.
SEGMENT_SOURCE_MODEL_DERIVED = "model_derived"


class DiarizationAdapterState(StrEnum):
    """Whether a `DiarizationAdapter` can actually diarize right now."""

    UNAVAILABLE = "unavailable"
    READY = "ready"


@dataclass(frozen=True)
class DiarizationResult:
    """What a `READY` adapter's `diarize` call returns.

    `segments` are plain `DiarizationSegmentInput`s -- the exact same
    typed shape `audio/diarization_import.py` already validates today, so
    a future real adapter's output flows through the same, already-tested
    `validate_diarization_segments`/`diarization_segments_to_mentions`
    pipeline unchanged.
    """

    segments: tuple[DiarizationSegmentInput, ...]
    model_name: str
    model_version: str
    config_hash: str
    backend_name: str = "unknown"
    backend_version: str = "unknown"
    model_identity_hash: str = ""


@runtime_checkable
class DiarizationAdapter(Protocol):
    """The interface any local diarization adapter -- real or fixture -- must implement."""

    @property
    def state(self) -> DiarizationAdapterState: ...

    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationResult:
        """Diarize one audio file. Callers must check `state` first.

        Never called by `worker.py` today -- `audio/routing.py` already
        defers before reaching any adapter. Implementations must raise
        rather than fabricate a result if called while not `READY`.
        """
        ...


class UnavailableDiarizationAdapter:
    """The only production `DiarizationAdapter` in this phase: always defers.

    No local diarization model is bundled, cached, or auto-downloaded,
    per CLAUDE.md's Phase 1 non-goals and this module's docstring above.
    """

    @property
    def state(self) -> DiarizationAdapterState:
        return DiarizationAdapterState.UNAVAILABLE

    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationResult:
        raise ProcessingError(
            ErrorCode.DIARIZATION_ADAPTER_UNAVAILABLE,
            "no local diarization adapter is available in this phase",
        )


class LocalCommandDiarizationAdapter:
    """Invoke an operator-provisioned offline diarization executable.

    It uses the same fixed ``--model``/``--input`` command boundary as ASR
    and accepts only JSON speaker-turn metadata on stdout. Labels are
    validated source-local technical labels, never person identities.
    """

    def __init__(
        self, *, command: Path | None, model_path: Path | None, timeout_seconds: float
    ) -> None:
        self._command = command
        self._model_path = model_path
        self._timeout_seconds = timeout_seconds

    @property
    def state(self) -> DiarizationAdapterState:
        if (
            self._command is not None
            and self._model_path is not None
            and self._command.is_file()
            and self._model_path.exists()
        ):
            return DiarizationAdapterState.READY
        return DiarizationAdapterState.UNAVAILABLE

    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationResult:  # noqa: ARG002
        if self.state is not DiarizationAdapterState.READY:
            raise ProcessingError(
                ErrorCode.DIARIZATION_ADAPTER_UNAVAILABLE,
                "configured local diarization executable or model is unavailable",
            )
        assert self._command is not None and self._model_path is not None
        with tempfile.TemporaryDirectory(prefix="tracex-diarization-") as directory:
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
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProcessingError(
                    ErrorCode.DIARIZATION_BACKEND_UNAVAILABLE,
                    "local diarization backend did not complete",
                    retryable=True,
                ) from exc
        if completed.returncode != 0:
            raise ProcessingError(
                ErrorCode.DIARIZATION_BACKEND_UNAVAILABLE,
                "local diarization backend rejected the audio",
                retryable=True,
            )
        try:
            payload = json.loads(completed.stdout)
            raw_segments = payload["segments"]
            if not isinstance(raw_segments, list):
                raise TypeError
            segments = tuple(
                DiarizationSegmentInput(
                    start_ms=_required_int(item, "start_ms"),
                    end_ms=_required_int(item, "end_ms"),
                    speaker_label=_required_str(item, "speaker_label"),
                    confidence=_required_float(item, "confidence"),
                    source_segment_id=_optional_str(item, "source_segment_id") or f"diar-{index}",
                )
                for index, item in enumerate(raw_segments)
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProcessingError(
                ErrorCode.DIARIZATION_BACKEND_INVALID_OUTPUT,
                "local diarization backend returned invalid turn metadata",
            ) from exc
        config_hash = canonical_sha256(
            {
                "backend": "local_command_diarization.v1",
                "model_path_identity": str(self._model_path),
            }
        )
        return DiarizationResult(
            segments=segments,
            model_name=_optional_str(payload, "model_name") or "local_command_diarization",
            model_version=_optional_str(payload, "model_version") or "unknown",
            config_hash=config_hash,
            backend_name="local_command_diarization",
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
    "SEGMENT_SOURCE_METADATA_SUPPLIED",
    "SEGMENT_SOURCE_MODEL_DERIVED",
    "DiarizationAdapter",
    "DiarizationAdapterState",
    "DiarizationResult",
    "LocalCommandDiarizationAdapter",
    "UnavailableDiarizationAdapter",
]
