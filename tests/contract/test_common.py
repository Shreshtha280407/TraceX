"""Contract tests for shared primitives: SourceLocator, BoundingBox, TimeWindow.

Covers CORE-CONTRACT-002 (locator/bbox/time-range edge cases) — see
docs/qa/test-matrix.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts.common import BoundingBoxNormalized, SourceLocator, TimeWindow


def test_source_locator_with_single_field_is_valid() -> None:
    locator = SourceLocator(page=3)
    assert locator.page == 3


def test_empty_source_locator_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceLocator()


def test_source_locator_invalid_time_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceLocator(time_start_ms=500, time_end_ms=100)


def test_source_locator_equal_time_bounds_is_valid() -> None:
    locator = SourceLocator(time_start_ms=500, time_end_ms=500)
    assert locator.time_end_ms == locator.time_start_ms


def test_source_locator_invalid_span_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceLocator(span_start=50, span_end=10)


def test_bounding_box_valid() -> None:
    box = BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.5)
    assert box.x_max > box.x_min


@pytest.mark.parametrize(
    "coords",
    [
        {"x_min": 0.5, "y_min": 0.1, "x_max": 0.1, "y_max": 0.5},  # x_min >= x_max
        {"x_min": 0.1, "y_min": 0.5, "x_max": 0.5, "y_max": 0.1},  # y_min >= y_max
        {"x_min": -0.1, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5},  # out of [0,1]
        {"x_min": 0.0, "y_min": 0.0, "x_max": 1.1, "y_max": 0.5},  # out of [0,1]
    ],
)
def test_bounding_box_invalid_geometry_is_rejected(coords: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        BoundingBoxNormalized(**coords)


def test_source_locator_with_bbox_is_valid() -> None:
    box = BoundingBoxNormalized(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)
    locator = SourceLocator(bbox_xyxy_normalized=box)
    assert locator.bbox_xyxy_normalized == box


def test_time_window_valid() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    window = TimeWindow(start=start, end=end)
    assert window.end > window.start


def test_time_window_invalid_range_is_rejected() -> None:
    start = datetime(2026, 1, 2, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        TimeWindow(start=start, end=end)
