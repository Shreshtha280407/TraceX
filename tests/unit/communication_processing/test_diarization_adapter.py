"""Scenarios 8, 9: diarization adapter boundary -- unavailable in production, fixture in tests."""

from __future__ import annotations

import pytest

from app.modules.communication_processing.audio.diarization_adapter import (
    DiarizationAdapter,
    DiarizationAdapterState,
    UnavailableDiarizationAdapter,
)
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import DiarizationSegmentInput
from tests.fixtures.communication_processing.diarization_fixture_adapter import (
    FIXTURE_MODEL_NAME,
    FIXTURE_MODEL_VERSION,
    DiarizationFixtureAdapter,
)


def test_unavailable_adapter_reports_unavailable() -> None:
    adapter = UnavailableDiarizationAdapter()
    assert adapter.state is DiarizationAdapterState.UNAVAILABLE


def test_unavailable_adapter_never_fabricates_speaker_turns() -> None:
    """Scenario 9: no local model, so `diarize` must fail loudly, never invent turns."""
    adapter = UnavailableDiarizationAdapter()
    with pytest.raises(ProcessingError) as exc_info:
        adapter.diarize(b"", filename="evidence.wav")
    assert exc_info.value.code == ErrorCode.DIARIZATION_ADAPTER_UNAVAILABLE


def test_unavailable_adapter_satisfies_the_protocol() -> None:
    assert isinstance(UnavailableDiarizationAdapter(), DiarizationAdapter)


def test_fixture_adapter_satisfies_the_protocol() -> None:
    assert isinstance(DiarizationFixtureAdapter(segments=()), DiarizationAdapter)


def test_fixture_adapter_is_ready_and_labelled_as_a_fixture() -> None:
    """Scenario 8: real diarization is never claimed -- the fixture path says so plainly."""
    adapter = DiarizationFixtureAdapter(segments=())
    assert adapter.state is DiarizationAdapterState.READY
    result = adapter.diarize(b"", filename="evidence.wav")
    assert result.model_name == FIXTURE_MODEL_NAME == "fixture_diarization_adapter"
    assert result.model_version == FIXTURE_MODEL_VERSION
    assert "fixture" in result.model_name


def test_fixture_adapter_returns_exactly_the_segments_it_was_given() -> None:
    segment = DiarizationSegmentInput(
        start_ms=0,
        end_ms=800,
        speaker_label="SPEAKER_00",
        confidence=0.8,
        source_segment_id="seg-1",
    )
    adapter = DiarizationFixtureAdapter(segments=(segment,))
    result = adapter.diarize(b"", filename="evidence.wav")
    assert result.segments == (segment,)
