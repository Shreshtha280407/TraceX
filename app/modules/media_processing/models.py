"""Shared types for the media-processing module.

Kept here (rather than duplicated per-submodule) because the same shapes
flow across `video/`, `image/`, and `analysis/`: a decoded video frame and a
decoded image are both a plain RGB pixel array (`Frame`), and both video
detections and image detections end up as the same
`MediaObservationDraft` shape on their way to `provenance.py`. Mirrors the
role `app.modules.structured_processing.models` plays for that module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import numpy.typing as npt
from pydantic import JsonValue

from app.contracts.common import SourceLocator

#: A decoded frame or image: an RGB, uint8, (height, width, 3) array. Shared
#: by video frame extraction and image decoding so every downstream
#: analysis interface (detector/tracker/OCR) has exactly one pixel-data
#: shape to accept, regardless of source modality.
Frame = npt.NDArray[np.uint8]


class SamplingStrategy(StrEnum):
    """Deterministic video frame-sampling strategies.

    See `docs/architecture/media-processing-v1.md` for the exact policy of
    each strategy. No strategy here is adaptive/ML-based -- that is
    explicitly out of scope for this phase.
    """

    UNIFORM_INTERVAL = "uniform_interval"
    FIXED_FPS = "fixed_fps"
    EXPLICIT_TIMESTAMPS = "explicit_timestamps"


@dataclass(frozen=True)
class VideoMetadata:
    """Safe video metadata as extracted by the `ffprobe` adapter.

    Every field except `duration_ms`/`width`/`height` may be `None` --
    `frame_count`, `video_codec`, `has_audio`, and `rotation_degrees` are
    not always present or trustworthy in a probed container, and this
    module never guesses a value it cannot verify (see
    `video/probe.py`).
    """

    container_format: str
    duration_ms: int
    width: int
    height: int
    frame_rate: float | None
    frame_count: int | None
    video_codec: str | None
    has_audio: bool
    rotation_degrees: int | None


@dataclass(frozen=True)
class ImageMetadata:
    """Safe image metadata as extracted by the image decoder."""

    width: int
    height: int
    format: str
    color_mode: str | None
    orientation: int | None


@dataclass(frozen=True)
class SampleTimestamp:
    """One deterministic sampling instant, produced by `video/sampling.py`.

    `index` is the timestamp's ordinal position within its sample plan --
    used as a discriminator so two timestamps that happen to land on the
    same millisecond (never expected, but never assumed impossible either)
    still produce distinct observation IDs.
    """

    time_ms: int
    index: int


@dataclass(frozen=True)
class SamplingRequest:
    """Caller-specified sampling configuration for one video.

    Exactly one of `interval_ms` (`UNIFORM_INTERVAL`), `fps` (`FIXED_FPS`),
    or `explicit_timestamps_ms` (`EXPLICIT_TIMESTAMPS`) is read, matching
    `strategy` -- see `video/sampling.py::build_sample_plan`.
    """

    strategy: SamplingStrategy
    max_frames: int
    interval_ms: int | None = None
    fps: float | None = None
    explicit_timestamps_ms: tuple[int, ...] = ()


@dataclass(frozen=True)
class ExtractedFrame:
    """One video frame extracted at a `SampleTimestamp`.

    `frame_number` is `None` whenever the timestamp-to-frame mapping is not
    reliably known (the common case for variable-frame-rate containers or
    when the probe could not determine `frame_count`) -- see
    `video/frames.py`. This is a transient runtime artifact only: it is
    never persisted or embedded in an `ObservationV1`.
    """

    frame_number: int | None
    time_start_ms: int
    time_end_ms: int
    width: int
    height: int
    image: Frame


@dataclass(frozen=True)
class MediaObservationDraft:
    """One extracted, unresolved fact, ready to become one `ObservationV1`.

    Shared by media-metadata, detection, tracking, and OCR observation
    construction in `provenance.py` -- every one of these ends with "here
    is a small piece of information, here is exactly where in the evidence
    it came from, here is how confident we are," the same shape
    `app.modules.structured_processing.models.RawMention` uses for
    document/structured sources.
    """

    observation_type: str
    locator: SourceLocator
    confidence: float
    entity_text: str | None = None
    entity_type_hint: str | None = None
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    discriminator: str = ""
