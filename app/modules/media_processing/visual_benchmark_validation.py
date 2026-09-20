"""Phase 7 Part 3 visual-benchmark request validation and local-root resolution.

Mirrors the safety pattern an equivalent Phase 7 benchmark harness for
another modality would use: local roots come only from explicit
environment variables or CLI flags (never a hardcoded path), a narrow
allow-list restricts this harness to exactly Gaurav's own Phase 7 Part 1
manifest entries (`virat_ground`, `safe_unsafe_behaviour`, `ufpr_alpr` /
`yolo11n`, `yolo11s`, `bytetrack`, `paddleocr-lightweight-visual-text`),
and a completed real benchmark cannot run at all while either the dataset's
or the candidate's own `license_status` is still `pending_verification`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.canonical import canonical_sha256
from app.modules.evaluation.models import (
    DatasetManifestEntryV1,
    DatasetManifestV1,
    LicenseStatus,
    ModelCandidateCatalogV1,
    ModelCandidateV1,
)

ENV_BENCHMARK_DATA_ROOT = "TRACEX_BENCHMARK_DATA_ROOT"
ENV_MODEL_CACHE_ROOT = "TRACEX_MODEL_CACHE_ROOT"
ENV_BENCHMARK_OUTPUT_ROOT = "TRACEX_BENCHMARK_OUTPUT_ROOT"

#: Exactly Gaurav's three Phase 7 Part 1 manifest entries -- every other
#: dataset in the shared catalogue (fir_icdar_2023, gomask_voice_cdr, ...)
#: is structurally valid but out of this harness's scope and rejected.
ALLOWED_DATASET_IDS = frozenset({"virat_ground", "safe_unsafe_behaviour", "ufpr_alpr"})

#: Exactly Gaurav's four Phase 7 Part 1 candidates.
ALLOWED_CANDIDATE_IDS = frozenset(
    {"yolo11n", "yolo11s", "bytetrack", "paddleocr-lightweight-visual-text"}
)

#: Every dataset/candidate combination this harness actually supports,
#: matching the task brief's own "Permitted benchmark use" column exactly:
#: `virat_ground` supports detection (both YOLO variants) and tracking
#: (ByteTrack); `safe_unsafe_behaviour` is detection-only additional
#: stress-testing (no tracking, no behaviour-classification candidate
#: exists in the catalogue -- inventing one is explicitly out of scope);
#: `ufpr_alpr` supports plate-region detection (both YOLO variants) and
#: plate-text OCR (the lightweight visual-text candidate), gated further
#: by `require_license_cleared_for_real_execution` below.
ALLOWED_DATASET_CANDIDATE_PAIRS = frozenset(
    {
        ("virat_ground", "yolo11n"),
        ("virat_ground", "yolo11s"),
        ("virat_ground", "bytetrack"),
        ("safe_unsafe_behaviour", "yolo11n"),
        ("safe_unsafe_behaviour", "yolo11s"),
        ("ufpr_alpr", "yolo11n"),
        ("ufpr_alpr", "yolo11s"),
        ("ufpr_alpr", "paddleocr-lightweight-visual-text"),
    }
)

#: A dataset/candidate whose `license_status` is anything else (in
#: practice, `pending_verification` for every entry in this harness's
#: scope today) cannot produce a `SUCCEEDED` real benchmark result --
#: only a truthful `UNAVAILABLE` one naming the license gate.
_CLEARED_LICENSE_STATUSES = frozenset(
    {
        LicenseStatus.VERIFIED_PERMISSIVE,
        LicenseStatus.VERIFIED_RESTRICTED_NONCOMMERCIAL,
        LicenseStatus.INTERNAL_ONLY,
    }
)


class BenchmarkConfigError(ValueError):
    """A caller/configuration mistake -- not a benchmark outcome."""


class UnknownDatasetError(BenchmarkConfigError):
    pass


class UnknownCandidateError(BenchmarkConfigError):
    pass


class UnsupportedCombinationError(BenchmarkConfigError):
    pass


class LicenseNotClearedError(ValueError):
    """The dataset's or candidate's `license_status` blocks a real benchmark run.

    Not a `BenchmarkConfigError`: this is a real, meaningful outcome of the
    request (truthfully reported as `BenchmarkRunStatus.UNAVAILABLE`), not
    a caller mistake like an unknown ID.
    """


def validate_dataset_id(manifest: DatasetManifestV1, dataset_id: str) -> DatasetManifestEntryV1:
    if dataset_id not in ALLOWED_DATASET_IDS:
        raise UnknownDatasetError(
            f"dataset_id '{dataset_id}' is rejected -- this harness only benchmarks "
            f"{sorted(ALLOWED_DATASET_IDS)}"
        )
    entry = manifest.get(dataset_id)
    if entry is None:
        raise UnknownDatasetError(f"dataset_id '{dataset_id}' is not in the frozen manifest")
    return entry


def validate_candidate_id(catalog: ModelCandidateCatalogV1, candidate_id: str) -> ModelCandidateV1:
    if candidate_id not in ALLOWED_CANDIDATE_IDS:
        raise UnknownCandidateError(
            f"candidate_id '{candidate_id}' is rejected -- this harness only benchmarks "
            f"{sorted(ALLOWED_CANDIDATE_IDS)}"
        )
    candidate = next((c for c in catalog.candidates if c.candidate_id == candidate_id), None)
    if candidate is None:
        raise UnknownCandidateError(f"candidate_id '{candidate_id}' is not in the frozen catalog")
    return candidate


def validate_dataset_candidate_pair(
    dataset: DatasetManifestEntryV1, candidate: ModelCandidateV1
) -> None:
    pair = (dataset.dataset_id, candidate.candidate_id)
    if pair not in ALLOWED_DATASET_CANDIDATE_PAIRS:
        raise UnsupportedCombinationError(
            f"'{candidate.candidate_id}' is not an approved candidate for dataset "
            f"'{dataset.dataset_id}' in this harness"
        )


def require_license_cleared_for_real_execution(
    dataset: DatasetManifestEntryV1, candidate: ModelCandidateV1
) -> None:
    """Block a completed real benchmark while either license is unresolved.

    Raised, never silently downgraded -- the caller (`visual_benchmark.py`)
    catches this and converts it into a safe `UNAVAILABLE` result naming
    the gate, exactly like a missing local artifact.
    """
    if dataset.license_status not in _CLEARED_LICENSE_STATUSES:
        raise LicenseNotClearedError(
            f"dataset '{dataset.dataset_id}' licence status is "
            f"'{dataset.license_status.value}', not yet cleared for a real benchmark run"
        )
    if candidate.license_status not in _CLEARED_LICENSE_STATUSES:
        raise LicenseNotClearedError(
            f"candidate '{candidate.candidate_id}' licence status is "
            f"'{candidate.license_status.value}', not yet cleared for a real benchmark run"
        )


def _read_required_root(env_var: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    raw = os.environ.get(env_var)
    if not raw or not raw.strip():
        raise BenchmarkConfigError(
            f"{env_var} is not set -- pass it explicitly or export it before running "
            "the visual benchmark CLI"
        )
    return Path(raw).expanduser()


def resolve_benchmark_data_root(explicit: Path | None = None) -> Path:
    return _read_required_root(ENV_BENCHMARK_DATA_ROOT, explicit)


def resolve_benchmark_output_root(explicit: Path | None = None) -> Path:
    return _read_required_root(ENV_BENCHMARK_OUTPUT_ROOT, explicit)


def resolve_model_cache_root(explicit: Path | None = None) -> Path | None:
    """Unlike the data/output roots, a missing model cache root is not an error here.

    A tracking-only run (ByteTrack, no downloaded model weight) genuinely
    has no model cache to resolve; `visual_benchmark.py` decides per-task
    whether `None` here means "unavailable."
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_MODEL_CACHE_ROOT)
    return Path(raw).expanduser() if raw and raw.strip() else None


