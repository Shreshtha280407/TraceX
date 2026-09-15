"""Phase 7 Part 1 typed evaluation contracts: the referee and scoreboard.

These are internal, versioned contracts -- like `app.modules.graph
.integration_models` and `app.modules.integrity.models` -- not additions to
the frozen `app.contracts` V1 payloads. They define what may be measured,
how, and how safely; they do not themselves contain a measurement, a
downloaded dataset, or a model weight.

See `docs/architecture/phase-7-evaluation-and-model-governance.md` for the
full design and `docs/decisions/ADR-016-phase-7-evaluation-and-model-selection.md`
for the reasoning behind the frozen evaluation rules encoded here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.evaluation.validation import (
    FORBIDDEN_METRIC_KEY_TOKENS,
    check_forbidden_tokens,
    check_safe_relative_path,
)

SCHEMA_VERSION: Literal["v1"] = "v1"


class EvaluationModel(BaseModel):
    """Base class for internal evaluation-module models: immutable, no stray fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Shared vocabulary -------------------------------------------------


class TeamOwner(StrEnum):
    """A named contributor, or the shared-team fallback for a plan-level artifact."""

    NIPUN = "nipun"
    JASRAJ = "jasraj"
    GAURAV = "gaurav"
    SARTHAK = "sarthak"
    ADITYA = "aditya"
    SHRESHTHA = "shreshtha"


class EvaluationModality(StrEnum):
    DOCUMENT = "document"
    CDR = "cdr"
    FINANCE = "finance"
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    SOCIAL_TEXT = "social_text"
    MULTI_MODAL = "multi_modal"
    CONTEXT_REFERENCE = "context_reference"


class CandidateTask(StrEnum):
    OCR = "ocr"
    FIR_CDR_FINANCE_EXTRACTION = "fir_cdr_finance_extraction"
    DETECTION = "detection"
    TRACKING = "tracking"
    VISUAL_TEXT = "visual_text"
    VAD = "vad"
    ASR = "asr"
    DIARIZATION = "diarization"
    LANGUAGE_IDENTIFICATION = "language_identification"
    #: Forward-compatible only, mirroring Phase 6's `review_decision`/
    #: `hypothesis_action` precedent: no Part 1 candidate exists for this
    #: task yet (the existing deterministic social/chat parsers are the
    # ; baseline), but the metrics table's own "Social/text" row needs a
    #: home for a future candidate to benchmark against.
    SOCIAL_TEXT_EXTRACTION = "social_text_extraction"
    CORRELATION = "correlation"


# --- Dataset manifest ----------------------------------------------------


class DatasetRole(StrEnum):
    PRIMARY_BENCHMARK = "primary_benchmark"
    CONDITIONAL_SUPPORT = "conditional_support"
    SYNTHETIC_MULTICASE = "synthetic_multicase"
    PRIVATE_SHOWCASE_HOLDOUT = "private_showcase_holdout"


class LicenseStatus(StrEnum):
    VERIFIED_PERMISSIVE = "verified_permissive"
    VERIFIED_RESTRICTED_NONCOMMERCIAL = "verified_restricted_noncommercial"
    PENDING_VERIFICATION = "pending_verification"
    INTERNAL_ONLY = "internal_only"


class GitStoragePolicy(StrEnum):
    #: Raw data lives outside Git under a `.gitignore`d local-data root;
    #: only this manifest entry (safe metadata) is ever committed.
    EXCLUDED_MANIFEST_ONLY = "excluded_manifest_only"
    #: Operation Nightfall only: not even a content manifest is committed,
    #: just this entry's existence/policy fields.
    EXCLUDED_PRIVATE_NEVER_DOCUMENTED = "excluded_private_never_documented"
    #: `synthetic_multicase_v1` only: small, licence-safe, non-sensitive
    #: fixtures may be committed alongside the manifest entry.
    SYNTHETIC_FIXTURES_MAY_BE_COMMITTED = "synthetic_fixtures_may_be_committed"


