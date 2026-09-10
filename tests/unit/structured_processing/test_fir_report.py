"""Deterministic FIR/report field extraction: every documented mention type."""

from __future__ import annotations

from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.models import TextSegment


def _mentions_of(text: str, observation_type: str) -> list[str]:
    segments = [TextSegment(page=1, text=text)]
    return [
        m.text for m in extract_fir_mentions(segments) if m.observation_type == observation_type
    ]


def test_fir_reference_requires_explicit_label() -> None:
    assert _mentions_of("FIR No: 45/2026 filed today", "fir_reference") == ["45/2026"]
    assert _mentions_of("case reference 45/2026 without a label", "fir_reference") == []


def test_police_station_requires_explicit_label() -> None:
    assert _mentions_of("Police Station: MG Road", "police_station_mention") == ["MG Road"]


def test_phone_number_indian_mobile() -> None:
    assert _mentions_of("Contact: 9876543210", "phone_number_mention") == ["9876543210"]
    assert _mentions_of("Contact: +91-9876543210", "phone_number_mention") == ["9876543210"]


def test_email_address() -> None:
    assert _mentions_of("Email: victim@example.com", "email_address_mention") == [
        "victim@example.com"
    ]


def test_vehicle_identifier() -> None:
    assert _mentions_of("Vehicle KA01AB1234 seen fleeing", "vehicle_identifier_mention") == [
        "KA01AB1234"
    ]


def test_financial_identifier_upi_id() -> None:
    assert _mentions_of("Paid via 9876543210@ybl", "financial_identifier_mention") == [
        "9876543210@ybl"
    ]


def test_financial_identifier_account_number_requires_label() -> None:
    assert _mentions_of("A/C No: 123456789012", "financial_identifier_mention") == ["123456789012"]
    assert (
        _mentions_of("random number 123456789012 with no label", "financial_identifier_mention")
        == []
    )


def test_financial_identifier_transaction_reference() -> None:
    assert _mentions_of("UTR: ABCDEF1234", "financial_identifier_mention") == ["ABCDEF1234"]


def test_amount_requires_currency_context() -> None:
    assert _mentions_of("Amount involved: Rs. 50,000", "amount_mention") == ["50,000"]
    assert _mentions_of("50000 rupees with no symbol", "amount_mention") == []


def test_legal_section_requires_explicit_label() -> None:
    assert _mentions_of("charged under Section 420 IPC", "legal_section_mention") == ["420 IPC"]


def test_date_time_dmy_and_iso_formats() -> None:
    assert _mentions_of("filed on 05/01/2026", "date_time_mention") == ["05/01/2026"]
    assert _mentions_of("filed on 2026-01-05", "date_time_mention") == ["2026-01-05"]


def test_every_mention_has_confidence_0_95() -> None:
    segments = [
        TextSegment(
            page=1,
            text="FIR No: 1/2026, Police Station: X, phone 9876543210, email a@b.com, "
            "vehicle KA01AB1234, Rs. 100, Section 1 IPC",
        )
    ]
    mentions = extract_fir_mentions(segments)
    assert mentions
    assert all(m.confidence == 0.95 for m in mentions)


def test_no_mentions_extracted_from_text_with_no_patterns() -> None:
    segments = [TextSegment(page=1, text="This is a plain sentence with nothing extractable.")]
    assert extract_fir_mentions(segments) == []
