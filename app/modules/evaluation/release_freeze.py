"""Gate C release-evidence freeze and fail-closed configuration controls.

The committed JSON is safe aggregate metadata only.  This loader binds it
back to the frozen Gate B dataset/candidate catalogues and their exact file
hashes before any release component can be resolved.  It never loads a model
artifact, accepts a local path, or downloads anything.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import get_request_id
from app.modules.communication_processing.audio_social_benchmark_validation import (
    ALLOWED_DATASET_CANDIDATE_PAIRS as AUDIO_SOCIAL_PAIRS,
)
from app.modules.evaluation.catalog import (
    MODEL_CANDIDATE_CATALOG_PATH,
    load_model_candidate_catalog,
)
from app.modules.evaluation.manifest import DATASET_MANIFEST_PATH, load_dataset_manifest
from app.modules.evaluation.models import (
    BenchmarkRunStatus,
    CandidateTask,
    DatasetEvaluationStatus,
    ExecutionTarget,
    LicenseStatus,
    SplitId,
)
from app.modules.evaluation.results import (
    BENCHMARK_METRICS_SPEC_PATH,
    load_benchmark_metrics_spec,
)
from app.modules.evaluation.validation import check_forbidden_tokens
from app.modules.media_processing.visual_benchmark_validation import (
    ALLOWED_DATASET_CANDIDATE_PAIRS as VISUAL_PAIRS,
)
from app.modules.structured_processing.benchmark_validation import (
    ALLOWED_DATASET_CANDIDATE_PAIRS as STRUCTURED_PAIRS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_FREEZE_PATH = REPO_ROOT / "configs" / "benchmarks" / "release-freeze.v1.json"
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_ALLOWED_EVIDENCE_PAIRS = STRUCTURED_PAIRS | VISUAL_PAIRS | AUDIO_SOCIAL_PAIRS

logger = structlog.get_logger(__name__)


class ReleaseFreezeError(ValueError):
    """A safe, non-sensitive release-freeze validation failure."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GovernanceStatus(StrEnum):
    APPROVED_EVIDENCE = "approved_evidence"
    APPROVED_BASELINE = "approved_baseline"
    EVIDENCE_ONLY = "evidence_only"
    UNAVAILABLE = "unavailable"
    DEFERRED = "deferred"


class DatasetGovernanceV1(FrozenModel):
    dataset_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    status: GovernanceStatus
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("reason")
    @classmethod
    def _safe_reason(cls, value: str) -> str:
        check_forbidden_tokens(value)
        return value


class CandidateGovernanceV1(FrozenModel):
    candidate_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,80}$")
    status: GovernanceStatus
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("reason")
    @classmethod
    def _safe_reason(cls, value: str) -> str:
        check_forbidden_tokens(value)
        return value


class FrozenEvidenceV1(FrozenModel):
    schema_version: Literal["v1"]
    disposition: GovernanceStatus
    dataset_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    candidate_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,80}$")
    dataset_version: str = Field(min_length=1, max_length=500)
    candidate_version: str = Field(min_length=1, max_length=200)
    artifact_version: str = Field(min_length=1, max_length=300)
    run_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,120}$")
    result_schema_version: Literal["v1"]
    split_id: SplitId
    task: CandidateTask
    status: BenchmarkRunStatus
    runtime_environment: str = Field(min_length=1, max_length=200)
    hardware_profile: str = Field(min_length=1, max_length=300)
    inference_config_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    dataset_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    artifact_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    failure_reason_safe: str | None = Field(default=None, max_length=500)
    host_limitations: tuple[str, ...] = Field(min_length=1)
    known_limitations: tuple[str, ...] = Field(min_length=1)

    @field_validator("failure_reason_safe", "host_limitations", "known_limitations")
    @classmethod
    def _safe_text(cls, value: object) -> object:
        check_forbidden_tokens(value)
        return value

    @model_validator(mode="after")
    def _validate_disposition(self) -> FrozenEvidenceV1:
        if self.disposition == GovernanceStatus.APPROVED_EVIDENCE:
            if self.status != BenchmarkRunStatus.SUCCEEDED:
                raise ValueError("approved evidence must have status=succeeded")
            required_hashes = (
                self.inference_config_hash,
                self.dataset_sha256,
                self.artifact_sha256,
                self.result_sha256,
            )
            if any(value is None for value in required_hashes):
                raise ValueError(
                    "approved evidence requires config, dataset, artifact, and result hashes"
                )
            if not self.metrics:
                raise ValueError("approved evidence requires measured metrics")
            if self.failure_reason_safe is not None:
                raise ValueError("approved evidence cannot carry a failure reason")
        elif self.disposition == GovernanceStatus.UNAVAILABLE:
            if self.status != BenchmarkRunStatus.UNAVAILABLE:
                raise ValueError("unavailable evidence must have status=unavailable")
            if self.artifact_sha256 is not None or self.metrics:
                raise ValueError("unavailable evidence cannot claim an artifact or metrics")
            if self.failure_reason_safe is None:
                raise ValueError("unavailable evidence requires a safe reason")
        elif self.disposition == GovernanceStatus.DEFERRED:
            if self.status == BenchmarkRunStatus.SUCCEEDED:
                if not self.metrics or self.artifact_sha256 is None:
                    raise ValueError(
                        "a deferred successful measurement requires metrics and artifact hash"
                    )
                if self.failure_reason_safe is not None:
                    raise ValueError(
                        "a deferred successful measurement cannot carry a failure reason"
                    )
            elif self.status == BenchmarkRunStatus.UNAVAILABLE:
                if self.metrics or self.artifact_sha256 is not None:
                    raise ValueError(
                        "deferred unavailable evidence cannot claim metrics or an artifact"
                    )
                if self.failure_reason_safe is None:
                    raise ValueError("deferred unavailable evidence requires a safe reason")
            else:
                raise ValueError(
                    "deferred evidence must preserve a succeeded or unavailable result"
                )
        else:
            raise ValueError("evidence disposition is not valid for a frozen result")
        return self