_ABSOLUTE_OR_HOME_PATH = re.compile(r"(^|[\s\"'=:])(/home/|/Users/|~/|[A-Za-z]:\\\\)")


def reject_private_local_paths(value: Any) -> None:
    """Recursively reject anything shaped like a private local filesystem path.

    Applied to a fully-built `BenchmarkRunV1` (via `model_dump(mode="json")`)
    before it is written to disk -- the same defence-in-depth check Phase 7
    Part 2's benchmark harness uses, reused here as an independent
    implementation since this module must not import from another
    contributor's in-progress module.
    """
    if isinstance(value, dict):
        for nested in value.values():
            reject_private_local_paths(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            reject_private_local_paths(nested)
    elif isinstance(value, str) and _ABSOLUTE_OR_HOME_PATH.search(value):
        raise BenchmarkConfigError(
            "a benchmark result must never contain a private local filesystem path"
        )


def benchmark_inference_config_hash(config: dict[str, Any]) -> str:
    """A deterministic, content-sensitive hash of the exact benchmark configuration."""
    return canonical_sha256(config)


__all__ = [
    "ALLOWED_CANDIDATE_IDS",
    "ALLOWED_DATASET_CANDIDATE_PAIRS",
    "ALLOWED_DATASET_IDS",
    "ENV_BENCHMARK_DATA_ROOT",
    "ENV_BENCHMARK_OUTPUT_ROOT",
    "ENV_MODEL_CACHE_ROOT",
    "BenchmarkConfigError",
    "LicenseNotClearedError",
    "UnknownCandidateError",
    "UnknownDatasetError",
    "UnsupportedCombinationError",
    "benchmark_inference_config_hash",
    "reject_private_local_paths",
    "require_license_cleared_for_real_execution",
    "resolve_benchmark_data_root",
    "resolve_benchmark_output_root",
    "resolve_model_cache_root",
    "validate_candidate_id",
    "validate_dataset_candidate_pair",
    "validate_dataset_id",
]
