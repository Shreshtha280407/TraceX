"""Phase 6 tamper-evident integrity models: events, Merkle checkpoints, signatures.

These are internal, versioned integration records -- like
`app.modules.graph.integration_models` -- not additions to the frozen
`app.contracts` V1 payloads.

An integrity event is a safe, non-secret fingerprint of a durable write. Its
`canonical_metadata` (validated below, then hashed by the service layer into
`canonical_payload_sha256`) is *never itself persisted* -- only its hash is.
That is the primary defence against ever storing raw evidence, transcript,
chat, or OCR text in this module: the content never reaches storage in the
first place. The forbidden-token scan on submission is defence in depth
against a producer accidentally passing raw content as "metadata".

`REVIEW_DECISION` and `HYPOTHESIS_ACTION` are forward-compatible event kinds
only -- Shreshtha's later phase builds the workflow that emits them. No
review/hypothesis logic exists here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

INTEGRITY_SCHEMA_VERSION = "v1"
MERKLE_TREE_FORMAT_VERSION = "tracex-sha256-domain-separated-v1"
SIGNATURE_ALGORITHM = "ed25519"
SIGNATURE_ENCODING = "base64"

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_.:-]{1,200}$"

_FORBIDDEN_METADATA_TOKENS = {
    "password",
    "secret",
    "token",
    "credential",
    "object_uri",
    "dsn",
    "transcript",
    "chat_body",
    "ocr_text",
    "private_key",
    "raw_text",
    "body_text",
    "message_body",
}
_MAX_METADATA_STRING_LENGTH = 2_000


def _validate_metadata_value(value: Any, forbidden: set[str]) -> None:
    """Reject source-like payloads at every nesting level, not only the root."""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if any(token in normalized for token in forbidden):
                raise ValueError("integrity metadata contains a prohibited key")
            _validate_metadata_value(nested, forbidden)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_metadata_value(nested, forbidden)
    elif isinstance(value, str) and len(value) > _MAX_METADATA_STRING_LENGTH:
        raise ValueError("integrity metadata string values must be bounded")


class IntegrityModel(BaseModel):
    """Base class for internal integrity-module models: immutable, no stray fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class IntegrityEventKind(StrEnum):
    """Producer boundaries this module can seal. See module docstring for scope."""

    EVIDENCE_REGISTERED = "evidence_registered"
    OBSERVATION_PUBLISHED = "observation_published"
    CORRELATION_COMPLETED = "correlation_completed"
    REVIEW_DECISION = "review_decision"
    HYPOTHESIS_ACTION = "hypothesis_action"


class IntegrityEventSubmission(IntegrityModel):
    """The producer-facing write boundary for one integrity event.

    `canonical_metadata` is hashed by the service layer and then discarded --
    it never reaches the repository or the database.
    """

    case_id: UUID
    event_kind: IntegrityEventKind
    subject_type: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=200)
    canonical_metadata: dict[str, Any] = Field(default_factory=dict)
    payload_schema_version: str = Field(min_length=1, max_length=32)
    source_created_at: datetime
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)

    @model_validator(mode="after")
    def _reject_unsafe_metadata(self) -> IntegrityEventSubmission:
        _validate_metadata_value(self.canonical_metadata, _FORBIDDEN_METADATA_TOKENS)
        return self


class IntegrityEventRecord(IntegrityModel):
    integrity_event_id: UUID
    case_id: UUID
    event_kind: IntegrityEventKind
    subject_type: str
    subject_id: str
    canonical_payload_sha256: str
    payload_schema_version: str
    source_created_at: datetime
    idempotency_key: str
    sequence_number: int
    created_at: datetime


class MerkleCheckpointRecord(IntegrityModel):
    checkpoint_id: UUID
    case_id: UUID
    start_sequence: int
    end_sequence: int
    leaf_count: int
    root_hash: str
    tree_format_version: str
    created_at: datetime


class CheckpointSignatureRecord(IntegrityModel):
    signature_id: UUID
    checkpoint_id: UUID
    case_id: UUID
    key_id: str
    algorithm: str
    signature_encoding: str
    signature: str
    public_key_b64: str
    public_key_fingerprint: str
    signed_root_hash: str
    signed_at: datetime


class CheckpointBuildReceipt(IntegrityModel):
    checkpoint: MerkleCheckpointRecord
    signature: CheckpointSignatureRecord
    replayed: bool


class VerificationLeaf(IntegrityModel):
    """One leaf's safe, public verification material -- no source content."""

    integrity_event_id: UUID
    sequence_number: int
    event_kind: IntegrityEventKind
    subject_type: str
    subject_id: str
    canonical_payload_sha256: str
    payload_schema_version: str


class VerificationBundle(IntegrityModel):
    """Portable export: only public verification material and hashed metadata."""

    case_id: UUID
    checkpoint: MerkleCheckpointRecord
    signature: CheckpointSignatureRecord
    leaves: tuple[VerificationLeaf, ...]
    bundle_schema_version: str = INTEGRITY_SCHEMA_VERSION


class VerificationResult(IntegrityModel):
    checkpoint_id: UUID
    case_id: UUID
    leaf_count_matches: bool
    root_matches: bool
    signature_valid: bool
    ok: bool
    reason: str | None = None


class StructuredObservationProvenanceRecord(IntegrityModel):
    """Durable safe projection paired with one structured integrity leaf.

    ``canonical_payload`` is already safe, committed metadata produced by
    ``structured_provenance.py``; unlike normal observation payloads it never
    contains an extracted entity, raw source locator, or source attribute.
    """

    provenance_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    source_family: str
    schema_version: str
    canonical_payload: dict[str, Any]
    canonical_payload_sha256: str
    idempotency_key: str
    source_created_at: datetime
    created_at: datetime


class ModalityObservationProvenanceRecord(IntegrityModel):
    """Durable safe visual/communication projection paired with one leaf.

    ``canonical_payload`` contains only the typed, commitment-only modality
    projection.  It is intentionally separate from a canonical observation
    payload, which may contain protected source content.
    """

    provenance_id: UUID
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    provenance_kind: str
    source_family: str
    schema_version: str
    canonical_payload: dict[str, Any]
    canonical_payload_sha256: str
    idempotency_key: str
    source_created_at: datetime
    created_at: datetime
