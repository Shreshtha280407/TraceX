"""Phase 7 Part 2 safe-configuration and dataset/candidate validation.

Every local filesystem root a benchmark run touches comes from an explicit
environment variable or CLI argument -- never a hardcoded developer path.
Only the dataset/candidate IDs this task (Jasraj's Phase 7 Part 2 scope)
actually owns are accepted, even though `configs/benchmarks/*.v1.json`
catalogues many more (Gaurav's/Sarthak's/Shreshtha's datasets and
candidates exist in the same frozen manifest but are out of scope here).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.canonical import canonical_sha256
from app.modules.evaluation.models import (
    CandidateTask,
    DatasetManifestEntryV1,
    DatasetManifestV1,
    ModelCandidateCatalogV1,
    ModelCandidateV1,
)
from app.modules.evaluation.validation import UnsafeContentError

#: Explicit, named local-configuration environment variables. Never read
#: any other environment variable for a filesystem root, and never fall
#: back to a hardcoded path -- see this module's own docstring and the
#: task's "local roots only through explicit environment/configuration
#: values" requirement.
ENV_BENCHMARK_DATA_ROOT = "TRACEX_BENCHMARK_DATA_ROOT"
ENV_MODEL_CACHE_ROOT = "TRACEX_MODEL_CACHE_ROOT"
ENV_BENCHMARK_OUTPUT_ROOT = "TRACEX_BENCHMARK_OUTPUT_ROOT"

#: The exact (dataset_id, candidate_id) pairs this Part 2 task benchmarks.
#: `configs/benchmarks/*.v1.json` catalogues many more entries (owned by
#: Gaurav/Sarthak/Shreshtha's later Phase 7 parts) -- this benchmark CLI
#: must reject every one of them, not merely the ones it happens not to
#: implement an adapter for.
ALLOWED_DATASET_IDS: frozenset[str] = frozenset(
    {"fir_icdar_2023", "gomask_voice_cdr", "ibm_amlsim"}
)
ALLOWED_CANDIDATE_IDS: frozenset[str] = frozenset(
    {"paddleocr-ppocrv5-mobile", "paddleocr-ppocrv5-server", "existing-deterministic-parsers"}
)
#: The only valid dataset+candidate combinations for this task's three
#: benchmark adapters (OCR, CDR, finance) -- see `benchmark_adapters.py`.
ALLOWED_DATASET_CANDIDATE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("fir_icdar_2023", "paddleocr-ppocrv5-mobile"),
        ("fir_icdar_2023", "paddleocr-ppocrv5-server"),
        ("gomask_voice_cdr", "existing-deterministic-parsers"),
        ("ibm_amlsim", "existing-deterministic-parsers"),
    }
)


class BenchmarkConfigError(ValueError):
    """A required local root/environment value is missing or unsafe."""


class UnknownDatasetError(ValueError):
    """`dataset_id` is not one of this task's approved Part 2 datasets."""


class UnknownCandidateError(ValueError):
    """`candidate_id` is not one of this task's approved Part 2 candidates."""


class UnsupportedCombinationError(ValueError):
    """`dataset_id`/`candidate_id` are each individually valid but never paired."""


def validate_dataset_id(manifest: DatasetManifestV1, dataset_id: str) -> DatasetManifestEntryV1:
    """Return the manifest entry for an approved Part 2 dataset, or raise safely.

    Rejects both a dataset unknown to the frozen manifest *and* a dataset
    that exists in the manifest but belongs to a different owner's Phase 7
    part -- see `ALLOWED_DATASET_IDS`.
    """
    if dataset_id not in ALLOWED_DATASET_IDS:
        raise UnknownDatasetError(
            f"dataset_id '{dataset_id}' is not an approved Phase 7 Part 2 dataset "
            f"(approved: {sorted(ALLOWED_DATASET_IDS)})"
        )
    entry = manifest.get(dataset_id)
    if entry is None:  # pragma: no cover - manifest/allow-list drift, not a normal input error
        raise UnknownDatasetError(
            f"dataset_id '{dataset_id}' is approved but missing from the frozen manifest"
        )
    return entry


def validate_candidate_id(catalog: ModelCandidateCatalogV1, candidate_id: str) -> ModelCandidateV1:
    """Return the catalog entry for an approved Part 2 candidate, or raise safely."""
    if candidate_id not in ALLOWED_CANDIDATE_IDS:
        raise UnknownCandidateError(
            f"candidate_id '{candidate_id}' is not an approved Phase 7 Part 2 candidate "
            f"(approved: {sorted(ALLOWED_CANDIDATE_IDS)})"
        )
    entry = next((c for c in catalog.candidates if c.candidate_id == candidate_id), None)
    if entry is None:  # pragma: no cover - catalog/allow-list drift, not a normal input error
        raise UnknownCandidateError(
            f"candidate_id '{candidate_id}' is approved but missing from the frozen catalog"
        )
    return entry


