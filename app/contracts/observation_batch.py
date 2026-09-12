"""ObservationBatchSubmissionV1: a worker's partial, provenance-rich micro-batch.

Phase 3 (Nipun): the batch-submission counterpart to `WorkerResultV1`. A
source worker processing a large document/CDR/finance file emits many of
these while still running, then submits one terminal `WorkerResultV1` (see
`app/contracts/worker.py`) exactly as before -- this contract does not
replace or change that terminal step's shape or semantics in any way.

Two independent opaque identifiers travel with every submission, deliberately
not conflated:

- `batch_id` names *this specific batch* -- retried under the same `job_id`
  + `batch_id`, it is the same logical batch (compare payload to
  replay-or-conflict).
- `idempotency_key` is a second, independently-unique opaque token per job,
  a defense-in-depth safety net against a `batch_id` accidentally reused for
  a genuinely different batch (or vice versa) -- see
  `docs/architecture/phase-3-decisions.md`.

Every observation/transformation embedded here must already carry the same
`case_id`/`evidence_id`/`job_id`/`batch_id` as the submission itself --
enforced here, at the contract level, as defense in depth; the authoritative
check (against the server's own record of which job was actually claimed) is
`EvidenceLifecycleService.submit_observation_batch`'s job.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.contracts.common import ContractVersion, SourceLocator, TraceXModel
from app.contracts.observation import ObservationV1
from app.contracts.worker import DerivedArtifact

#: Opaque, worker-supplied `batch_id`/`idempotency_key` tokens: bounded length,
#: safe charset -- never interpreted, only compared and stored.
_OPAQUE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

#: A safe, non-content progress classifier -- never raw document/media text.
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MESSAGE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: Key substrings (case-insensitive) that must never appear in
#: `TransformationProvenanceV1.safe_metadata` -- a coarse but effective net
#: against accidentally persisting a credential, a raw stderr dump, or a
#: local filesystem path alongside otherwise-safe step bookkeeping.
_FORBIDDEN_METADATA_KEY_SUBSTRINGS = (
    "password",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "stderr",
    "stacktrace",
    "traceback",
    "object_uri",
    "filepath",
    "file_path",
    "dsn",
    "private_key",
)
#: Above this length, a string value is treated as "probably raw content"
#: (document text, a media byte dump, a full log) rather than safe,
#: short bookkeeping metadata -- rejected outright rather than silently
#: truncated, so a caller learns immediately rather than losing data quietly.
_MAX_METADATA_STRING_LENGTH = 500


def _validate_safe_metadata(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    def walk(node: JsonValue, *, key: str | None) -> None:
        if key is not None:
            lowered = key.lower()
            for forbidden in _FORBIDDEN_METADATA_KEY_SUBSTRINGS:
                if forbidden in lowered:
                    raise ValueError(
                        f"safe_metadata key '{key}' looks secret/unsafe-bearing "
                        f"(matches '{forbidden}') and is not permitted"
                    )
        if isinstance(node, str):
            if len(node) > _MAX_METADATA_STRING_LENGTH:
                raise ValueError(
                    "safe_metadata string values must be short bookkeeping data, not raw "
                    f"content (max {_MAX_METADATA_STRING_LENGTH} chars)"
                )
        elif isinstance(node, dict):
            for child_key, child_value in node.items():
                walk(child_value, key=child_key)
        elif isinstance(node, list):
            for item in node:
                walk(item, key=None)

    walk(value, key=None)
    return value


class TransformationStatus(StrEnum):
    """Outcome of one transformation/extraction step within a batch."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class TransformationProvenanceV1(TraceXModel):
    """Durable, review-safe provenance for one processing step within a batch.

    Answers: which tool/model/config ran, over which source range, producing
    which observations -- without ever carrying the raw content it consumed.
    Immutable once accepted (see `docs/architecture/phase-3-decisions.md`).
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    transformation_id: UUID
    case_id: UUID
    evidence_id: UUID
    job_id: UUID
    batch_id: str = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    ordinal: int = Field(ge=0)
    step_name: str = Field(min_length=1)
    step_version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    #: Required even for non-ML steps (use `"n/a"`) -- mirrors
    #: `Extractor.model_version`'s identical reasoning.
    model_version: str = Field(min_length=1)
    input_locator: SourceLocator | None = None
    output_observation_ids: list[UUID] = Field(default_factory=list)
    derived_artifact_refs: list[DerivedArtifact] = Field(default_factory=list)
    status: TransformationStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    safe_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_timing_and_metadata(self) -> TransformationProvenanceV1:
        if self.completed_at < self.started_at:
            raise ValueError("transformation completed_at must be >= started_at")
        _validate_safe_metadata(self.safe_metadata)
        return self


class ObservationBatchProgressV1(TraceXModel):
    """One durable progress report: operational state, never an investigative conclusion.

    `message_code` is a short, safe, uppercase-snake-case classifier (e.g.
    `PAGE_PARSED`) -- never a human-readable message that might embed raw
    document content.
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    stage: str = Field(pattern=_STAGE_PATTERN)
    units_total: int | None = Field(default=None, ge=0)
    units_completed: int = Field(ge=0)
    observations_emitted: int = Field(ge=0)
    batch_sequence: int = Field(ge=0)
    message_code: str | None = Field(default=None, pattern=_MESSAGE_CODE_PATTERN)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_coherence(self) -> ObservationBatchProgressV1:
        if self.units_total is not None and self.units_completed > self.units_total:
            raise ValueError("units_completed must not exceed units_total")
        return self