class ReleaseComponentV1(FrozenModel):
    purpose: Literal["relationship_scoring"]
    candidate_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,80}$")
    configuration_version: str = Field(pattern=r"^[a-z0-9_.\-]{1,120}$")
    configuration_sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_loading: Literal["none"]
    artifact_sha256: None = None
    final_relationship_ml_selection: Literal[False] = False


class ReleaseConfigurationV1(FrozenModel):
    configuration_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,120}$")
    enabled: bool
    components: tuple[ReleaseComponentV1, ...]
    safe_reason: str = Field(min_length=1, max_length=500)

    @field_validator("safe_reason")
    @classmethod
    def _safe_reason(cls, value: str) -> str:
        check_forbidden_tokens(value)
        return value

    @model_validator(mode="after")
    def _validate_enabled_shape(self) -> ReleaseConfigurationV1:
        if self.enabled and not self.components:
            raise ValueError("an enabled release configuration requires a component")
        if not self.enabled and self.components:
            raise ValueError("a disabled rollback configuration cannot load components")
        return self


class SourceHashesV1(FrozenModel):
    dataset_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    model_candidates_sha256: str = Field(pattern=_SHA256_PATTERN)
    benchmark_metrics_sha256: str = Field(pattern=_SHA256_PATTERN)


class ReleaseFreezeV1(FrozenModel):
    schema_version: Literal["v1"]
    release_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,120}$")
    evidence_cycle_id: Literal["gate-b-v1"]
    frozen_at: str
    current_evidence_frozen: Literal[True]
    future_expansion_requires_versioned_cycle: Literal[True]
    final_relationship_ml_selected: Literal[False]
    source_hashes: SourceHashesV1
    datasets: tuple[DatasetGovernanceV1, ...] = Field(min_length=1)
    candidates: tuple[CandidateGovernanceV1, ...] = Field(min_length=1)
    evidence: tuple[FrozenEvidenceV1, ...] = Field(min_length=1)
    configurations: tuple[ReleaseConfigurationV1, ...] = Field(min_length=2)
    active_configuration_id: str
    rollback_configuration_id: str

    @model_validator(mode="after")
    def _validate_ids(self) -> ReleaseFreezeV1:
        dataset_ids = [item.dataset_id for item in self.datasets]
        candidate_ids = [item.candidate_id for item in self.candidates]
        configuration_ids = [item.configuration_id for item in self.configurations]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("duplicate dataset governance entry")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("duplicate candidate governance entry")
        if len(configuration_ids) != len(set(configuration_ids)):
            raise ValueError("duplicate release configuration")
        configurations = {item.configuration_id: item for item in self.configurations}
        active = configurations.get(self.active_configuration_id)
        rollback = configurations.get(self.rollback_configuration_id)
        if active is None or not active.enabled:
            raise ValueError("active configuration must name an enabled frozen configuration")
        if rollback is None or rollback.enabled:
            raise ValueError("rollback configuration must name a disabled frozen configuration")
        return self


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_against_frozen_sources(freeze: ReleaseFreezeV1) -> None:
    manifest = load_dataset_manifest()
    catalog = load_model_candidate_catalog()
    metrics_spec = load_benchmark_metrics_spec()

    actual_hashes = {
        "dataset_manifest_sha256": _sha256(DATASET_MANIFEST_PATH),
        "model_candidates_sha256": _sha256(MODEL_CANDIDATE_CATALOG_PATH),
        "benchmark_metrics_sha256": _sha256(BENCHMARK_METRICS_SPEC_PATH),
    }
    if freeze.source_hashes.model_dump() != actual_hashes:
        raise ReleaseFreezeError("release freeze source hashes do not match the frozen catalogues")

    manifest_by_id = {item.dataset_id: item for item in manifest.datasets}
    catalog_by_id = {item.candidate_id: item for item in catalog.candidates}
    if {item.dataset_id for item in freeze.datasets} != set(manifest_by_id):
        raise ReleaseFreezeError("release freeze must classify every dataset exactly once")
    if {item.candidate_id for item in freeze.candidates} != set(catalog_by_id):
        raise ReleaseFreezeError("release freeze must classify every candidate exactly once")

    dataset_governance = {item.dataset_id: item.status for item in freeze.datasets}
    candidate_governance = {item.candidate_id: item.status for item in freeze.candidates}
    for item in freeze.evidence:
        dataset = manifest_by_id.get(item.dataset_id)
        candidate = catalog_by_id.get(item.candidate_id)
        if dataset is None or candidate is None:
            raise ReleaseFreezeError("release evidence names an unknown dataset or candidate")
        if (item.dataset_id, item.candidate_id) not in _ALLOWED_EVIDENCE_PAIRS:
            raise ReleaseFreezeError("release evidence names an unapproved dataset/candidate pair")
        if item.dataset_version != dataset.release_or_version:
            raise ReleaseFreezeError("release evidence dataset version does not match the manifest")
        if item.candidate_version != candidate.variant or item.task != candidate.task:
            raise ReleaseFreezeError(
                "release evidence candidate metadata does not match the catalogue"
            )
        if item.disposition == GovernanceStatus.APPROVED_EVIDENCE:
            if dataset.evaluation_status != DatasetEvaluationStatus.COMPLETE:
                raise ReleaseFreezeError("approved evidence requires a complete dataset evaluation")
            if dataset_governance[item.dataset_id] != GovernanceStatus.APPROVED_EVIDENCE:
                raise ReleaseFreezeError("approved evidence conflicts with dataset governance")
            group = metrics_spec.for_task(item.task)
            if group is None or any(
                name not in item.metrics for name in group.required_metric_names
            ):
                raise ReleaseFreezeError("approved evidence is missing required task metrics")

    for configuration in freeze.configurations:
        for component in configuration.components:
            candidate = catalog_by_id.get(component.candidate_id)
            if candidate is None:
                raise ReleaseFreezeError("release configuration names an unknown candidate")
            if candidate_governance[component.candidate_id] != GovernanceStatus.APPROVED_BASELINE:
                raise ReleaseFreezeError("release configuration selects an unapproved candidate")
            if (
                candidate.task != CandidateTask.CORRELATION
                or candidate.execution_target != ExecutionTarget.DETERMINISTIC_RULES
                or candidate.license_status != LicenseStatus.INTERNAL_ONLY
            ):
                raise ReleaseFreezeError(
                    "release component must be the internal deterministic baseline"
                )
            if component.configuration_version != candidate.variant:
                raise ReleaseFreezeError("release component version does not match the catalogue")


