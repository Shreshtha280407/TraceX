from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.modules.communication_processing.phase4 import (
    DEEP_AUDIO_PROFILE,
    RAPID_AUDIO_PROFILE,
    DiarizationDecision,
    SpeechInterval,
    detect_script_hint,
    diarization_decision,
    normalize_vad_intervals,
    plan_audio_manifest,
)


def test_vad_is_deterministic_bounded_and_silence_is_valid() -> None:
    intervals = normalize_vad_intervals(
        (SpeechInterval(0, 1000), SpeechInterval(1100, 2000)), RAPID_AUDIO_PROFILE
    )
    assert intervals == (SpeechInterval(0, 2000),)
    assert normalize_vad_intervals((), RAPID_AUDIO_PROFILE) == ()
    with pytest.raises(ValueError, match="overlap"):
        normalize_vad_intervals(
            (SpeechInterval(0, 1000), SpeechInterval(900, 1200)), RAPID_AUDIO_PROFILE
        )


def test_diarization_policy_and_script_hint_are_explicit() -> None:
    assert (
        diarization_decision(profile=RAPID_AUDIO_PROFILE, intervals=(), adapter_ready=True)
        is DiarizationDecision.SKIP_DISABLED
    )
    assert (
        diarization_decision(
            profile=DEEP_AUDIO_PROFILE, intervals=(SpeechInterval(0, 25_000),), adapter_ready=False
        )
        is DiarizationDecision.UNAVAILABLE
    )
    assert detect_script_hint("नमस्ते")[0] == "devanagari"
    assert detect_script_hint("123")[0] == "unknown"


def test_audio_manifest_is_scoped_and_deterministic() -> None:
    values = {
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "job_id": uuid4(),
        "input_object_uri": "evidence/audio",
        "source_type": "audio",
        "processor_name": "audio_asr_v1",
        "processor_version": "1",
        "duration_ms": 61_000,
        "profile": RAPID_AUDIO_PROFILE,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    assert plan_audio_manifest(**values).manifest_id == plan_audio_manifest(**values).manifest_id
    assert RAPID_AUDIO_PROFILE.config_hash() != DEEP_AUDIO_PROFILE.config_hash()