class RedistributionStatus(StrEnum):
    NOT_REDISTRIBUTED = "not_redistributed"
    PENDING_VERIFICATION = "pending_verification"
    NOT_APPLICABLE_INTERNAL = "not_applicable_internal"


class DatasetEvaluationStatus(StrEnum):
    PLANNED = "planned"
    LICENSE_VERIFICATION_REQUIRED = "license_verification_required"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"


class DatasetManifestEntryV1(EvaluationModel):
    schema_version: Literal["v1"]
    dataset_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    display_name: str = Field(min_length=1, max_length=200)
    role: DatasetRole
    owner: TeamOwner
    modalities: tuple[EvaluationModality, ...] = Field(min_length=1)
    allowed_tasks: tuple[str, ...]
    prohibited_claims: tuple[str, ...]
    source_reference: str | None = Field(default=None, max_length=500)
    release_or_version: str | None = Field(default=None, max_length=200)
    license_status: LicenseStatus
    license_notes: str = Field(max_length=2_000)
    local_path_placeholder: str
    git_storage_policy: GitStoragePolicy
    redistribution_status: RedistributionStatus
    evaluation_status: DatasetEvaluationStatus
    known_limitations: tuple[str, ...]

    @field_validator("local_path_placeholder")
    @classmethod
    def _validate_local_path(cls, value: str) -> str:
        check_safe_relative_path(value)
        return value

    @field_validator("license_notes", "allowed_tasks", "prohibited_claims", "known_limitations")
    @classmethod
    def _validate_safe_text(cls, value: object) -> object:
        check_forbidden_tokens(value)
        return value


class DatasetManifestV1(EvaluationModel):
    """The authoritative, locked V1 dataset manifest -- Dataset Manifest V1."""

    schema_version: Literal["v1"]
    frozen_at: datetime
    #: Structurally frozen at `False`: no dataset entry, and no combination
    #: of dataset entries, may ever be presented as one real, continuous
    #: investigation -- unrelated public benchmark records are exactly
    #: that, unrelated. See the architecture doc's "Cross-dataset
    #: composite investigation is never a claim" section.
    composite_investigation_claim_allowed: Literal[False] = False
    datasets: tuple[DatasetManifestEntryV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_manifest(self) -> DatasetManifestV1:
        ids = [entry.dataset_id for entry in self.datasets]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate dataset_id in manifest")
        for entry in self.datasets:
            if entry.role == DatasetRole.PRIMARY_BENCHMARK and not (
                entry.allowed_tasks and entry.prohibited_claims and entry.known_limitations
            ):
                raise ValueError(
                    f"primary benchmark dataset '{entry.dataset_id}' must declare "
                    "allowed_tasks, prohibited_claims, and known_limitations"
                )
            if entry.dataset_id == "operation_nightfall_v1":
                if entry.role != DatasetRole.PRIVATE_SHOWCASE_HOLDOUT:
                    raise ValueError("operation_nightfall_v1 must be role=private_showcase_holdout")
                if entry.git_storage_policy != GitStoragePolicy.EXCLUDED_PRIVATE_NEVER_DOCUMENTED:
                    raise ValueError(
                        "operation_nightfall_v1 must use the private "
                        "never-documented storage policy"
                    )
        return self

    def get(self, dataset_id: str) -> DatasetManifestEntryV1 | None:
        return next((entry for entry in self.datasets if entry.dataset_id == dataset_id), None)


# --- Model candidate catalogue -------------------------------------------


class ExecutionTarget(StrEnum):
    CPU = "cpu"
    GPU_OPTIONAL = "gpu_optional"
    GPU_REQUIRED = "gpu_required"
    #: The existing deterministic parser/rules baseline -- not a model at all.
    DETERMINISTIC_RULES = "deterministic_rules"


class CandidateSelectionStatus(StrEnum):
    CANDIDATE = "candidate"
    CONDITIONAL = "conditional"
    #: Never valid on a Part 1 candidate record -- see
    #: `ModelCandidateV1._reject_selected_in_part_1`. Reserved for the
    #: later benchmark/selection parts (Parts 2-6) to set once a winner is
    #: actually measured and frozen.
    SELECTED = "selected"


class ModelCandidateV1(EvaluationModel):
    """One catalogued candidate. Never a production model choice in Part 1."""

    schema_version: Literal["v1"]
    candidate_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,80}$")
    owner: TeamOwner
    task: CandidateTask
    framework: str = Field(min_length=1, max_length=200)
    model_family: str = Field(min_length=1, max_length=200)
    variant: str = Field(min_length=1, max_length=200)
    execution_target: ExecutionTarget
    license_status: LicenseStatus
    source_reference: str | None = Field(default=None, max_length=500)
    selection_status: CandidateSelectionStatus
    required_metrics: tuple[str, ...]
    known_limitations: tuple[str, ...]
    fallback_candidate_id: str | None = None

    @field_validator("selection_status")
    @classmethod
    def _reject_selected_in_part_1(
        cls, value: CandidateSelectionStatus
    ) -> CandidateSelectionStatus:
        if value == CandidateSelectionStatus.SELECTED:
            raise ValueError(
                "a Phase 7 Part 1 candidate must start as 'candidate' or 'conditional', "
                "never 'selected' -- selection happens in a later benchmark part"
            )
        return value

    @field_validator("known_limitations")
    @classmethod
    def _validate_safe_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        check_forbidden_tokens(value)
        return value


