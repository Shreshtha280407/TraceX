"""An explicit, test-only `DiarizationAdapter` -- never imported by `app/`, never real diarization.

Mirrors `asr_fixture_adapter.py`'s exact rationale: proves
`app.modules.communication_processing.audio.diarization_adapter.
DiarizationAdapter` is genuinely implementable, with obvious fixture
markers so a result can never be mistaken for a real local diarization
result.
"""

from __future__ import annotations

from app.modules.communication_processing.audio.diarization_adapter import (
    DiarizationAdapterState,
    DiarizationResult,
)
from app.modules.communication_processing.models import DiarizationSegmentInput

FIXTURE_MODEL_NAME = "fixture_diarization_adapter"
FIXTURE_MODEL_VERSION = "test-only-1.0.0"


class DiarizationFixtureAdapter:
    """Always `READY`; `diarize` returns the exact segments it was constructed with.

    Never wired into `worker.py` or any other production dispatch path --
    grep for `DiarizationFixtureAdapter` finds only test files.
    """

    def __init__(self, segments: tuple[DiarizationSegmentInput, ...]) -> None:
        self._segments = segments

    @property
    def state(self) -> DiarizationAdapterState:
        return DiarizationAdapterState.READY

    def diarize(self, audio_bytes: bytes, *, filename: str) -> DiarizationResult:
        return DiarizationResult(
            segments=self._segments,
            model_name=FIXTURE_MODEL_NAME,
            model_version=FIXTURE_MODEL_VERSION,
            config_hash="fixture",
        )


__all__ = ["FIXTURE_MODEL_NAME", "FIXTURE_MODEL_VERSION", "DiarizationFixtureAdapter"]
