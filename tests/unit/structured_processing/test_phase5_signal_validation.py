"""Phase 5B producer-side validation and sourcing-boundary regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import BoundingBoxNormalized, SourceLocator
from app.modules.graph.intelligence.sourcing import build_motif_edges
from app.modules.structured_processing.document.ner_fallback import DeterministicNerAdapter
from app.modules.structured_processing.document.ocr import OcrPageResult, OcrRegion
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.provenance import mention_to_observation
from app.modules.structured_processing.signal_validation import (
    SignalValidationOutcome,
    SignalValidationResult,
)
from app.modules.structured_processing.structured.cdr import normalize_cdr_records
from app.modules.structured_processing.structured.csv_parser import parse_csv
from app.modules.structured_processing.structured.finance import normalize_financial_records
from app.modules.structured_processing.structured.json_parser import parse_json_records
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
)
from app.modules.structured_processing.structured.xlsx_parser import parse_xlsx
from app.modules.structured_processing.worker import _extract_page_mentions
from tests.fixtures.structured_processing.builders import build_xlsx

CASE_ID = uuid4()
EVIDENCE_ID = uuid4()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _record_observation(profile: object, mention: object):
    return mention_to_observation(
        case_id=CASE_ID,
        evidence_id=EVIDENCE_ID,
        profile=profile,  # type: ignore[arg-type]
        mention=mention,  # type: ignore[arg-type]
        created_at=NOW,
    )


def test_cdr_participants_locator_quality_and_event_time_survive_to_canonical_observation() -> None:
    mentions = normalize_cdr_records(
        parse_csv(
            b"Caller,Called Number,Start Time,Duration,Call ID\n"
            b" caller-A , callee-B ,2026-01-01 10:00:00,15,CDR-001\n"
        )
    )
    mention = next(item for item in mentions if item.observation_type == "cdr_call_record")
    observation = _record_observation(CDR_GENERIC_V1, mention)

    assert observation.attributes["caller_number"] == "caller-A"
    assert observation.attributes["callee_number"] == "callee-B"
    assert observation.attributes["participants"] == [
        {"role": "caller", "identifier": "caller-A"},
        {"role": "callee", "identifier": "callee-B"},
    ]
    assert observation.attributes["call_id"] == "CDR-001"
    assert observation.source_locator.row == 2
    assert observation.event_time is not None
    assert observation.extractor.config_hash
    quality = observation.attributes["source_signal_quality"]
    assert quality["outcome"] == "accepted"  # type: ignore[index]


def test_finance_participants_locator_and_event_time_survive_to_canonical_observation() -> None:
    mentions = normalize_financial_records(
        parse_csv(
            b"From Account,To Account,Value,CCY,Transaction Date,Txn Ref,Mode\n"
            b" sender-A , receiver-B ,12.340,INR,2026-01-01 10:00:00,TXN-001,UPI\n"
        )
    )
    mention = next(
        item for item in mentions if item.observation_type == "financial_transaction_record"
    )
    observation = _record_observation(FINANCIAL_TRANSACTION_GENERIC_V1, mention)

    assert observation.attributes["sender_account"] == "sender-A"
    assert observation.attributes["receiver_account"] == "receiver-B"
    assert observation.attributes["participants"] == [
        {"role": "sender", "identifier": "sender-A"},
        {"role": "receiver", "identifier": "receiver-B"},
    ]
    assert observation.attributes["transaction_id"] == "TXN-001"
    assert observation.attributes["channel"] == "UPI"
    assert observation.source_locator.row == 2
    assert observation.event_time is not None


def test_both_source_parties_reach_phase5_evidence_local_motif_boundary_without_swap() -> None:
    cdr = _record_observation(
        CDR_GENERIC_V1,
        next(
            item
            for item in normalize_cdr_records(
                parse_csv(
                    b"caller,callee,timestamp\n+919876543210,+919123456789,2026-01-01 10:00:00\n"
                )
            )
            if item.observation_type == "cdr_call_record"
        ),
    )
    finance = _record_observation(
        FINANCIAL_TRANSACTION_GENERIC_V1,
        next(
            item
            for item in normalize_financial_records(
                parse_csv(
                    b"from_account,to_account,amount,currency,timestamp\n"
                    b"sender-A,receiver-B,5,INR,2026-01-01 10:05:00\n"
                )
            )
            if item.observation_type == "financial_transaction_record"
        ),
    )

    edges = build_motif_edges([cdr, finance])
    assert [(edge.event_kind, edge.left_id != edge.right_id) for edge in edges] == [
        ("cdr_call", True),
        ("financial_transaction", True),
    ]
    assert all(edge.evidence_observation_ids == (edge.event_id,) for edge in edges)


@pytest.mark.parametrize(
    ("normalizer", "payload", "code"),
    [
        (
            normalize_cdr_records,
            b"caller,callee,timestamp\n ,callee-B,2026-01-01 10:00:00\n",
            ErrorCode.REQUIRED_FIELD_MISSING,
        ),
        (
            normalize_financial_records,
            b"sender_account,receiver_account,amount,currency,timestamp\n"
            b"sender-A, ,5,INR,2026-01-01 10:00:00\n",
            ErrorCode.REQUIRED_FIELD_MISSING,
        ),
    ],
)
def test_blank_or_missing_parties_are_not_published_as_correlation_ready_records(
    normalizer: object, payload: bytes, code: str
) -> None:
    with pytest.raises(ProcessingError) as exc_info:
        normalizer(parse_csv(payload))  # type: ignore[operator]
    assert exc_info.value.code == code


def test_invalid_cdr_range_and_negative_finance_amount_are_rejected_deterministically() -> None:
    cdr_payload = (
        b"caller,callee,timestamp,end_time\n"
        b"caller-A,callee-B,2026-01-01 10:00:00,2026-01-01 09:59:59\n"
    )
    finance_payload = (
        b"sender_account,receiver_account,amount,currency,timestamp\n"
        b"sender-A,receiver-B,-0.01,INR,2026-01-01 10:00:00\n"
    )
    pairs = (
        (normalize_cdr_records, cdr_payload),
        (normalize_financial_records, finance_payload),
    )
    for normalizer, payload in pairs:
        with pytest.raises(ProcessingError) as exc_info:
            normalizer(parse_csv(payload))
        assert exc_info.value.code == ErrorCode.INVALID_SOURCE_SIGNAL


def test_json_and_xlsx_explicit_aliases_preserve_the_same_roles() -> None:
    json_records = parse_json_records(
        b'[{"a_number":"caller-A","b_number":"callee-B","call_time":"2026-01-01 10:00:00"}]'
    )
    cdr = next(
        item
        for item in normalize_cdr_records(json_records)
        if item.observation_type == "cdr_call_record"
    )
    assert cdr.locator.json_path == "$[0]"
    assert cdr.attributes["participants"][1] == {"role": "callee", "identifier": "callee-B"}  # type: ignore[index]

    xlsx_records = parse_xlsx(
        build_xlsx(
            headers=["From Account", "To Account", "Value", "CCY", "Transaction Date"],
            rows=[["sender-A", "receiver-B", "5", "INR", "2026-01-01 10:00:00"]],
            sheet_name="Transfers",
        )
    )
    finance = next(
        item
        for item in normalize_financial_records(xlsx_records)
        if item.observation_type == "financial_transaction_record"
    )
    assert finance.attributes["participants"][0] == {"role": "sender", "identifier": "sender-A"}  # type: ignore[index]
    assert finance.locator.sheet == "Transfers"
    assert finance.locator.row == 2


def test_free_form_reference_is_not_copied_to_graph_facing_attributes() -> None:
    raw_reference = "synthetic narrative that must not reach graph attributes"
    record = next(
        item
        for item in normalize_financial_records(
            parse_csv(
                (
                    "sender_account,receiver_account,amount,currency,timestamp,description\n"
                    f"sender-A,receiver-B,5,INR,2026-01-01 10:00:00,{raw_reference}\n"
                ).encode()
            )
        )
        if item.observation_type == "financial_transaction_record"
    )
    assert raw_reference not in str(record.attributes)
    assert record.attributes["reference_unusable"] is True


def test_validation_result_has_all_safe_outcomes_and_profile_identity() -> None:
    locator = SourceLocator(sheet="CDR", row=2)
    incomplete = SignalValidationResult.incomplete(
        profile=CDR_GENERIC_V1,
        source_locator=locator,
        reason_codes=("missing_callee",),
        explanation="counterparty is unavailable",
    )
    rejected = SignalValidationResult.rejected(
        profile=CDR_GENERIC_V1,
        source_locator=locator,
        reason_codes=("malformed_duration",),
        explanation="duration is invalid",
    )
    assert incomplete.outcome is SignalValidationOutcome.INCOMPLETE
    assert rejected.outcome is SignalValidationOutcome.REJECTED
    assert rejected.extractor_config_hash
    assert "caller-A" not in str(rejected)


def test_ocr_page_result_rejects_invalid_span_geometry_and_attaches_safe_extractor_metadata() -> (
    None
):
    bbox = BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.2, y_max=0.2)
    region = OcrRegion(text="Phone: 9876543210", confidence=0.8, bbox=bbox, word_count=2)
    with pytest.raises(ValueError, match="regions and region_spans"):
        OcrPageResult(
            page=1,
            regions=(region,),
            joined_text=region.text,
            region_spans=(),
            engine_version="1.0.0",
            tesseract_version="5.0",
            average_confidence=0.8,
            config_hash="synthetic-config",
        )
    result = OcrPageResult(
        page=1,
        regions=(region,),
        joined_text=region.text,
        region_spans=((0, len(region.text)),),
        engine_version="1.0.0",
        tesseract_version="5.0",
        average_confidence=0.8,
        config_hash="synthetic-config",
    )
    extracted = _extract_page_mentions(
        result.joined_text, 1, DeterministicNerAdapter(), result
    ).mentions
    phone = next(item for item in extracted if item.observation_type == "phone_number_mention")
    assert phone.locator.page == 1
    assert phone.locator.span_start is not None and phone.locator.span_end is not None
    assert phone.locator.bbox_xyxy_normalized == bbox
    assert phone.attributes["ocr_extractor"] == {
        "name": "structured_document_page_ocr_v1",
        "version": "1.0.0",
        "model_version": "5.0",
        "config_hash": "synthetic-config",
    }
    assert result.joined_text not in str(phone.attributes)
