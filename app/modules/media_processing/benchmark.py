"""Reproducible local image/video processing benchmark.

    uv run python -m app.modules.media_processing.benchmark <path/to/image_or_video>

Runs the exact same `process_job` pipeline a real claimed `media_detection_v1`
job would (the real detector/OCR/tracker if their assets/runtime are
configured and available, exactly as `worker.py`'s
`_build_analysis_components` would build them; a plain metadata-only run
otherwise) against a local file already on disk -- no evidence upload, no
worker claim, no PostgreSQL/Neo4j/MinIO connection, no test fixtures.

Reports *measured* results only. Per `performance.py`'s policy, this never
prints or claims a promised or extrapolated throughput figure ("processes
an hour of footage in N minutes") -- only what was actually measured, on
the actual hardware this ran on, for the actual file given, once. Re-run
this command to get a fresh, independently measured result; averaging or
projecting from a single run is left to the operator, not fabricated here.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
    SourceType,
)
from app.contracts.worker import WorkerJobV1
from app.core.config import get_settings
from app.modules.media_processing.performance import Stopwatch, build_performance_report
from app.modules.media_processing.provenance import OBSERVATION_MEDIA_METADATA
from app.modules.media_processing.source import StaticBytesResolver, classify_media, is_video
from app.modules.media_processing.worker import (
    PROCESSOR_NAME_DETECTION,
    PROCESSOR_VERSION,
    _build_analysis_components,
    process_job,
)

_CONTENT_TYPE_BY_EXTENSION = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _guess_content_type(path: Path) -> str:
    content_type = _CONTENT_TYPE_BY_EXTENSION.get(path.suffix.lower())
    if content_type is None:
        raise ValueError(
            f"could not infer a content type from the extension '{path.suffix}'; "
            f"pass --content-type explicitly"
        )
    return content_type


def _build_job_and_evidence(
    *, content_type: str, filename: str
) -> tuple[WorkerJobV1, EvidenceRecordV1]:
    case_id, evidence_id, job_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    kind = classify_media(content_type, filename)
    source_type = SourceType.VIDEO if is_video(kind) else SourceType.IMAGE
    job = WorkerJobV1(
        job_id=job_id,
        case_id=case_id,
        evidence_id=evidence_id,
        source_type=source_type,
        processor_name=PROCESSOR_NAME_DETECTION,
        processor_version=PROCESSOR_VERSION,
        attempt=1,
        idempotency_key=f"{case_id}:{evidence_id}:{PROCESSOR_NAME_DETECTION}:{PROCESSOR_VERSION}",
        input_object_uri="benchmark://local-file",
        requested_at=now,
    )
    evidence = EvidenceRecordV1(
        evidence_id=evidence_id,
        case_id=case_id,
        source_type=source_type,
        original_filename=filename,
        content_type=content_type,
        object_uri="benchmark://local-file",
        sha256="0" * 64,  # unused by process_job; see worker._shim_evidence_record
        classification=EvidenceClassification.UNCLASSIFIED,
        uploaded_by="benchmark-cli",
        uploaded_at=now,
        parser_profile=None,
        processing_status=EvidenceProcessingStatus.QUEUED,
    )
    return job, evidence


def run_benchmark(path: Path, *, content_type: str | None = None) -> dict[str, Any]:
    """Run the real processing pipeline once against `path` and return a measured report."""
    data = path.read_bytes()
    resolved_content_type = content_type or _guess_content_type(path)
    job, evidence = _build_job_and_evidence(content_type=resolved_content_type, filename=path.name)

    settings = get_settings()
    components = _build_analysis_components(settings)

    sw = Stopwatch()
    result = process_job(
        job,
        evidence,
        StaticBytesResolver(payload=data),
        detector=components.detector,
        tracker=components.tracker,
        ocr_adapter=components.ocr_adapter,
        stopwatch=sw,
    )
    performance = build_performance_report(sw.timings)

    observation_counts: dict[str, int] = {}
    for observation in result.observations:
        observation_counts[observation.observation_type] = (
            observation_counts.get(observation.observation_type, 0) + 1
        )

    metadata_attributes: dict[str, Any] = {}
    for observation in result.observations:
        if observation.observation_type == OBSERVATION_MEDIA_METADATA:
            metadata_attributes = dict(observation.attributes)
            break

    media_duration_ms = metadata_attributes.get("duration_ms")
    total_seconds = performance.timings.total_ms / 1000.0
    throughput: dict[str, float | None] = {
        "input_mib_per_second": (
            (len(data) / (1024 * 1024)) / total_seconds if total_seconds > 0 else None
        ),
        "media_seconds_processed_per_wall_clock_second": (
            (media_duration_ms / 1000.0) / total_seconds
            if isinstance(media_duration_ms, int | float) and total_seconds > 0
            else None
        ),
    }

    return {
        "input": {
            "path": str(path),
            "size_bytes": len(data),
            "content_type": resolved_content_type,
            "media_duration_ms": media_duration_ms,
            "media_width": metadata_attributes.get("media_width"),
            "media_height": metadata_attributes.get("media_height"),
        },
        "device": {
            "detector_loaded": components.detector is not None,
            "detector_device": (
                getattr(components.detector, "device", None) if components.detector else None
            ),
            "ocr_loaded": components.ocr_adapter is not None,
            "cpu_only": performance.capability.cpu_only,
            "gpu_visible": performance.capability.gpu_visible,
            "gpu_name": performance.capability.gpu_name,
        },
        "sampling": {
            "frames_requested": performance.timings.frames_requested,
            "frames_extracted": performance.timings.frames_extracted,
            "frames_failed": performance.timings.frames_failed,
        },
        "timings_ms": asdict(performance.timings),
        "throughput": throughput,
        "result_status": result.status.value,
        "observation_counts": observation_counts,
        "total_observations": len(result.observations),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.media_processing.benchmark",
        description=(
            "Run the real media-processing pipeline once against a local image/video file "
            "and report measured timings, device, and observation counts. Never fabricates "
            "or extrapolates a throughput figure -- only what this run actually measured."
        ),
    )
    parser.add_argument("path", type=Path, help="Path to a local image or video file.")
    parser.add_argument(
        "--content-type",
        default=None,
        help="Override the content type instead of guessing it from the file extension.",
    )
    parser.add_argument(
        "--json-output", type=Path, default=None, help="Also write the full report as JSON here."
    )
    args = parser.parse_args(argv)

    if not args.path.is_file():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 1

    try:
        report = run_benchmark(args.path, content_type=args.content_type)
    except ValueError as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, default=str))
    if args.json_output is not None:
        args.json_output.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nwrote {args.json_output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run_benchmark", "main"]
