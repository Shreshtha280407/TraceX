"""Safe, deterministic Phase 6 structured-observation provenance tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.contracts.common import BoundingBoxNormalized, Extractor, SourceLocator
from app.contracts.evidence import EvidenceClassification, SourceType
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.contracts.observation_batch import ObservationBatchSubmissionV1
from app.modules.evidence_lifecycle.jobs import FakeJobProducer
from app.modules.evidence_lifecycle.service import EvidenceLifecycleService, UploadContext
from app.modules.evidence_lifecycle.storage import FakeObjectStorage
from app.modules.integrity.models import IntegrityEventKind, IntegrityEventSubmission
from app.modules.integrity.structured_provenance import (
    StructuredObservationIntegrityProvenanceV1,
    build_structured_observation_provenance,
)
from tests.fixtures.evidence_lifecycle.factories import make_upload_file
from tests.fixtures.evidence_lifecycle.fake_repository import FakeEvidenceLifecycleRepository
from tests.fixtures.factories import make_batch_progress, make_observation_batch_submission

NOW = datetime(2026, 9, 15, tzinfo=UTC)
EVIDENCE_SHA = "a" * 64


def _observation(
    *,
    case_id: UUID,
    evidence_id: UUID,
    observation_type: str,
    attributes: dict[str, object],
    text: str = "Synthetic mention",
    locator: SourceLocator | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=evidence_id,
        observation_type=observation_type,
        extracted_entities=[ExtractedEntityMention(text=text, entity_type_hint="synthetic")],
        attributes=attributes,
        extraction_confidence=1.0,
        source_locator=locator or SourceLocator(page=1, span_start=0, span_end=9),
        extractor=Extractor(
            name="synthetic-parser", version="1.0", config_hash="config-v1", model_version="n/a"
        ),
        created_at=NOW,
    )


def _projection(
    source_type: SourceType, **overrides: object
) -> StructuredObservationIntegrityProvenanceV1:
    case_id = uuid4()
    evidence_id = uuid4()
    fields: dict[str, object] = {
        "case_id": case_id,
        "evidence_id": evidence_id,
        "observation_type": "document_claim",
        "attributes": {"language": "en"},
    }
    fields.update(overrides)
    observation = _observation(**fields)  # type: ignore[arg-type]
    projection = build_structured_observation_provenance(
        observation=observation, evidence_sha256=EVIDENCE_SHA, source_type=source_type
    )
    assert projection is not None
    return projection


def test_document_and_ocr_projections_commit_text_without_storing_it() -> None:
    document = _projection(SourceType.DOCUMENT)
    ocr = _projection(
        SourceType.DOCUMENT,
        attributes={
            "language": "en",
            "ocr_extractor": {"name": "tesseract", "config_hash": "ocr-config-v1"},
        },
        text="OCR text that must not persist",
        locator=SourceLocator(
            page=2,
            span_start=5,
            span_end=14,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.2, y_max=0.2),
        ),
    )
    assert document.source_family.value == "document"
    assert ocr.source_family.value == "ocr"
    assert ocr.text_content_commitment_sha256
    payload = ocr.canonical_metadata()
    assert "OCR text that must not persist" not in str(payload)
    assert "Synthetic mention" not in str(payload)
    assert "ocr_text" not in str(payload)


def test_cdr_and_finance_projections_store_only_sensitive_value_commitments() -> None:
    cdr = _projection(
        SourceType.CDR,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919900001111",
            "callee_number": "+919900002222",
            "timestamp": "2026-09-15T00:00:00+00:00",
            "call_id": "CDR-SYNTHETIC-1",
            "call_type": "voice",
            "source_signal_quality": {"outcome": "accepted"},
        },
        locator=SourceLocator(row=2),
    )
    finance = _projection(
        SourceType.FINANCIAL,
        observation_type="financial_transaction_record",
        attributes={
            "sender_account": "SENDER-SYNTHETIC",
            "receiver_account": "RECEIVER-SYNTHETIC",
            "amount": "42.00",
            "currency": "INR",
            "transaction_id": "TXN-SYNTHETIC-1",
            "direction": "debit",
            "source_signal_quality": {"outcome": "accepted"},
        },
        locator=SourceLocator(sheet="Transactions", row=2),
    )
    for projection, forbidden in (
        (cdr, ("+919900001111", "+919900002222", "CDR-SYNTHETIC-1")),
        (
            finance,
            ("SENDER-SYNTHETIC", "RECEIVER-SYNTHETIC", "42.00", "TXN-SYNTHETIC-1"),
        ),
    ):
        assert all(value not in str(projection.canonical_metadata()) for value in forbidden)
    assert cdr.caller_value_commitment_sha256 and cdr.callee_value_commitment_sha256
    assert finance.sender_value_commitment_sha256 and finance.amount_currency_commitment_sha256


@pytest.mark.parametrize(
    ("source_type", "first", "second"),
    [
        (SourceType.DOCUMENT, {"language": "en"}, {"language": "hi"}),
        (
            SourceType.CDR,
            {"caller_number": "caller-A", "callee_number": "callee-B"},
            {"caller_number": "caller-C", "callee_number": "callee-B"},
        ),
        (
            SourceType.FINANCIAL,
            {"sender_account": "sender-A", "receiver_account": "receiver-B", "amount": "1"},
            {"sender_account": "sender-A", "receiver_account": "receiver-B", "amount": "2"},
        ),
    ],
)
def test_changed_safe_provenance_input_changes_commitment(
    source_type: SourceType, first: dict[str, object], second: dict[str, object]
) -> None:
    case_id, evidence_id, observation_id = uuid4(), uuid4(), uuid4()
    base = {"case_id": case_id, "evidence_id": evidence_id, "observation_type": "synthetic"}
    one = _observation(**base, attributes=first).model_copy(
        update={"observation_id": observation_id}
    )
    two = _observation(**base, attributes=second).model_copy(
        update={"observation_id": observation_id}
    )
    first_projection = build_structured_observation_provenance(
        observation=one, evidence_sha256=EVIDENCE_SHA, source_type=source_type
    )
    second_projection = build_structured_observation_provenance(
        observation=two, evidence_sha256=EVIDENCE_SHA, source_type=source_type
    )
    assert first_projection is not None and second_projection is not None
    assert first_projection.idempotency_key == second_projection.idempotency_key
    assert first_projection.canonical_payload_sha256 != second_projection.canonical_payload_sha256


def test_rejected_or_incomplete_source_signal_creates_no_projection() -> None:
    observation = _observation(
        case_id=uuid4(),
        evidence_id=uuid4(),
        observation_type="cdr_call_record",
        attributes={"source_signal_quality": {"outcome": "rejected"}},
    )
    assert (
        build_structured_observation_provenance(
            observation=observation, evidence_sha256=EVIDENCE_SHA, source_type=SourceType.CDR
        )
        is None
    )


class _RecordingIntegrity:
    def __init__(self) -> None:
        self.generic_events: list[IntegrityEventSubmission] = []
        self.structured: list[StructuredObservationIntegrityProvenanceV1] = []

    async def record_integrity_event(self, submission: IntegrityEventSubmission) -> None:
        self.generic_events.append(submission)

    async def record_structured_observation_provenance(
        self, projection: StructuredObservationIntegrityProvenanceV1, *, source_created_at: datetime
    ) -> None:
        assert source_created_at == NOW
        self.structured.append(projection)


async def test_accepted_batch_adds_one_structured_leaf_without_duplicate_generic_leaf() -> None:
    repository = FakeEvidenceLifecycleRepository()
    recorder = _RecordingIntegrity()
    service = EvidenceLifecycleService(
        repository=repository,
        storage=FakeObjectStorage(),
        job_producer=FakeJobProducer(),
        max_evidence_bytes=1024,
        integrity_recorder=recorder,  # type: ignore[arg-type]
    )
    case_id = uuid4()
    await service.upload_evidence(
        case_id=case_id,
        uploaded_by=uuid4(),
        source_type=SourceType.CDR,
        classification=EvidenceClassification.UNCLASSIFIED,
        parser_profile=None,
        upload=make_upload_file(content=b"a,b\n1,2\n", content_type="text/csv"),
        idempotency_key=None,
        context=UploadContext(now=NOW, request_id="req-structured-provenance"),
    )
    claim = await service.claim_job(
        processor_name="cdr_generic_v1",
        processor_version="1.0.0",
        context=UploadContext(now=NOW, request_id="req-structured-provenance"),
    )
    assert claim.job and claim.claim_token
    observation = _observation(
        case_id=case_id,
        evidence_id=claim.job.evidence_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "caller-A",
            "callee_number": "callee-B",
            "source_signal_quality": {"outcome": "accepted"},
        },
        locator=SourceLocator(row=2),
    )
    submission = ObservationBatchSubmissionV1(
        **make_observation_batch_submission(
            job_id=claim.job.job_id,
            case_id=case_id,
            evidence_id=claim.job.evidence_id,
            observations=[observation],
            progress=make_batch_progress(batch_sequence=0),
            submitted_at=NOW,
        ).model_dump()
    )
    context = UploadContext(now=NOW, request_id="req-structured-provenance")
    await service.submit_observation_batch(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        submission=submission,
        context=context,
    )
    await service.submit_observation_batch(
        job_id=claim.job.job_id,
        claim_token=claim.claim_token,
        submission=submission,
        context=context,
    )
    generic = [
        event
        for event in recorder.generic_events
        if event.event_kind is IntegrityEventKind.OBSERVATION_PUBLISHED
    ]
    assert len(generic) == 1
    assert len(recorder.structured) == 1
