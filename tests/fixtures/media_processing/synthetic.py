"""Synthetic, small, non-sensitive media builders for tests.

`make_png_bytes`/`make_solid_frame` need no external binary and are safe
for unit tests. `make_synthetic_mp4_bytes` shells out to `ffmpeg`'s `lavfi`
test-source filter and is used only by integration tests, which self-skip
when `ffmpeg` is genuinely unavailable.

`make_text_png_bytes`/`make_text_jpeg_bytes`/`make_text_frame` render
labelled synthetic text at known coordinates -- used by OCR bounding-box
correctness tests (Phase 3, Gaurav). They require only Pillow (always
available) and optionally a system TrueType font. When no font is found
they fall back to Pillow's default bitmap font (small and unreliable for
real OCR, but still correct for geometry tests that use ``FixtureOcrAdapter``).

`make_text_video_bytes` builds a real, tiny MP4 by looping one labelled
text PNG frame through `ffmpeg` -- used by the video-frame real-OCR live
test. Requires both `ffmpeg` (`ffmpeg_available()`) and a real TrueType
font (`find_test_font()`); callers must check both first and self-skip
otherwise.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.modules.media_processing.models import Frame

#: TrueType fonts tried in order; the first that exists is used.
#: Used by `make_text_png_bytes` and `make_text_frame` for tests that need
#: real OCR-readable renders (large, high-contrast text).  Tests that only
#: need synthetic geometry (``FixtureOcrAdapter``) never need a font at all.
CANDIDATE_FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


def find_test_font() -> str | None:
    """Return the path to the first available TrueType font, or None."""
    for candidate in CANDIDATE_FONT_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


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


def make_text_frame(
    text: str,
    *,
    width: int = 400,
    height: int = 100,
    font_path: str | None = None,
    font_size: int = 48,
    text_x: int = 20,
    text_y: int = 20,
    bg_color: tuple[int, int, int] = (255, 255, 255),
    fg_color: tuple[int, int, int] = (0, 0, 0),
) -> Frame:
    """A synthetic RGB ``Frame`` (numpy array) with ``text`` rendered at a
    known pixel position.

    ``text_x``/``text_y`` are the top-left anchor of the drawn text.
    When ``font_path`` is ``None``, Pillow's default bitmap font is used
    (good enough for geometry tests; too small for real Tesseract runs).

    Returns a ``(height, width, 3)`` uint8 numpy array -- the same shape
    ``decode_image`` and ``extract_frames`` produce.
    """
    image = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(image)
    if font_path is not None:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype(
            font_path, font_size
        )
    else:
        font = ImageFont.load_default()
    draw.text((text_x, text_y), text, fill=fg_color, font=font)
    return np.asarray(image, dtype=np.uint8)


def make_text_png_bytes(
    text: str,
    *,
    width: int = 400,
    height: int = 100,
    font_path: str | None = None,
    font_size: int = 48,
    text_x: int = 20,
    text_y: int = 20,
    bg_color: tuple[int, int, int] = (255, 255, 255),
    fg_color: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """Synthetic PNG bytes with ``text`` rendered at a known pixel position.

    Useful for bounding-box geometry tests (``FixtureOcrAdapter``) and,
    when a real TrueType font is available, for real Tesseract OCR tests.
    """
    frame = make_text_frame(
        text,
        width=width,
        height=height,
        font_path=font_path,
        font_size=font_size,
        text_x=text_x,
        text_y=text_y,
        bg_color=bg_color,
        fg_color=fg_color,
    )
    image = Image.fromarray(frame, mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_text_jpeg_bytes(
    text: str,
    *,
    width: int = 400,
    height: int = 100,
    font_path: str | None = None,
    font_size: int = 48,
    text_x: int = 20,
    text_y: int = 20,
    bg_color: tuple[int, int, int] = (255, 255, 255),
    fg_color: tuple[int, int, int] = (0, 0, 0),
    quality: int = 95,
) -> bytes:
    """Synthetic JPEG bytes with ``text`` rendered at a known pixel position.

    JPEG compression may slightly alter pixel values but the text geometry
    is stable enough for bounding-box tests.
    """
    frame = make_text_frame(
        text,
        width=width,
        height=height,
        font_path=font_path,
        font_size=font_size,
        text_x=text_x,
        text_y=text_y,
        bg_color=bg_color,
        fg_color=fg_color,
    )
    image = Image.fromarray(frame, mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_text_video_bytes(
    text: str,
    *,
    width: int = 640,
    height: int = 180,
    font_path: str,
    font_size: int = 72,
    text_x: int = 30,
    text_y: int = 40,
    duration_seconds: float = 1.0,
    fps: float = 5.0,
) -> bytes:
    """A tiny, synthetic MP4 built by looping one labelled text PNG frame.

    Reuses `make_text_png_bytes`'s exact rendering, so a real sampled video
    frame carries the same real-Tesseract-readable text the image-path OCR
    tests already prove -- without depending on ffmpeg's `drawtext` filter
    (which needs a fontconfig/freetype build not guaranteed present).
    Requires `ffmpeg` on `PATH`; callers must check `ffmpeg_available()`
    first and self-skip otherwise (see `tests/integration/media_processing/`).
    """
    frame_png = make_text_png_bytes(
        text,
        width=width,
        height=height,
        font_path=font_path,
        font_size=font_size,
        text_x=text_x,
        text_y=text_y,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        frame_path = Path(tmp_dir) / "frame.png"
        frame_path.write_bytes(frame_png)
        output_path = Path(tmp_dir) / "synthetic_text.mp4"
        subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only helper
            [
                "ffmpeg",
                "-v",
                "error",
                "-loop",
                "1",
                "-i",
                str(frame_path),
                "-t",
                str(duration_seconds),
                "-vf",
                f"fps={fps}",
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
