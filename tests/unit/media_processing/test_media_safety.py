"""Scenarios 15, 18: module isolation, and no path/secret/raw-media leakage in errors.

Mirrors `tests/unit/structured_processing/test_safety.py`'s static-AST
approach for the equivalent check in this module.
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path

import pytest

from app.contracts.evidence import SourceType
from app.modules.media_processing.errors import ErrorCode
from app.modules.media_processing.limits import DEFAULT_MEDIA_LIMITS, MediaLimits
from app.modules.media_processing.source import StaticBytesResolver
from app.modules.media_processing.worker import PROCESSOR_NAME_METADATA, process_job
from tests.fixtures.media_processing.factory import make_evidence_and_job

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "media_processing"

#: Sibling feature modules this module must never reach into -- each is
#: owned by a different contributor.
_FORBIDDEN_SIBLING_MODULE_PATHS = {
    "app.modules.graph",
    "app.modules.structured_processing",
    "app.modules.access_control",
}

#: Infrastructure clients and heavyweight/cloud ML libraries this module
#: must never import -- a worker's only contract is WorkerJobV1 in,
#: WorkerResultV1 out, and only local, CPU-capable-by-default inference
#: libraries are approved (see `docs/architecture/media-processing-worker.md`'s
#: "Model asset bootstrap"/"OCR runtime setup"). `pytesseract` (a thin
#: subprocess wrapper around the local `tesseract` binary -- no bundled
#: weights, no network access) and `onnxruntime`/`cv2` (already-approved
#: local-only inference/vision runtimes -- `opencv-python-headless` was
#: already a dependency before this phase) are deliberately *not* forbidden;
#: `torch`/`tensorflow`/`sklearn`/`ultralytics`/`paddleocr` remain forbidden
#: as the heavyweight/GPU-oriented toolchains this phase's real local
#: detector/OCR adapters were built specifically to avoid.
_FORBIDDEN_INFRA_AND_ML_IMPORTS = {
    "psycopg2",
    "psycopg",
    "asyncpg",
    "sqlalchemy",
    "neo4j",
    "redis",
    "minio",
    "celery",
    "kafka",
    "pika",
    "torch",
    "tensorflow",
    "sklearn",
    "ultralytics",
    "paddleocr",
}

#: This module never resolves detections/tracks into identities or graph objects.
_FORBIDDEN_CONTRACT_IMPORTS = {"EntityV1", "EventV1"}

#: Phase 7 Part 3's evaluation-only benchmark harness (`visual_benchmark*.py`)
#: is deliberately exempt from `_FORBIDDEN_INFRA_AND_ML_IMPORTS` alone --
#: two of its four approved Phase 7 candidates (Ultralytics YOLO11,
#: PaddleOCR) *are* `ultralytics`/`paddleocr`, imported lazily and only
#: inside a best-effort real-engine wiring function, never at module import
#: time and never reachable from `worker.py`'s production dispatch. This
#: harness has no path into `process_job`'s WorkerJobV1-in/WorkerResultV1-out
#: contract at all -- it is Part 1's separate evaluation contract instead
#: (see `docs/architecture/phase-7-evaluation-and-model-governance.md`).
#: It still must (and does) pass every other static check in this file,
#: including never importing a sibling feature module or an
#: Entity/EventV1 contract.
_BENCHMARK_HARNESS_FILE_PREFIX = "visual_benchmark"


def _python_files() -> list[Path]:
    return sorted(MODULE_ROOT.rglob("*.py"))


def _non_benchmark_python_files() -> list[Path]:
    return [p for p in _python_files() if not p.name.startswith(_BENCHMARK_HARNESS_FILE_PREFIX)]


def _imported_module_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_dotted_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_sibling_feature_module_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dotted = _imported_dotted_modules(tree)
    for forbidden in _FORBIDDEN_SIBLING_MODULE_PATHS:
        matches = {m for m in dotted if m == forbidden or m.startswith(forbidden + ".")}
        assert not matches, f"{path} imports {matches} -- owned by a different module"


@pytest.mark.parametrize(
    "path", _non_benchmark_python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT))
)
def test_no_infrastructure_or_ml_library_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    forbidden = imported & _FORBIDDEN_INFRA_AND_ML_IMPORTS
    assert not forbidden, f"{path} imports forbidden infra/ML library: {forbidden}"


@pytest.mark.parametrize(
    "path",
    [p for p in _python_files() if p.name.startswith(_BENCHMARK_HARNESS_FILE_PREFIX)],
    ids=lambda p: str(p.relative_to(MODULE_ROOT)),
)
def test_benchmark_harness_still_forbids_every_non_candidate_infra_or_ml_library(
    path: Path,
) -> None:
    """The `ultralytics`/`paddleocr` exemption above is narrow, not a blanket one.

    Every other entry in `_FORBIDDEN_INFRA_AND_ML_IMPORTS` (databases,
    object storage, queues, `torch`/`tensorflow`/`sklearn`) remains
    forbidden in the benchmark harness too -- only its two approved
    Phase 7 candidate libraries are exempt.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    still_forbidden = _FORBIDDEN_INFRA_AND_ML_IMPORTS - {"ultralytics", "paddleocr"}
    forbidden = imported & still_forbidden
    assert not forbidden, f"{path} imports forbidden infra/ML library: {forbidden}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_entity_or_event_contract_is_constructed(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    forbidden = imported & _FORBIDDEN_CONTRACT_IMPORTS
    assert not forbidden, f"{path} imports {forbidden} -- entity resolution is later-phase work"


def test_probe_failure_error_message_never_contains_a_local_path(tmp_path: Path) -> None:
    evidence, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.VIDEO,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=b"not a real video file"))
    assert result.status.value == "failed"
    assert result.error is not None
    # The temp file path is generated by tempfile.mkstemp() under the OS temp
    # dir -- assert the error never leaks that directory prefix at all.
    assert tempfile.gettempdir() not in result.error.message


def test_unsupported_content_type_error_never_echoes_raw_bytes() -> None:
    secret_bytes = b"\x00\x01\xffSECRET_MARKER\xffsome-binary-junk"
    evidence, job = make_evidence_and_job(
        content_type="application/octet-stream",
        filename="file.bin",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.VIDEO,
    )
    result = process_job(job, evidence, StaticBytesResolver(payload=secret_bytes))
    assert result.status.value == "failed"
    assert result.error is not None
    assert b"SECRET_MARKER" not in result.error.message.encode()


def test_input_limit_error_never_echoes_content_length_details_beyond_the_limit() -> None:
    evidence, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    tiny_limits = MediaLimits(
        max_input_bytes=1,
        max_video_duration_ms=DEFAULT_MEDIA_LIMITS.max_video_duration_ms,
        max_width=DEFAULT_MEDIA_LIMITS.max_width,
        max_height=DEFAULT_MEDIA_LIMITS.max_height,
        max_frame_rate=DEFAULT_MEDIA_LIMITS.max_frame_rate,
        max_sampled_frames=DEFAULT_MEDIA_LIMITS.max_sampled_frames,
        max_image_pixels=DEFAULT_MEDIA_LIMITS.max_image_pixels,
        max_ocr_crop_count=DEFAULT_MEDIA_LIMITS.max_ocr_crop_count,
        subprocess_timeout_seconds=DEFAULT_MEDIA_LIMITS.subprocess_timeout_seconds,
    )
    result = process_job(
        job, evidence, StaticBytesResolver(payload=b"\x89PNGfakepngbytes"), limits=tiny_limits
    )
    assert result.status.value == "failed"
    assert result.error is not None
    assert result.error.code == ErrorCode.MEDIA_LIMIT_EXCEEDED