def validate_dataset_candidate_pair(
    dataset: DatasetManifestEntryV1, candidate: ModelCandidateV1
) -> None:
    """Reject a structurally valid dataset and candidate that are never benchmarked together."""
    pair = (dataset.dataset_id, candidate.candidate_id)
    if pair not in ALLOWED_DATASET_CANDIDATE_PAIRS:
        raise UnsupportedCombinationError(
            f"dataset '{dataset.dataset_id}' is never benchmarked against candidate "
            f"'{candidate.candidate_id}' in Phase 7 Part 2"
        )


def expected_task_for_dataset(dataset_id: str) -> CandidateTask:
    """The one `CandidateTask` this task's adapter benchmarks for `dataset_id`."""
    if dataset_id == "fir_icdar_2023":
        return CandidateTask.OCR
    return CandidateTask.FIR_CDR_FINANCE_EXTRACTION


def _read_required_root(env_var: str) -> Path:
    raw = os.environ.get(env_var)
    if not raw or not raw.strip():
        raise BenchmarkConfigError(
            f"{env_var} is not set. Set it to a local, git-ignored directory before "
            f"running a real benchmark -- see docs/runbooks/local-development.md's "
            f"'Phase 7 Part 2 benchmark CLI' section. Never a hardcoded path."
        )
    return Path(raw.strip()).expanduser()


def resolve_benchmark_data_root(explicit: Path | None = None) -> Path:
    """The root directory local dataset artifacts live under.

    `explicit` (a CLI `--data-root` argument) always wins over the
    environment variable, mirroring `resolve_model_cache_root`/
    `resolve_benchmark_output_root` below.
    """
    return explicit if explicit is not None else _read_required_root(ENV_BENCHMARK_DATA_ROOT)


def resolve_model_cache_root(explicit: Path | None = None) -> Path | None:
    """The root directory local model weights/caches live under.

    `None` is a valid, safe outcome for the deterministic-rules candidate
    (`existing-deterministic-parsers`), which needs no model cache at all --
    callers must not treat a missing model-cache root as an error until
    they know the candidate actually requires one.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_MODEL_CACHE_ROOT)
    return Path(raw.strip()).expanduser() if raw and raw.strip() else None


def resolve_benchmark_output_root(explicit: Path | None = None) -> Path:
    """The root directory safe aggregate JSON result files are written under."""
    return explicit if explicit is not None else _read_required_root(ENV_BENCHMARK_OUTPUT_ROOT)


def benchmark_inference_config_hash(config: dict[str, Any]) -> str:
    """A stable hash identifying exactly which inference configuration produced a run.

    Any change to `config`'s content changes this hash -- required so two
    `BenchmarkRunV1`s can be told apart even when every other field
    matches (proof point 13: exact configuration affects the hash).
    """
    return canonical_sha256(config)


#: A local filesystem path shaped like a real user/home directory --
#: rejected from any field a committed `BenchmarkRunV1` JSON result exposes.
#: Deliberately broader than `evaluation.validation.check_safe_relative_path`
#: (which validates the frozen manifest's own `local_path_placeholder`):
#: this catches an *absolute* path leaking into a free-text run field, not
#: just a malformed relative one.
_ABSOLUTE_OR_HOME_PATH = re.compile(r"(^|[\s\"'=:])(/home/|/Users/|~[/\\]|[A-Za-z]:\\\\)")


def reject_private_local_paths(value: Any) -> None:
    """Recursively reject a value that looks like an absolute local filesystem path.

    Applied to every free-text field of a `BenchmarkRunV1` before it is
    written to a committed-safe result file -- defense in depth beyond
    `BenchmarkRunV1.metrics`'s own `float | int | None` type constraint,
    which already makes a raw path structurally impossible in a *metric*
    value.
    """
    if isinstance(value, dict):
        for nested in value.values():
            reject_private_local_paths(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            reject_private_local_paths(nested)
    elif isinstance(value, str) and _ABSOLUTE_OR_HOME_PATH.search(value):
        raise UnsafeContentError("value contains an absolute or home-relative local path")


__all__ = [
    "ALLOWED_CANDIDATE_IDS",
    "ALLOWED_DATASET_CANDIDATE_PAIRS",
    "ALLOWED_DATASET_IDS",
    "ENV_BENCHMARK_DATA_ROOT",
    "ENV_BENCHMARK_OUTPUT_ROOT",
    "ENV_MODEL_CACHE_ROOT",
    "BenchmarkConfigError",
    "UnknownCandidateError",
    "UnknownDatasetError",
    "UnsupportedCombinationError",
    "benchmark_inference_config_hash",
    "expected_task_for_dataset",
    "reject_private_local_paths",
    "resolve_benchmark_data_root",
    "resolve_benchmark_output_root",
    "resolve_model_cache_root",
]