@lru_cache(maxsize=1)
def load_release_freeze(path: Path = RELEASE_FREEZE_PATH) -> ReleaseFreezeV1:
    """Load and fully validate the committed freeze.  No alternate path is used at runtime."""
    data = json.loads(path.read_text(encoding="utf-8"))
    freeze = ReleaseFreezeV1.model_validate(data)
    _validate_against_frozen_sources(freeze)
    return freeze


def get_release_configuration(
    freeze: ReleaseFreezeV1,
    configuration_id: str,
    *,
    allow_disabled: bool = False,
) -> ReleaseConfigurationV1:
    configuration = next(
        (item for item in freeze.configurations if item.configuration_id == configuration_id), None
    )
    if configuration is None:
        raise ReleaseFreezeError("release configuration is not approved")
    if not configuration.enabled and not allow_disabled:
        raise ReleaseFreezeError("release configuration is disabled")
    return configuration


def require_release_component(
    *,
    configuration_id: str,
    purpose: Literal["relationship_scoring"],
    candidate_id: str,
    configuration_sha256: str,
    disabled: bool = False,
) -> ReleaseComponentV1:
    """Resolve one exact frozen component or fail before data/model work begins."""
    if disabled:
        logger.warning(
            "evaluation.release_configuration_disabled",
            request_id=get_request_id() or None,
            configuration_id=configuration_id,
        )
        raise ReleaseFreezeError("release configuration is disabled")
    configuration = get_release_configuration(load_release_freeze(), configuration_id)
    component = next(
        (
            item
            for item in configuration.components
            if item.purpose == purpose and item.candidate_id == candidate_id
        ),
        None,
    )
    if component is None or component.configuration_sha256 != configuration_sha256:
        raise ReleaseFreezeError("release component is not approved or its configuration changed")
    return component


__all__ = [
    "RELEASE_FREEZE_PATH",
    "CandidateGovernanceV1",
    "DatasetGovernanceV1",
    "FrozenEvidenceV1",
    "GovernanceStatus",
    "ReleaseConfigurationV1",
    "ReleaseFreezeError",
    "ReleaseFreezeV1",
    "get_release_configuration",
    "load_release_freeze",
    "require_release_component",
]