class ObservationBatchSubmissionV1(TraceXModel):
    """One partial, provenance-rich micro-batch submitted by an authorized worker.

    `observations`/`transformations` may both be empty **only** if `progress`
    carries a real update -- an entirely empty batch is meaningless and
    rejected (see `docs/architecture/phase-3-decisions.md`).
    """

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    job_id: UUID
    case_id: UUID
    evidence_id: UUID
    batch_id: str = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    batch_sequence: int = Field(ge=0)
    idempotency_key: str = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    observations: list[ObservationV1] = Field(default_factory=list)
    transformations: list[TransformationProvenanceV1] = Field(default_factory=list)
    progress: ObservationBatchProgressV1 | None = None
    submitted_at: AwareDatetime
    is_final_batch: bool = False

    @model_validator(mode="after")
    def _validate_batch(self) -> ObservationBatchSubmissionV1:
        if not self.observations and not self.transformations and self.progress is None:
            raise ValueError(
                "an empty observation batch must carry a progress or transformation update"
            )

        observation_ids = [o.observation_id for o in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("duplicate observation_id within a single batch is not permitted")
        observation_id_set = set(observation_ids)

        for observation in self.observations:
            if observation.case_id != self.case_id or observation.evidence_id != self.evidence_id:
                raise ValueError(
                    "an observation's case_id/evidence_id must match the submission's own"
                )

        ordinals = [t.ordinal for t in self.transformations]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("duplicate transformation ordinal within a single batch")

        for transformation in self.transformations:
            if (
                transformation.case_id != self.case_id
                or transformation.evidence_id != self.evidence_id
                or transformation.job_id != self.job_id
                or transformation.batch_id != self.batch_id
            ):
                raise ValueError(
                    "a transformation's case_id/evidence_id/job_id/batch_id must match "
                    "the submission's own"
                )
            unknown = [
                oid
                for oid in transformation.output_observation_ids
                if oid not in observation_id_set
            ]
            if unknown:
                raise ValueError(
                    "a transformation's output_observation_ids must all be present in this "
                    "same batch's observations"
                )

        if self.progress is not None and self.progress.batch_sequence != self.batch_sequence:
            raise ValueError("progress.batch_sequence must match the submission's batch_sequence")

        return self


class BatchAcceptanceStatus(StrEnum):
    """Whether a batch submission was newly accepted or is a replay of a prior one."""

    ACCEPTED = "accepted"
    REPLAYED = "replayed"


class ObservationBatchReceiptV1(TraceXModel):
    """Safe acknowledgement of a submitted batch -- never an object URI or credential."""

    schema_version: Literal[ContractVersion.V1] = ContractVersion.V1
    job_id: UUID
    batch_id: str
    status: BatchAcceptanceStatus
    accepted_observation_count: int = Field(ge=0)
    progress: ObservationBatchProgressV1 | None = None
    request_id: str | None = None
