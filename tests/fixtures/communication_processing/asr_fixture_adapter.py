"""An explicit, test-only `AsrAdapter` -- never imported by `app/`, never real ASR.

Exists solely to prove
`app.modules.communication_processing.audio.asr_adapter.AsrAdapter` is a
genuinely implementable, exercisable `Protocol`, and to give tests a
deterministic way to exhibit the "adapter is `READY`" code path without
running (or pretending to run) real speech recognition. `model_name`/
`model_version` are deliberately obvious fixture markers -- never a
plausible-looking real model name -- so a result from this adapter can
never be mistaken for a genuine local ASR result in a log, test report, or
provenance record (see Scenario 7 in docs/qa/test-matrix.md).
"""

from __future__ import annotations

from app.modules.communication_processing.audio.asr_adapter import (
    AsrAdapterState,
    AsrTranscriptionResult,
)
from app.modules.communication_processing.models import TranscriptSegmentInput

FIXTURE_MODEL_NAME = "fixture_asr_adapter"
FIXTURE_MODEL_VERSION = "test-only-1.0.0"


class AsrFixtureAdapter:
    """Always `READY`; `transcribe` returns the exact segments it was constructed with.

    Never wired into `worker.py` or any other production dispatch path --
    grep for `AsrFixtureAdapter` finds only test files.
    """

    def __init__(self, segments: tuple[TranscriptSegmentInput, ...]) -> None:
        self._segments = segments

    @property
    def state(self) -> AsrAdapterState:
        return AsrAdapterState.READY

    def transcribe(self, audio_bytes: bytes, *, filename: str) -> AsrTranscriptionResult:
        return AsrTranscriptionResult(
            segments=self._segments,
            model_name=FIXTURE_MODEL_NAME,
            model_version=FIXTURE_MODEL_VERSION,
            config_hash="fixture",
        )


__all__ = ["FIXTURE_MODEL_NAME", "FIXTURE_MODEL_VERSION", "AsrFixtureAdapter"]
