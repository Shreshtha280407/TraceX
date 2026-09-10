"""Safe `ffprobe` adapter: extracts video metadata without trusting its output.

Every field is defensively parsed -- a missing, malformed, or absent value
never crashes the probe, it just leaves that field `None` (see
`app.modules.media_processing.models.VideoMetadata`). `duration_ms`/
`width`/`height` are the exception: without them nothing downstream
(limits, sampling) can work safely, so their absence is a hard
`media_probe_failed`. Never logs or raises with the file path, or with raw
`ffprobe` stdout/stderr -- only a safe, generic description of the failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.modules.media_processing.errors import ErrorCode, ProcessingError
from app.modules.media_processing.limits import MediaLimits
from app.modules.media_processing.models import VideoMetadata


def _parse_rate(raw: str | None) -> float | None:
    if not raw or "/" not in raw:
        return None
    numerator_str, _, denominator_str = raw.partition("/")
    try:
        numerator, denominator = float(numerator_str), float(denominator_str)
    except ValueError:
        return None
    if denominator <= 0 or numerator <= 0:
        return None
    return numerator / denominator


def _parse_frame_count(stream: dict[str, Any]) -> int | None:
    raw = stream.get("nb_frames")
    if raw is None:
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _parse_rotation(stream: dict[str, Any]) -> int | None:
    tags = stream.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        try:
            return int(tags["rotate"])
        except (TypeError, ValueError):
            pass
    for side_data in stream.get("side_data_list", []) or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"])))
            except (TypeError, ValueError):
                continue
    return None


def probe_video(path: Path, *, limits: MediaLimits) -> VideoMetadata:
    """Probe a local video file with `ffprobe` and return safe metadata.

    Raises `ProcessingError(FFPROBE_UNAVAILABLE)` if the binary is missing,
    or `ProcessingError(MEDIA_PROBE_FAILED)` if the file cannot be probed or
    lacks a usable video stream/duration/dimensions.
    """
    if shutil.which("ffprobe") is None:
        raise ProcessingError(ErrorCode.FFPROBE_UNAVAILABLE, "ffprobe is not available")

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, path is a local temp file
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=limits.subprocess_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessingError(
            ErrorCode.MEDIA_PROBE_FAILED, "ffprobe could not be executed against this input"
        ) from exc

    if completed.returncode != 0:
        raise ProcessingError(ErrorCode.MEDIA_PROBE_FAILED, "ffprobe reported the input as invalid")

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProcessingError(
            ErrorCode.MEDIA_PROBE_FAILED, "ffprobe returned an unparseable result"
        ) from exc

    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise ProcessingError(ErrorCode.MEDIA_PROBE_FAILED, "ffprobe result has no stream list")

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise ProcessingError(ErrorCode.MEDIA_PROBE_FAILED, "no video stream found in this input")

    format_block = payload.get("format", {})
    if not isinstance(format_block, dict):
        format_block = {}

    try:
        width = int(video_stream["width"])
        height = int(video_stream["height"])
        if width <= 0 or height <= 0:
            raise ValueError("non-positive dimension")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProcessingError(
            ErrorCode.MEDIA_PROBE_FAILED, "video stream is missing usable width/height"
        ) from exc

    duration_raw = format_block.get("duration") or video_stream.get("duration")
    try:
        duration_ms = int(round(float(duration_raw) * 1000))
        if duration_ms < 0:
            raise ValueError("negative duration")
    except (TypeError, ValueError) as exc:
        raise ProcessingError(
            ErrorCode.MEDIA_PROBE_FAILED, "could not determine a usable video duration"
        ) from exc

    frame_rate = _parse_rate(video_stream.get("avg_frame_rate")) or _parse_rate(
        video_stream.get("r_frame_rate")
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    return VideoMetadata(
        container_format=str(format_block.get("format_name", "unknown")),
        duration_ms=duration_ms,
        width=width,
        height=height,
        frame_rate=frame_rate,
        frame_count=_parse_frame_count(video_stream),
        video_codec=(
            str(video_stream["codec_name"]) if video_stream.get("codec_name") is not None else None
        ),
        has_audio=has_audio,
        rotation_degrees=_parse_rotation(video_stream),
    )
