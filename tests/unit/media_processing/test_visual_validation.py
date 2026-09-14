"""Phase 5B producer-side visual provenance and quality validation tests."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.contracts.common import BoundingBoxNormalized, SourceLocator
from app.modules.evidence_lifecycle.media_orchestration import ChunkBoundary
from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.analysis.iou_tracker import IoUTracker
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import VideoMetadata
from app.modules.media_processing.provenance import build_extractor
from app.modules.media_processing.visual_validation import (
    VisualSignalOutcome,
    frame_number_for_time,
    validate_visual_signal,
)

_METADATA = VideoMetadata(
    container_format="mp4",
    duration_ms=3_000,
    width=100,
    height=100,
    frame_rate=10.0,
    frame_count=30,
    video_codec="h264",
    has_audio=False,
    rotation_degrees=None,
)


def _extractor():
    return build_extractor(
        processor_name="media_detection_v1", processor_version="1", model_version="v1"
    )


def _box() -> BoundingBoxNormalized:
    return BoundingBoxNormalized(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.8)


def test_valid_image_geometry_is_accepted_with_safe_quality_metadata() -> None:
    result = validate_visual_signal(
        locator=SourceLocator(bbox_xyxy_normalized=_box()),
        extractor=_extractor(),
        require_bbox=True,
    )
    assert result.outcome is VisualSignalOutcome.ACCEPTED
    assert result.correlation_ready is True
    assert result.attribute_value()["reason_codes"] == []


@pytest.mark.parametrize(
    "values",
    [
        (0.5, 0.1, 0.2, 0.9),
        (-0.1, 0.1, 0.2, 0.9),
        (0.1, 0.1, math.nan, 0.9),
    ],
)
def test_invalid_normalized_geometry_is_rejected_by_the_frozen_contract(values) -> None:
    with pytest.raises(ValidationError):
        BoundingBoxNormalized(x_min=values[0], y_min=values[1], x_max=values[2], y_max=values[3])


def test_valid_video_frame_is_bounded_by_media_and_processed_chunk() -> None:
    result = validate_visual_signal(
        locator=SourceLocator(
            frame_number=10, time_start_ms=1_000, time_end_ms=1_100, bbox_xyxy_normalized=_box()
        ),
        extractor=_extractor(),
        video_metadata=_METADATA,
        chunk_boundary=ChunkBoundary(
            time_start_ms=500, time_end_ms=1_500, frame_start=5, frame_end=15
        ),
        require_bbox=True,
    )
    assert result.outcome is VisualSignalOutcome.ACCEPTED


def test_video_time_outside_chunk_is_rejected_and_unknown_frame_mapping_is_incomplete() -> None:
    outside = validate_visual_signal(
        locator=SourceLocator(
            frame_number=20, time_start_ms=2_000, time_end_ms=2_100, bbox_xyxy_normalized=_box()
        ),
        extractor=_extractor(),
        video_metadata=_METADATA,
        chunk_boundary=ChunkBoundary(time_start_ms=0, time_end_ms=1_500),
        require_bbox=True,
    )
    assert outside.outcome is VisualSignalOutcome.REJECTED
    assert "time_outside_processed_chunk" in outside.reason_codes

    unknown = validate_visual_signal(
        locator=SourceLocator(time_start_ms=0, time_end_ms=100, bbox_xyxy_normalized=_box()),
        extractor=_extractor(),
        video_metadata=_METADATA,
        require_bbox=True,
    )
    assert unknown.outcome is VisualSignalOutcome.INCOMPLETE
    assert unknown.correlation_ready is False


def test_frame_timestamp_mapping_is_deterministic() -> None:
    assert frame_number_for_time(_METADATA, 1_000) == 10
    assert frame_number_for_time(_METADATA, 1_000) == 10


def test_local_track_lifecycle_is_source_local_and_reappearance_is_unlinked() -> None:
    detection = ObjectDetection("person", 0.9, PixelBoundingBox(1, 1, 20, 20))
    tracker = IoUTracker()
    segments = tracker.track({0: [detection], 1_000: [], 2_000: [detection]})
    assert len(segments) == 2
    assert all("source_local" in item.attributes["track_lifecycle_conditions"] for item in segments)
    assert "reappearance_unlinked" in segments[1].attributes["track_lifecycle_conditions"]
    assert not hasattr(segments[0], "entity_id")


def test_frame_time_mismatch_is_rejected_before_correlation_readiness() -> None:
    result = validate_visual_signal(
        locator=SourceLocator(
            frame_number=4, time_start_ms=1_000, time_end_ms=1_100, bbox_xyxy_normalized=_box()
        ),
        extractor=_extractor(),
        video_metadata=_METADATA,
        require_bbox=True,
    )
    assert result.outcome is VisualSignalOutcome.REJECTED
    assert result.correlation_ready is False
