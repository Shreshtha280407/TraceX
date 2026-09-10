"""Scenario 3: unsupported audio format is deferred/rejected without fabricated metadata."""

from __future__ import annotations

from app.modules.communication_processing.audio.routing import (
    AudioRoutingDecision,
    route_audio_metadata,
)
from tests.fixtures.communication_processing.builders import (
    build_fake_m4a_bytes,
    build_fake_mp3_bytes,
    build_fake_ogg_bytes,
    build_wav_bytes,
)


def test_wav_extension_and_magic_routes_supported() -> None:
    assert (
        route_audio_metadata("evidence.wav", build_wav_bytes())
        is AudioRoutingDecision.SUPPORTED_METADATA_ONLY
    )


def test_mp3_extension_routes_unsupported_without_reading_content() -> None:
    assert (
        route_audio_metadata("evidence.mp3", build_fake_mp3_bytes())
        is AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT
    )


def test_m4a_extension_routes_unsupported() -> None:
    assert (
        route_audio_metadata("evidence.m4a", build_fake_m4a_bytes())
        is AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT
    )


def test_ogg_extension_routes_unsupported() -> None:
    assert (
        route_audio_metadata("evidence.ogg", build_fake_ogg_bytes())
        is AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT
    )


def test_no_extension_sniffs_mp3_magic_bytes() -> None:
    assert (
        route_audio_metadata("evidence", build_fake_mp3_bytes())
        is AudioRoutingDecision.UNSUPPORTED_AUDIO_FORMAT
    )


def test_no_extension_sniffs_wav_magic_bytes() -> None:
    assert (
        route_audio_metadata("evidence", build_wav_bytes())
        is AudioRoutingDecision.SUPPORTED_METADATA_ONLY
    )


def test_wav_extension_with_mismatched_content_is_invalid() -> None:
    """A `.wav` label whose bytes don't actually sniff as RIFF/WAVE is rejected,
    never silently reinterpreted as the mismatched real format."""
    assert (
        route_audio_metadata("evidence.wav", build_fake_mp3_bytes())
        is AudioRoutingDecision.INVALID_INPUT
    )


def test_unrecognized_extension_and_content_is_invalid() -> None:
    assert (
        route_audio_metadata("evidence.xyz", b"random junk bytes")
        is AudioRoutingDecision.INVALID_INPUT
    )
