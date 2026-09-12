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

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

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


__all__ = [
    "AsrAdapter",
    "AsrAdapterState",
    "AsrTranscriptionResult",
    "UnavailableAsrAdapter",
]