class ModelCandidateCatalogV1(EvaluationModel):
    schema_version: Literal["v1"]
    frozen_at: datetime
    candidates: tuple[ModelCandidateV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_catalog(self) -> ModelCandidateCatalogV1:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate_id in catalog")
        known_ids = set(ids)
        for candidate in self.candidates:
            if candidate.fallback_candidate_id is not None and (
                candidate.fallback_candidate_id not in known_ids
            ):
                raise ValueError(
                    f"candidate '{candidate.candidate_id}' names an unknown "
                    f"fallback_candidate_id '{candidate.fallback_candidate_id}'"
                )
        return self

    def by_task(self, task: CandidateTask) -> tuple[ModelCandidateV1, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.task == task)


# --- Benchmark runs and results ------------------------------------------


class SplitId(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class BenchmarkRunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class BenchmarkRunV1(EvaluationModel):
    schema_version: Literal["v1"]
    run_id: str = Field(pattern=r"^[a-z0-9_.\-]{1,120}$")
    candidate_id: str
    dataset_id: str
    dataset_release_or_version: str | None = None
    split_id: SplitId
    task: CandidateTask
    runtime_environment: str = Field(min_length=1, max_length=200)
    hardware_profile: str = Field(min_length=1, max_length=200)
    inference_config_hash: str = Field(min_length=1, max_length=128)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: BenchmarkRunStatus
    failure_reason_safe: str | None = Field(default=None, max_length=500)

    @field_validator("metrics")
    @classmethod
    def _validate_metric_keys(
        cls, value: dict[str, float | int | None]
    ) -> dict[str, float | int | None]:
        check_forbidden_tokens(value, forbidden=FORBIDDEN_METRIC_KEY_TOKENS)
        return value

    @field_validator("failure_reason_safe")
    @classmethod
    def _validate_failure_reason(cls, value: str | None) -> str | None:
        if value is not None:
            check_forbidden_tokens(value)
        return value

    @model_validator(mode="after")
    def _validate_run(self) -> BenchmarkRunV1:
        if self.status == BenchmarkRunStatus.SUCCEEDED and self.artifact_sha256 is None:
            raise ValueError(
                "a completed successful benchmark run requires artifact_sha256 -- "
                "only an unexecuted planned run may omit it"
            )
        return self


def comparable_runs(run_a: BenchmarkRunV1, run_b: BenchmarkRunV1) -> bool:
    """Frozen rule 5: a comparison is valid only for the same dataset+split.

    Two runs measured on different datasets, or the same dataset's
    different splits, are never directly comparable -- callers must not
    rank/compare across a mismatch here without explicitly marking the
    comparison non-comparable.
    """
    return run_a.dataset_id == run_b.dataset_id and run_a.split_id == run_b.split_id


# --- Benchmark metrics specification --------------------------------------


class MetricGroupSpecV1(EvaluationModel):
    task: CandidateTask
    required_metric_names: tuple[str, ...] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=1_000)


class BenchmarkMetricsSpecV1(EvaluationModel):
    schema_version: Literal["v1"]
    frozen_at: datetime
    metric_groups: tuple[MetricGroupSpecV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_spec(self) -> BenchmarkMetricsSpecV1:
        tasks = [group.task for group in self.metric_groups]
        if len(set(tasks)) != len(tasks):
            raise ValueError("duplicate task in metric_groups")
        return self

    def for_task(self, task: CandidateTask) -> MetricGroupSpecV1 | None:
        return next((group for group in self.metric_groups if group.task == task), None)


# --- Synthetic case plan ---------------------------------------------------


class CaseGroupKind(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class RequiredCrossModalScenario(StrEnum):
    SHARED_IDENTIFIER_ACROSS_MODALITIES = "shared_identifier_across_modalities"
    TIME_PROXIMATE_EVENT_SEQUENCE = "time_proximate_event_sequence"
    LOCATION_CAMERA_TOWER_LINKAGE = "location_camera_tower_linkage"
    INTENTIONAL_AMBIGUITY_OR_CONTRADICTION = "intentional_ambiguity_or_contradiction"
    EVIDENCE_SUPPORTED_TRUE_LINK = "evidence_supported_true_link"
    PLAUSIBLE_UNSUPPORTED_NON_LINK = "plausible_unsupported_non_link"
    SOURCE_LOCATORS_FOR_EVERY_FINDING = "source_locators_for_every_finding"


class RequiredReportAssertion(StrEnum):
    EVIDENCE_AND_PROVENANCE_REPORT_REQUIRED = "evidence_and_provenance_report_required"
    INVESTIGATOR_INTELLIGENCE_LEAD_REPORT_REQUIRED = (
        "investigator_intelligence_lead_report_required"
    )


_CASE_COUNT_MIN = 12
_CASE_COUNT_MAX = 15


class CaseGroupV1(EvaluationModel):
    group: CaseGroupKind
    case_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("case_ids")
    @classmethod
    def _reject_nightfall_case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for case_id in value:
            if "nightfall" in case_id.lower():
                raise ValueError(
                    "Operation Nightfall must never appear inside a development/"
                    "validation/holdout case group -- it is tracked only via "
                    "SyntheticCasePlanV1.operation_nightfall_policy, entirely "
                    "separate from the tunable synthetic case pool"
                )
        return value


class SyntheticCasePlanV1(EvaluationModel):
    schema_version: Literal["v1"]
    case_count_minimum: int = Field(ge=_CASE_COUNT_MIN, le=_CASE_COUNT_MAX)
    case_count_maximum: int = Field(ge=_CASE_COUNT_MIN, le=_CASE_COUNT_MAX)
    split_strategy: str = Field(min_length=1, max_length=500)
    case_groups: tuple[CaseGroupV1, ...] = Field(min_length=1)
    leakage_rules: tuple[str, ...] = Field(min_length=1)
    operation_nightfall_policy: str = Field(min_length=1, max_length=2_000)
    truth_file_policy: str = Field(min_length=1, max_length=2_000)
    required_modalities: tuple[EvaluationModality, ...] = Field(min_length=1)
    required_cross_modal_scenarios: tuple[RequiredCrossModalScenario, ...]
    required_report_assertions: tuple[RequiredReportAssertion, ...]

    @model_validator(mode="after")
    def _validate_plan(self) -> SyntheticCasePlanV1:
        if self.case_count_minimum > self.case_count_maximum:
            raise ValueError("case_count_minimum must be <= case_count_maximum")

        all_case_ids: list[str] = []
        seen_group_kinds: set[CaseGroupKind] = set()
        for group in self.case_groups:
            all_case_ids.extend(group.case_ids)
            seen_group_kinds.add(group.group)
        if len(set(all_case_ids)) != len(all_case_ids):
            raise ValueError("a case_id appears in more than one case group -- leakage")
        total_cases = len(all_case_ids)
        if not (self.case_count_minimum <= total_cases <= self.case_count_maximum):
            raise ValueError(
                f"total planned case count ({total_cases}) must fall within "
                f"[{self.case_count_minimum}, {self.case_count_maximum}]"
            )

        if set(self.required_cross_modal_scenarios) != set(RequiredCrossModalScenario):
            raise ValueError("every RequiredCrossModalScenario must be declared")
        if set(self.required_report_assertions) != set(RequiredReportAssertion):
            raise ValueError("both investigator-report proof requirements must be declared")

        check_forbidden_tokens(self.operation_nightfall_policy)
        check_forbidden_tokens(self.truth_file_policy)
        return self


# --- Investigator-report proof contracts -----------------------------------


class EvidenceProvenanceReportRequirementsV1(EvaluationModel):
    """What a future evidence-and-provenance report must be able to show.

    No frontend or report generator is built in Part 1 -- this is the
    proof contract Parts 2-6 and a later report generator are measured
    against.
    """

    schema_version: Literal["v1"]
    report_type: Literal["evidence_and_provenance_report"] = "evidence_and_provenance_report"
    required_elements: tuple[str, ...] = Field(min_length=1)


class InvestigatorLeadReportRequirementsV1(EvaluationModel):
    """What a future investigator intelligence-lead report must be able to show.

    Never a guilt conclusion -- `prohibited_language` names the vocabulary
    such a report must never use, mirroring the same "review-only, never a
    verdict" discipline `app.modules.integrity`'s review/hypothesis events
    already assume.
    """

    schema_version: Literal["v1"]
    report_type: Literal["investigator_intelligence_lead_report"] = (
        "investigator_intelligence_lead_report"
    )
    required_elements: tuple[str, ...] = Field(min_length=1)
    prohibited_language: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_never_a_verdict(self) -> InvestigatorLeadReportRequirementsV1:
        normalized_prohibited = {token.lower() for token in self.prohibited_language}
        if not {"guilt", "guilty"} & normalized_prohibited:
            raise ValueError("prohibited_language must explicitly name guilt-conclusion vocabulary")
        return self


def default_evidence_provenance_report_requirements() -> EvidenceProvenanceReportRequirementsV1:
    return EvidenceProvenanceReportRequirementsV1(
        schema_version=SCHEMA_VERSION,
        required_elements=(
            "case-scoped evidence/observation references",
            "source locators for every cited fact",
            "extraction/model provenance for every derived observation",
            "integrity/checkpoint/signature references where available",
            "a clear distinction between a source fact and a derived observation",
        ),
    )


def default_investigator_lead_report_requirements() -> InvestigatorLeadReportRequirementsV1:
    return InvestigatorLeadReportRequirementsV1(
        schema_version=SCHEMA_VERSION,
        required_elements=(
            "case-scoped candidate links/events/hypotheses",
            "supporting evidence for every reported item",
            "contradicting evidence for every reported item, when present",
            "a confidence/support explanation, framed as evidence-link support only",
            "review status",
            "explicit 'lead for review' language for every reported item",
        ),
        prohibited_language=(
            "guilt",
            "guilty",
            "proven criminal",
            "confirmed perpetrator",
            "probability of guilt",
        ),
    )
