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

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

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


__all__ = [
    "SEGMENT_SOURCE_METADATA_SUPPLIED",
    "SEGMENT_SOURCE_MODEL_DERIVED",
    "DiarizationAdapter",
    "DiarizationAdapterState",
    "DiarizationResult",
    "UnavailableDiarizationAdapter",
]
