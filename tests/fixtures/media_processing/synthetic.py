"""Synthetic, small, non-sensitive media builders for tests.

`make_png_bytes`/`make_solid_frame` need no external binary and are safe
for unit tests. `make_synthetic_mp4_bytes` shells out to `ffmpeg`'s `lavfi`
test-source filter and is used only by integration tests, which self-skip
when `ffmpeg` is genuinely unavailable.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from app.modules.media_processing.models import Frame


def make_png_bytes(
    *, width: int = 64, height: int = 48, color: tuple[int, int, int] = (10, 20, 30)
) -> bytes:
    """A tiny, solid-color, synthetic PNG -- no real photograph involved."""
    image = Image.new("RGB", (width, height), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_solid_frame(*, width: int = 64, height: int = 48, value: int = 128) -> Frame:
    """A synthetic in-memory RGB frame, for analysis-interface tests that
    need no decode step at all."""
    return np.full((height, width, 3), value, dtype=np.uint8)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_synthetic_mp4_bytes(
    *, width: int = 64, height: int = 48, duration_seconds: float = 2.0, fps: float = 5.0
) -> bytes:
    """A tiny, synthetic, pattern-generated MP4 -- no real footage involved.

    Requires `ffmpeg` on `PATH`; callers must check `ffmpeg_available()`
    first and self-skip otherwise (see
    `tests/integration/media_processing/`).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "synthetic.mp4"
        subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only helper
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={duration_seconds}:size={width}x{height}:rate={fps}",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return output_path.read_bytes()
