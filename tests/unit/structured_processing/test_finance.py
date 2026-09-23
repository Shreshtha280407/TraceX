"""Scenario 12: financial profile preserves money exactly, never through float."""

# ruff: noqa: E501 -- inline synthetic CSV fixtures are deliberately readable as rows.

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured.csv_parser import parse_csv
from app.modules.structured_processing.structured.finance import normalize_financial_records


def test_finance_preserves_amount_as_exact_decimal_string_not_float() -> None:
    # 0.1 + 0.2 style values are exactly where float would introduce error.
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,1234567.10,INR,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")

    assert record.attributes["amount"] == "1234567.10"
    assert Decimal(str(record.attributes["amount"])) == Decimal("1234567.10")
    assert record.attributes["amount_raw"] == "1234567.10"
    # never actually a float in the observation
    assert not isinstance(record.attributes["amount"], float)


def test_finance_strips_thousands_separators_and_currency_prefix_defensively() -> None:
    data = b'sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,"Rs. 1,00,000.50",INR,2026-01-01 10:00:00\n'
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["amount"] == "100000.50"
    assert record.attributes["amount_raw"] == "Rs. 1,00,000.50"


def test_finance_requires_explicit_currency() -> None:
    data = b"sender_account,receiver_account,amount,timestamp\nSENDER,RECEIVER,500,2026-01-01 10:00:00\n"  # no currency column at all
    with pytest.raises(ProcessingError) as exc_info:
        normalize_financial_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING


def test_finance_rejects_unparseable_amount() -> None:
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,not-a-number,INR,2026-01-01 10:00:00\n"
    with pytest.raises(ProcessingError) as exc_info:
        normalize_financial_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.INVALID_SOURCE_SIGNAL


def test_finance_emits_amount_mention_and_account_mentions() -> None:
    data = (
        b"amount,currency,sender_account,receiver_account,transaction_id,timestamp\n"
        b"500,INR,111122223333,444455556666,TXN123456,2026-01-01 10:00:00\n"
    )
    mentions = normalize_financial_records(parse_csv(data))
    types = [m.observation_type for m in mentions]
    assert "amount_mention" in types
    assert types.count("financial_account_mention") == 2
    assert "transaction_reference_mention" in types


def test_finance_transaction_record_retains_source_backed_instruments() -> None:
    data = b"amount,currency,sender_account,receiver_account,timestamp\n500,INR,SYNTH-SOURCE,SYNTH-DEST,2026-01-01 10:00:00\n"
    record = next(
        mention
        for mention in normalize_financial_records(parse_csv(data))
        if mention.observation_type == "financial_transaction_record"
    )

    assert record.attributes["sender_account"] == "SYNTH-SOURCE"
    assert record.attributes["receiver_account"] == "SYNTH-DEST"


def test_finance_never_infers_shared_account_ownership() -> None:
    """No entity/identity linkage is ever produced by this module."""
    data = b"amount,currency,sender_account,receiver_account,timestamp\n500,INR,111,222,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    for mention in mentions:
        assert mention.observation_type in {
            "financial_transaction_record",
            "amount_mention",
            "financial_account_mention",
        }


def test_finance_flags_a_known_iso4217_code() -> None:
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,500,inr,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["currency"] == "INR"
    assert record.attributes["currency_is_known_iso4217"] is True


def test_finance_accepts_an_unrecognized_but_well_formed_currency_code() -> None:
    """Never rejects a well-formed code just because it's outside the curated set."""
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,500,ZZZ,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["currency"] == "ZZZ"
    assert record.attributes["currency_is_known_iso4217"] is False


def test_finance_normalizes_debit_credit_direction() -> None:
    data = b"sender_account,receiver_account,amount,currency,direction,timestamp\nSENDER,RECEIVER,500,INR,DR,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["direction"] == "debit"
    assert record.attributes["direction_raw"] == "DR"


def test_finance_preserves_unrecognized_direction_raw_only() -> None:
    data = b"sender_account,receiver_account,amount,currency,direction,timestamp\nSENDER,RECEIVER,500,INR,unclear,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert "direction" not in record.attributes
    assert record.attributes["direction_raw"] == "unclear"


def test_finance_timestamp_uses_the_configured_default_timezone_when_none_is_given() -> None:
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,500,INR,2026-01-01 10:00:00\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["timestamp"] == "2026-01-01T04:30:00+00:00"
    assert record.attributes["timestamp_source_timezone"] == "Asia/Kolkata"


def test_finance_timestamp_accepts_trailing_z_iso8601() -> None:
    """Gap-Closure follow-up: mirrors the CDR regression test -- a trailing
    `Z` ISO 8601 timestamp is self-describing UTC and wins outright over
    the configured default timezone."""
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,500,INR,2032-01-01T00:10:00Z\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["timestamp"] == "2032-01-01T00:10:00+00:00"
    assert record.attributes["timestamp_source_timezone"] == "UTC"


def test_finance_rejects_unparseable_timestamp() -> None:
    data = b"sender_account,receiver_account,amount,currency,timestamp\nSENDER,RECEIVER,500,INR,not-a-real-date\n"
    with pytest.raises(ProcessingError) as exc_info:
        normalize_financial_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.INVALID_SOURCE_SIGNAL
