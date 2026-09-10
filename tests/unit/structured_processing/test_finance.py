"""Scenario 12: financial profile preserves money exactly, never through float."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured.csv_parser import parse_csv
from app.modules.structured_processing.structured.finance import normalize_financial_records


def test_finance_preserves_amount_as_exact_decimal_string_not_float() -> None:
    # 0.1 + 0.2 style values are exactly where float would introduce error.
    data = b"amount,currency\n1234567.10,INR\n"
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")

    assert record.attributes["amount"] == "1234567.10"
    assert Decimal(str(record.attributes["amount"])) == Decimal("1234567.10")
    assert record.attributes["amount_raw"] == "1234567.10"
    # never actually a float in the observation
    assert not isinstance(record.attributes["amount"], float)


def test_finance_strips_thousands_separators_and_currency_prefix_defensively() -> None:
    data = b'amount,currency\n"Rs. 1,00,000.50",INR\n'
    mentions = normalize_financial_records(parse_csv(data))
    record = next(m for m in mentions if m.observation_type == "financial_transaction_record")
    assert record.attributes["amount"] == "100000.50"
    assert record.attributes["amount_raw"] == "Rs. 1,00,000.50"


def test_finance_requires_explicit_currency() -> None:
    data = b"amount\n500\n"  # no currency column at all
    with pytest.raises(ProcessingError) as exc_info:
        normalize_financial_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING


def test_finance_rejects_unparseable_amount() -> None:
    data = b"amount,currency\nnot-a-number,INR\n"
    with pytest.raises(ProcessingError) as exc_info:
        normalize_financial_records(parse_csv(data))
    assert exc_info.value.code == ErrorCode.REQUIRED_FIELD_MISSING


def test_finance_emits_amount_mention_and_account_mentions() -> None:
    data = (
        b"amount,currency,sender_account,receiver_account,transaction_id\n"
        b"500,INR,111122223333,444455556666,TXN123456\n"
    )
    mentions = normalize_financial_records(parse_csv(data))
    types = [m.observation_type for m in mentions]
    assert "amount_mention" in types
    assert types.count("financial_account_mention") == 2
    assert "transaction_reference_mention" in types


def test_finance_never_infers_shared_account_ownership() -> None:
    """No entity/identity linkage is ever produced by this module."""
    data = b"amount,currency,sender_account,receiver_account\n500,INR,111,222\n"
    mentions = normalize_financial_records(parse_csv(data))
    for mention in mentions:
        assert mention.observation_type in {
            "financial_transaction_record",
            "amount_mention",
            "financial_account_mention",
        }
