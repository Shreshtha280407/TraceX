from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pytest

from app.modules.media_processing.models import VideoMetadata
from app.modules.media_processing.phase4 import (
    DEEP_PROFILE,
    RAPID_PROFILE,
    FrameSignal,
    normalized_frame_difference,
    plan_manifest,
    safe_visual_attributes,
    select_adaptive_samples,
)


def _metadata() -> VideoMetadata:
    return VideoMetadata("mp4", 65_000, 1920, 1080, 25.0, 1625, "h264", False, None)


def test_profile_hashes_and_manifest_planning_are_deterministic() -> None:
    values = {
        "case_id": uuid4(),
        "evidence_id": uuid4(),
        "job_id": uuid4(),
        "input_object_uri": "evidence/input",
        "source_type": "video",
        "processor_name": "media_detection_v1",
        "processor_version": "1.0.0",
        "metadata": _metadata(),
        "profile": RAPID_PROFILE,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    first, second = plan_manifest(**values), plan_manifest(**values)
    assert RAPID_PROFILE.config_hash() == RAPID_PROFILE.config_hash()
    assert RAPID_PROFILE.config_hash() != DEEP_PROFILE.config_hash()
    assert first.manifest_id == second.manifest_id
    assert [chunk.boundary.time_start_ms for chunk in first.chunks] == [0, 60_000]


def test_adaptive_sampling_is_stable_bounded_and_deduplicated() -> None:
    selected = select_adaptive_samples(
        start_ms=0,
        end_ms=10_000,
        profile=RAPID_PROFILE,
        signals=(
            FrameSignal(2000, 0.4, 0.0),
            FrameSignal(2500, 0.0, 0.3),
            FrameSignal(2500, 0.4, 0.3),
        ),
    )
    assert [sample.timestamp.time_ms for sample in selected] == sorted(
        {sample.timestamp.time_ms for sample in selected}
    )
    assert next(sample for sample in selected if sample.timestamp.time_ms == 2500).reasons == (
        "motion",
        "scene_change",
    )
    assert len(selected) <= RAPID_PROFILE.max_frames_per_chunk


def test_motion_signal_and_safe_attributes_do_not_claim_identity() -> None:
    assert (
        normalized_frame_difference(
            np.zeros((2, 2, 3), dtype=np.uint8), np.full((2, 2, 3), 255, dtype=np.uint8)
        )
        == 1.0
    )
    assert (
        normalized_frame_difference(
            np.zeros((2, 2), dtype=np.uint8), np.zeros((2, 3), dtype=np.uint8)
        )
        == 0.0
    )
    attrs = safe_visual_attributes(
        detected_label="person", profile=RAPID_PROFILE, decode_mode="cpu", local_track_id="local-1"
    )
    assert attrs["candidate_only"] is True
    assert "entity_id" not in attrs


def test_invalid_chunk_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="end"):
        select_adaptive_samples(start_ms=2, end_ms=1, profile=RAPID_PROFILE)
