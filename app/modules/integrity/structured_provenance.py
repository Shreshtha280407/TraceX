"""Safe, versioned provenance commitments for structured observations.

This module deliberately receives an already accepted ``ObservationV1`` at
the evidence-lifecycle publication boundary.  It projects only structural
metadata and SHA-256 commitments; it never persists observation text,
identifiers, amounts, source locations, or source attributes themselves.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.evidence import SourceType
from app.contracts.observation import ObservationV1
from app.core.canonical import canonical_sha256
from app.modules.integrity.models import (
    IntegrityEventKind,
    IntegrityEventSubmission,
    IntegrityModel,
)

STRUCTURED_OBSERVATION_PROVENANCE_SCHEMA_VERSION = "structured_provenance.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StructuredSourceFamily(StrEnum):
    DOCUMENT = "document"
    OCR = "ocr"
    CDR = "cdr"
    FINANCE = "finance"


class StructuredObservationIntegrityProvenanceV1(IntegrityModel):
    """Immutable, safe metadata commitment for one canonical observation.

    Optional family-specific fields are populated only where the accepted
    observation supplied that structural signal.  Every potentially sensitive
    source value is represented solely by a SHA-256 commitment.
    """

    schema_version: Literal["structured_provenance.v1"] = "structured_provenance.v1"
    case_id: UUID
    evidence_id: UUID
    observation_id: UUID
    source_family: StructuredSourceFamily
    observation_type: str = Field(min_length=1, max_length=128)
    evidence_sha256: str
    source_locator_kind: str = Field(min_length=1, max_length=128)
    source_locator_commitment_sha256: str
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_version: str = Field(min_length=1, max_length=128)
    extractor_config_hash: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    validation_outcome: str = Field(min_length=1, max_length=32)
    correlation_ready: bool

    # Document / OCR structural metadata.
    page_number: int | None = Field(default=None, ge=1)
    span_commitment_sha256: str | None = None
    normalized_bbox_commitment_sha256: str | None = None
    text_content_commitment_sha256: str | None = None
    document_processing_config_hash: str | None = None
    language_or_script: str | None = Field(default=None, max_length=32)

    # CDR structural metadata.
    source_row_commitment_sha256: str | None = None
    event_time_commitment_sha256: str | None = None
    caller_value_commitment_sha256: str | None = None
    callee_value_commitment_sha256: str | None = None
    communication_type: str | None = Field(default=None, max_length=64)
    cdr_field_presence: dict[str, bool] | None = None

    # Finance structural metadata.
    sender_value_commitment_sha256: str | None = None
    receiver_value_commitment_sha256: str | None = None
    transaction_reference_commitment_sha256: str | None = None
    amount_currency_commitment_sha256: str | None = None
    transaction_type: str | None = Field(default=None, max_length=64)
    finance_field_presence: dict[str, bool] | None = None

    @model_validator(mode="after")
    def _validate_commitments(self) -> StructuredObservationIntegrityProvenanceV1:
        for field_name in self.__class__.model_fields:
            if field_name.endswith("_sha256"):
                candidate = getattr(self, field_name)
                if candidate is not None and not _SHA256.fullmatch(candidate):
                    raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
        return self

    @property
    def idempotency_key(self) -> str:
        """Stable retry key: case + observation + immutable schema version."""
        return f"structured-provenance:{self.case_id}:{self.observation_id}:{self.schema_version}"

    def canonical_metadata(self) -> dict[str, Any]:
        """The exact safe projection committed both durably and in the leaf."""
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def canonical_payload_sha256(self) -> str:
        return canonical_sha256(self.canonical_metadata())

    def to_integrity_submission(self, *, source_created_at: datetime) -> IntegrityEventSubmission:
        return IntegrityEventSubmission(
            case_id=self.case_id,
            event_kind=IntegrityEventKind.OBSERVATION_PUBLISHED,
            subject_type="structured_observation_provenance",
            subject_id=str(self.observation_id),
            canonical_metadata=self.canonical_metadata(),
            payload_schema_version=self.schema_version,
            source_created_at=source_created_at,
            idempotency_key=self.idempotency_key,
        )


def build_structured_observation_provenance(
    *,
    observation: ObservationV1,
    evidence_sha256: str,
    source_type: SourceType,
) -> StructuredObservationIntegrityProvenanceV1 | None:
    """Build a safe commitment only for the four owned structured families."""
    source_family = _source_family(observation, source_type)
    if source_family is None:
        return None

    attributes = observation.attributes
    validation = attributes.get("source_signal_quality")
    validation_outcome = (
        str(validation.get("outcome"))
        if isinstance(validation, dict) and isinstance(validation.get("outcome"), str)
        else "accepted"
    )
    if validation_outcome != "accepted":
        return None
    # This records the producer's existing validation statement only.  It
    # neither invokes graph sourcing nor changes candidate eligibility.
    correlation_ready = validation_outcome == "accepted"
    locator = observation.source_locator.model_dump(mode="json", exclude_none=True)
    locator_kind = "+".join(sorted(locator))
    values: dict[str, Any] = {
        "case_id": observation.case_id,
        "evidence_id": observation.evidence_id,
        "observation_id": observation.observation_id,
        "source_family": source_family,
        "observation_type": observation.observation_type,
        "evidence_sha256": evidence_sha256,
        "source_locator_kind": locator_kind,
        "source_locator_commitment_sha256": canonical_sha256(locator),
        "extractor_name": observation.extractor.name,
        "extractor_version": observation.extractor.version,
        "extractor_config_hash": observation.extractor.config_hash,
        "model_version": observation.extractor.model_version,
        "validation_outcome": validation_outcome,
        "correlation_ready": correlation_ready,
    }

    if source_family in {StructuredSourceFamily.DOCUMENT, StructuredSourceFamily.OCR}:
        values.update(_document_fields(observation))
    elif source_family is StructuredSourceFamily.CDR:
        values.update(_cdr_fields(observation))
    else:
        values.update(_finance_fields(observation))
    return StructuredObservationIntegrityProvenanceV1.model_validate(values)


def _source_family(
    observation: ObservationV1, source_type: SourceType
) -> StructuredSourceFamily | None:
    if source_type is SourceType.DOCUMENT:
        return (
            StructuredSourceFamily.OCR
            if isinstance(observation.attributes.get("ocr_extractor"), dict)
            else StructuredSourceFamily.DOCUMENT
        )
    if source_type is SourceType.CDR:
        return StructuredSourceFamily.CDR
    if source_type is SourceType.FINANCIAL:
        return StructuredSourceFamily.FINANCE
    return None


def _commit(value: object) -> str:
    return canonical_sha256({"committed_value": value})


def _document_fields(observation: ObservationV1) -> dict[str, Any]:
    locator = observation.source_locator
    ocr = observation.attributes.get("ocr_extractor")
    language = observation.attributes.get("language")
    # The extracted strings are deliberately consumed only as a hash input;
    # no text, OCR region, or entity value survives this projection.
    text_values = [entity.text for entity in observation.extracted_entities]
    return {
        "page_number": locator.page,
        "span_commitment_sha256": _commit({"start": locator.span_start, "end": locator.span_end})
        if locator.span_start is not None or locator.span_end is not None
        else None,
        "normalized_bbox_commitment_sha256": _commit(locator.bbox_xyxy_normalized)
        if locator.bbox_xyxy_normalized is not None
        else None,
        "text_content_commitment_sha256": _commit(text_values) if text_values else None,
        "document_processing_config_hash": (
            str(ocr.get("config_hash"))
            if isinstance(ocr, dict) and isinstance(ocr.get("config_hash"), str)
            else observation.extractor.config_hash
        ),
        "language_or_script": language
        if isinstance(language, str) and len(language) <= 32
        else None,
    }


def _cdr_fields(observation: ObservationV1) -> dict[str, Any]:
    attributes = observation.attributes
    return {
        "source_row_commitment_sha256": _commit(observation.source_locator),
        "event_time_commitment_sha256": _commit(
            {"event_time": observation.event_time, "time_window": observation.time_window}
        ),
        "caller_value_commitment_sha256": _optional_commit(attributes.get("caller_number")),
        "callee_value_commitment_sha256": _optional_commit(attributes.get("callee_number")),
        "communication_type": _safe_category(attributes.get("call_type")),
        "cdr_field_presence": _presence_map(
            attributes,
            (
                "caller_number",
                "callee_number",
                "timestamp",
                "timestamp_end",
                "call_type",
                "call_id",
            ),
        ),
    }


def _finance_fields(observation: ObservationV1) -> dict[str, Any]:
    attributes = observation.attributes
    reference = attributes.get("transaction_id") or attributes.get("reference")
    return {
        "source_row_commitment_sha256": _commit(observation.source_locator),
        "event_time_commitment_sha256": _commit(
            {"event_time": observation.event_time, "time_window": observation.time_window}
        ),
        "sender_value_commitment_sha256": _optional_commit(attributes.get("sender_account")),
        "receiver_value_commitment_sha256": _optional_commit(attributes.get("receiver_account")),
        "transaction_reference_commitment_sha256": _optional_commit(reference),
        "amount_currency_commitment_sha256": _commit(
            {"amount": attributes.get("amount"), "currency": attributes.get("currency")}
        ),
        "transaction_type": _safe_category(attributes.get("direction")),
        "finance_field_presence": _presence_map(
            attributes,
            (
                "sender_account",
                "receiver_account",
                "amount",
                "currency",
                "transaction_id",
                "reference",
            ),
        ),
    }


def _optional_commit(value: object) -> str | None:
    return _commit(value) if value is not None else None


def _presence_map(attributes: dict[str, Any], fields: tuple[str, ...]) -> dict[str, bool]:
    return {field: field in attributes and attributes[field] is not None for field in fields}


def _safe_category(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if re.fullmatch(r"[a-z0-9_-]{1,64}", normalized) else None


__all__ = [
    "STRUCTURED_OBSERVATION_PROVENANCE_SCHEMA_VERSION",
    "StructuredObservationIntegrityProvenanceV1",
    "StructuredSourceFamily",
    "build_structured_observation_provenance",
]
