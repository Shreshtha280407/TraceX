"""Scenarios 6, 7, 9: the ASR adapter boundary -- unavailable in production, fixture in tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.modules.communication_processing.audio.asr_adapter import (
    AsrAdapter,
    AsrAdapterState,
    LocalCommandAsrAdapter,
    UnavailableAsrAdapter,
)
from app.modules.communication_processing.errors import ErrorCode, ProcessingError
from app.modules.communication_processing.models import TranscriptSegmentInput
from tests.fixtures.communication_processing.asr_fixture_adapter import (
    FIXTURE_MODEL_NAME,
    FIXTURE_MODEL_VERSION,
    AsrFixtureAdapter,
)


def test_unavailable_adapter_reports_unavailable() -> None:
    adapter = UnavailableAsrAdapter()
    assert adapter.state is AsrAdapterState.UNAVAILABLE


def test_unavailable_adapter_never_fabricates_a_transcript() -> None:
    """Scenario 9: no local model, so `transcribe` must fail loudly, never invent text."""
    adapter = UnavailableAsrAdapter()
    with pytest.raises(ProcessingError) as exc_info:
        adapter.transcribe(b"", filename="evidence.wav")
    assert exc_info.value.code == ErrorCode.ASR_ADAPTER_UNAVAILABLE


def test_unavailable_adapter_satisfies_the_protocol() -> None:
    assert isinstance(UnavailableAsrAdapter(), AsrAdapter)


def test_fixture_adapter_satisfies_the_protocol() -> None:
    """Scenario 6/7 prerequisite: the `Protocol` is genuinely implementable."""
    assert isinstance(AsrFixtureAdapter(segments=()), AsrAdapter)


def test_fixture_adapter_is_ready_and_labelled_as_a_fixture() -> None:
    """Scenario 7: the fixture path is explicit and never presented as real ASR output."""
    adapter = AsrFixtureAdapter(segments=())
    assert adapter.state is AsrAdapterState.READY
    result = adapter.transcribe(b"", filename="evidence.wav")
    assert result.model_name == FIXTURE_MODEL_NAME == "fixture_asr_adapter"
    assert result.model_version == FIXTURE_MODEL_VERSION
    assert "fixture" in result.model_name


def test_fixture_adapter_returns_exactly_the_segments_it_was_given() -> None:
    segment = TranscriptSegmentInput(
        start_ms=0,
        end_ms=1000,
        text="hello",
        language_hint=None,
        confidence=0.9,
        source_segment_id="seg-1",
    )
    adapter = AsrFixtureAdapter(segments=(segment,))
    result = adapter.transcribe(b"", filename="evidence.wav")
    assert result.segments == (segment,)


def test_local_command_adapter_validates_configured_offline_json_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = tmp_path / "asr-bridge"
    model = tmp_path / "model.bin"
    command.touch()
    model.touch()
    adapter = LocalCommandAsrAdapter(
        command=command, model_path=model, language="en", timeout_seconds=1
    )
    monkeypatch.setattr(
        "app.modules.communication_processing.audio.asr_adapter.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"model_name":"local-test","model_version":"1",'
                '"segments":[{"start_ms":0,"end_ms":10,"text":"test",'
                '"confidence":0.8}]}'
            ),
        ),
    )

    result = adapter.transcribe(b"wav", filename="ignored.wav")

    assert adapter.state is AsrAdapterState.READY
    assert result.segments[0].start_ms == 0
    assert result.segments[0].end_ms == 10
    assert result.backend_name == "local_command_asr"
