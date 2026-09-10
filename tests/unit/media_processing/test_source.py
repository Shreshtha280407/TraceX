"""Scenario 1: valid/invalid media content classification, plus the source-resolver boundary."""

from __future__ import annotations

import pytest

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.source import (
    MediaKind,
    StaticBytesResolver,
    classify_media,
    is_video,
    temporary_media_file,
)


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("video/mp4", "clip.mp4", MediaKind.VIDEO_MP4),
        ("video/quicktime", "clip.mov", MediaKind.VIDEO_QUICKTIME),
        ("video/x-msvideo", "clip.avi", MediaKind.VIDEO_X_MSVIDEO),
        ("image/jpeg", "photo.jpg", MediaKind.IMAGE_JPEG),
        ("image/jpeg", "photo.jpeg", MediaKind.IMAGE_JPEG),
        ("image/png", "photo.png", MediaKind.IMAGE_PNG),
        ("image/webp", "photo.webp", MediaKind.IMAGE_WEBP),
        ("  IMAGE/PNG  ", "photo.png", MediaKind.IMAGE_PNG),
    ],
)
def test_classify_media_accepts_supported_combinations(
    content_type: str, filename: str, expected: MediaKind
) -> None:
    assert classify_media(content_type, filename) == expected


def test_classify_media_rejects_unsupported_content_type() -> None:
    with pytest.raises(ProcessingError) as excinfo:
        classify_media("audio/mpeg", "voice.mp3")
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_classify_media_rejects_extension_mismatch() -> None:
    with pytest.raises(ProcessingError) as excinfo:
        classify_media("video/mp4", "clip.avi")
    assert excinfo.value.code == ErrorCode.UNSUPPORTED_CONTENT_TYPE


def test_classify_media_rejects_extensionless_filename() -> None:
    with pytest.raises(ProcessingError):
        classify_media("image/png", "photo")


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (MediaKind.VIDEO_MP4, True),
        (MediaKind.VIDEO_QUICKTIME, True),
        (MediaKind.VIDEO_X_MSVIDEO, True),
        (MediaKind.IMAGE_JPEG, False),
        (MediaKind.IMAGE_PNG, False),
        (MediaKind.IMAGE_WEBP, False),
    ],
)
def test_is_video(kind: MediaKind, expected: bool) -> None:
    assert is_video(kind) is expected


def test_static_bytes_resolver_returns_fixed_payload_regardless_of_uri() -> None:
    resolver = StaticBytesResolver(payload=b"fixed-payload")
    assert resolver.read_bytes("local://anything") == b"fixed-payload"
    assert resolver.read_bytes("local://something-else") == b"fixed-payload"


def test_temporary_media_file_writes_and_cleans_up() -> None:
    with temporary_media_file(b"hello world", suffix=".bin") as path:
        assert path.exists()
        assert path.read_bytes() == b"hello world"
        captured_path = path
    assert not captured_path.exists()


def test_temporary_media_file_cleans_up_even_on_error() -> None:
    captured_path = None
    with pytest.raises(RuntimeError), temporary_media_file(b"data", suffix=".bin") as path:
        captured_path = path
        raise RuntimeError("boom")
    assert captured_path is not None
    assert not captured_path.exists()
