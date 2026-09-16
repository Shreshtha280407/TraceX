"""Phase 7 Part 4 audio/social-benchmark request validation and local-root resolution.

Mirrors the safety pattern Phase 7 Parts 2/3 established: local roots come
only from explicit environment variables or CLI flags (never a hardcoded
path), a narrow allow-list restricts this harness to exactly Sarthak's own
Phase 7 Part 1 manifest entries (`common_voice_indic`, `ami_meeting_corpus`,
`vast_social_text`, `vast_2014_mixed_records` / `silero-vad-v6`,
`faster-whisper-small`, `faster-whisper-medium`, `pyannote-community-local`,
`deterministic-diarization-fallback`, `fasttext-lid176`,
`existing-deterministic-social-parsers`), and a completed real benchmark
cannot run while either the dataset's or the candidate's own
`license_status` is unresolved, or the candidate's own `selection_status`
is still `conditional`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.canonical import canonical_sha256
from app.modules.evaluation.models import (
    CandidateSelectionStatus,
    DatasetManifestEntryV1,
    DatasetManifestV1,
    LicenseStatus,
    ModelCandidateCatalogV1,
    ModelCandidateV1,
)

ENV_BENCHMARK_DATA_ROOT = "TRACEX_BENCHMARK_DATA_ROOT"
ENV_MODEL_CACHE_ROOT = "TRACEX_MODEL_CACHE_ROOT"
ENV_BENCHMARK_OUTPUT_ROOT = "TRACEX_BENCHMARK_OUTPUT_ROOT"

#: Exactly Sarthak's four Phase 7 Part 1 manifest entries -- every other
#: dataset in the shared catalogue is structurally valid but out of this
#: harness's scope and rejected.
ALLOWED_DATASET_IDS = frozenset(
    {"common_voice_indic", "ami_meeting_corpus", "vast_social_text", "vast_2014_mixed_records"}
)

#: Sarthak's six catalogued candidates, plus this task's own additive
#: `existing-deterministic-social-parsers` entry (added to
#: `model-candidates.v1.json` in this same task -- see
#: `docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part 4"
#: section for why: Part 1 deliberately left `social_text_extraction`
#: forward-compatible-only with no candidate, anticipating exactly this).
ALLOWED_CANDIDATE_IDS = frozenset(
    {
        "silero-vad-v6",
        "faster-whisper-small",
        "faster-whisper-medium",
        "pyannote-community-local",
        "deterministic-diarization-fallback",
        "fasttext-lid176",
        "existing-deterministic-social-parsers",
    }
)

#: Every dataset/candidate combination this harness actually supports,
#: matching the task brief's own dataset-role table: `common_voice_indic`
#: supports ASR (both faster-whisper sizes) and language identification;
#: `ami_meeting_corpus` supports VAD and diarization (both the conditional
#: pyannote candidate and the always-available deterministic fallback);
#: `vast_social_text`/`vast_2014_mixed_records` (primary and conditional
#: support, respectively) both support social/chat structured extraction
#: via the one deterministic baseline candidate.
ALLOWED_DATASET_CANDIDATE_PAIRS = frozenset(
    {
        ("common_voice_indic", "faster-whisper-small"),
        ("common_voice_indic", "faster-whisper-medium"),
        ("common_voice_indic", "fasttext-lid176"),
        ("ami_meeting_corpus", "silero-vad-v6"),
        ("ami_meeting_corpus", "pyannote-community-local"),
        ("ami_meeting_corpus", "deterministic-diarization-fallback"),
        ("vast_social_text", "existing-deterministic-social-parsers"),
        ("vast_2014_mixed_records", "existing-deterministic-social-parsers"),
    }
)

#: A dataset/candidate whose `license_status` is anything else cannot
#: produce a `SUCCEEDED` real benchmark result -- only a truthful
#: `UNAVAILABLE` one naming the license gate.
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
    """The dataset's/candidate's `license_status`, or the candidate's own
    `conditional` `selection_status`, blocks a real benchmark run.

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


def require_cleared_for_real_execution(
    dataset: DatasetManifestEntryV1, candidate: ModelCandidateV1
) -> None:
    """Block a completed real benchmark while license or conditional status is unresolved.

    Three independent gates, any one of which blocks:
    (1) the dataset's own `license_status`, (2) the candidate's own
    `license_status`, (3) the candidate's own `selection_status ==
    conditional` (e.g. `pyannote-community-local`'s local-use terms are not
    yet accepted, `fasttext-lid176`'s deterministic-normalization-first
    policy is not yet proven insufficient). Raised, never silently
    downgraded -- the caller (`audio_social_benchmark.py`) catches this and
    converts it into a safe `UNAVAILABLE` result naming the gate.
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
    if candidate.selection_status == CandidateSelectionStatus.CONDITIONAL:
        raise LicenseNotClearedError(
            f"candidate '{candidate.candidate_id}' remains 'conditional' -- its "
            "local-use/adoption terms are not yet accepted for a real benchmark run"
        )


def _read_required_root(env_var: str, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    raw = os.environ.get(env_var)
    if not raw or not raw.strip():
        raise BenchmarkConfigError(
            f"{env_var} is not set -- pass it explicitly or export it before running "
            "the audio/social benchmark CLI"
        )
    return Path(raw).expanduser()


def resolve_benchmark_data_root(explicit: Path | None = None) -> Path:
    return _read_required_root(ENV_BENCHMARK_DATA_ROOT, explicit)


def resolve_benchmark_output_root(explicit: Path | None = None) -> Path:
    return _read_required_root(ENV_BENCHMARK_OUTPUT_ROOT, explicit)


def resolve_model_cache_root(explicit: Path | None = None) -> Path | None:
    """Unlike the data/output roots, a missing model cache root is not an error here.

    The deterministic social/chat baseline and the deterministic
    diarization fallback genuinely have no model cache to resolve;
    `audio_social_benchmark.py` decides per-candidate whether `None` here
    means "unavailable."
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_MODEL_CACHE_ROOT)
    return Path(raw).expanduser() if raw and raw.strip() else None


_ABSOLUTE_OR_HOME_PATH = re.compile(r"(^|[\s\"'=:])(/home/|/Users/|~/|[A-Za-z]:\\\\)")


def reject_private_local_paths(value: Any) -> None:
    """Recursively reject anything shaped like a private local filesystem path.

    Applied to a fully-built `BenchmarkRunV1` (via `model_dump(mode="json")`)
    before it is written to disk -- the same defence-in-depth check every
    other Phase 7 benchmark harness uses, reused here as an independent
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
    "require_cleared_for_real_execution",
    "resolve_benchmark_data_root",
    "resolve_benchmark_output_root",
    "resolve_model_cache_root",
    "validate_candidate_id",
    "validate_dataset_candidate_pair",
    "validate_dataset_id",
]
