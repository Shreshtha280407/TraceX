"""Phase 4 deterministic planning and staged-publication helpers.

Pure planning keeps media decoding/model execution replaceable and never writes
to storage or a graph.  A worker supplies actual frame signals and publishes
the resulting canonical batch through its authenticated API client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import numpy as np

from app.contracts.observation import ObservationV1
from app.core.canonical import canonical_sha256
from app.modules.evidence_lifecycle.media_orchestration import (
    ChunkBoundary,
    ChunkManifest,
    ChunkSpec,
    build_manifest,
)
from app.modules.media_processing.models import SampleTimestamp, VideoMetadata


class DecodePreference(StrEnum):
    AUTO = "auto"
    CPU = "cpu"
    HARDWARE = "hardware"


@dataclass(frozen=True)
class ProcessingProfile:
    name: str
    version: str
    baseline_interval_ms: int
    max_frames_per_chunk: int
    chunk_duration_ms: int
    scene_threshold: float
    motion_threshold: float
    ocr_enabled: bool
    max_ocr_regions_per_frame: int
    inference_batch_size: int
    decode_preference: DecodePreference
    detector_identity: str
    tracker_identity: str

    def __post_init__(self) -> None:
        if min(self.baseline_interval_ms, self.max_frames_per_chunk, self.chunk_duration_ms) <= 0:
            raise ValueError("profile limits must be positive")
        if not 0 <= self.scene_threshold <= 1 or not 0 <= self.motion_threshold <= 1:
            raise ValueError("scene and motion thresholds must be within [0, 1]")

    def config_hash(self) -> str:
        return canonical_sha256({"phase4_media_profile": self.__dict__})


RAPID_PROFILE = ProcessingProfile(
    name="rapid",
    version="1.0.0",
    baseline_interval_ms=2000,
    max_frames_per_chunk=40,
    chunk_duration_ms=60_000,
    scene_threshold=0.25,
    motion_threshold=0.12,
    ocr_enabled=True,
    max_ocr_regions_per_frame=5,
    inference_batch_size=4,
    decode_preference=DecodePreference.AUTO,
    detector_identity="local_detector",
    tracker_identity="iou_tracker_v1",
)
DEEP_PROFILE = ProcessingProfile(
    name="deep",
    version="1.0.0",
    baseline_interval_ms=500,
    max_frames_per_chunk=160,
    chunk_duration_ms=30_000,
    scene_threshold=0.15,
    motion_threshold=0.06,
    ocr_enabled=True,
    max_ocr_regions_per_frame=20,
    inference_batch_size=8,
    decode_preference=DecodePreference.AUTO,
    detector_identity="local_detector",
    tracker_identity="iou_tracker_v1",
)


@dataclass(frozen=True)
class FrameSignal:
    time_ms: int
    scene_change: float
    motion: float


@dataclass(frozen=True)
class PlannedSample:
    timestamp: SampleTimestamp
    reasons: tuple[str, ...]


def plan_manifest(
    *,
    case_id: UUID,
    evidence_id: UUID,
    job_id: UUID,
    input_object_uri: str,
    source_type: str,
    processor_name: str,
    processor_version: str,
    metadata: VideoMetadata,
    profile: ProcessingProfile,
    created_at: datetime,
) -> ChunkManifest:
    """Build Nipun's immutable manifest using only deterministic source offsets."""
    chunks = tuple(
        ChunkSpec(
            index=index,
            boundary=ChunkBoundary(
                time_start_ms=start,
                time_end_ms=min(start + profile.chunk_duration_ms, metadata.duration_ms),
            ),
        )
        for index, start in enumerate(
            range(0, metadata.duration_ms or 1, profile.chunk_duration_ms)
        )
    )
    return build_manifest(
        case_id=case_id,
        evidence_id=evidence_id,
        job_id=job_id,
        source_type=source_type,
        processor_name=processor_name,
        processor_version=processor_version,
        input_object_uri=input_object_uri,
        configuration_hash=profile.config_hash(),
        chunks=chunks,
        created_at=created_at,
    )


def select_adaptive_samples(
    *,
    start_ms: int,
    end_ms: int,
    profile: ProcessingProfile,
    signals: tuple[FrameSignal, ...] = (),
) -> tuple[PlannedSample, ...]:
    """Stable baseline plus bounded signal-triggered samples, never duplicates."""
    if end_ms < start_ms:
        raise ValueError("chunk end must not precede start")
    selected: dict[int, set[str]] = {
        value: {"baseline"} for value in range(start_ms, end_ms + 1, profile.baseline_interval_ms)
    }
    selected.setdefault(end_ms, {"chunk_end"})
    for signal in signals:
        if start_ms <= signal.time_ms <= end_ms and (
            signal.scene_change >= profile.scene_threshold
            or signal.motion >= profile.motion_threshold
        ):
            reasons = selected.setdefault(signal.time_ms, set())
            if signal.scene_change >= profile.scene_threshold:
                reasons.add("scene_change")
            if signal.motion >= profile.motion_threshold:
                reasons.add("motion")
    times = sorted(selected)[: profile.max_frames_per_chunk]
    return tuple(
        PlannedSample(SampleTimestamp(time_ms=value, index=index), tuple(sorted(selected[value])))
        for index, value in enumerate(times)
    )


def normalized_frame_difference(previous: np.ndarray, current: np.ndarray) -> float:
    """Explainable local motion/scene signal; input shape mismatch is not comparable."""
    if previous.shape != current.shape or previous.size == 0:
        return 0.0
    return float(np.mean(np.abs(previous.astype(np.float32) - current.astype(np.float32))) / 255.0)


def safe_visual_attributes(
    *,
    detected_label: str,
    profile: ProcessingProfile,
    decode_mode: str,
    local_track_id: str | None = None,
) -> dict[str, str | bool]:
    """Allow-listed graph-safe metadata; labels/tracks are not identities."""
    values: dict[str, str | bool] = {
        "detected_label": detected_label,
        "processing_profile": profile.name,
        "profile_config_hash": profile.config_hash(),
        "decode_mode": decode_mode,
        "candidate_only": True,
    }
    if local_track_id is not None:
        values["local_track_id"] = local_track_id
    return values


def same_case_observations(
    observations: tuple[ObservationV1, ...], case_id: UUID, evidence_id: UUID
) -> bool:
    """Pre-flight guard before Nipun's publication route; no mutation occurs here."""
    return all(item.case_id == case_id and item.evidence_id == evidence_id for item in observations)


__all__ = [
    "DEEP_PROFILE",
    "RAPID_PROFILE",
    "DecodePreference",
    "FrameSignal",
    "PlannedSample",
    "ProcessingProfile",
    "normalized_frame_difference",
    "plan_manifest",
    "safe_visual_attributes",
    "same_case_observations",
    "select_adaptive_samples",
]
