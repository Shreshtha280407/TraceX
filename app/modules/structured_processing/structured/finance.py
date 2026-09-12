"""Financial transaction normalization: `financial_transaction_generic_v1`.

Money is never passed through `float`. `_normalize_amount` uses
`decimal.Decimal` for exact base-10 arithmetic-safe representation, and
the *original* source string is always preserved alongside the normalized
value (`amount_raw` next to `amount`). Currency is never invented: a
record without an explicit currency is rejected via `required_field_missing`
rather than guessed; a well-formed but unrecognized ISO 4217-shaped code is
still accepted (`currency_is_known_iso4217=False`), never rejected outright
— this module validates *format*, it does not maintain the authoritative
currency-code registry. No account-owner identity linkage or
criminal-network inference happens here — only `ObservationV1`-ready
mentions. Timestamp timezone resolution reuses `cdr.resolve_timezone`'s
identical policy (explicit `source_timezone` field, else
`Settings.structured_default_timezone`) — `timestamp` is optional for this
profile (only `amount`/`currency` are required), so an unparseable
timestamp is preserved raw rather than rejecting the whole record.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import JsonValue

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.models import RawMention, RawRecord, normalize_header
from app.modules.structured_processing.provenance import (
    CONFIDENCE_NORMALIZATION_CONSERVATIVE,
    CONFIDENCE_STRUCTURED_COMPLETE,
)
from app.modules.structured_processing.structured.cdr import parse_record_timestamp
from app.modules.structured_processing.structured.profiles import FINANCIAL_TRANSACTION_GENERIC_V1

_CURRENCY_PREFIX = re.compile(r"^(?:₹|\$|Rs\.?|INR|USD)\s*", re.IGNORECASE)
_ISO4217_SHAPE = re.compile(r"^[A-Z]{3}$")

#: A curated, intentionally non-exhaustive set of common ISO 4217 codes —
#: used only to set a safe, informational `currency_is_known_iso4217` flag,
#: never to reject a well-formed but unlisted code (see module docstring).
_COMMON_ISO4217_CODES = frozenset(
    {
        "INR",
        "USD",
        "EUR",
        "GBP",
        "AED",
        "SGD",
        "JPY",
        "CNY",
        "AUD",
        "CAD",
        "CHF",
        "HKD",
        "SAR",
        "QAR",
        "KWD",
        "THB",
        "MYR",
        "IDR",
        "NPR",
        "LKR",
        "BDT",
        "PKR",
        "ZAR",
        "NZD",
        "SEK",
        "NOK",
        "DKK",
        "RUB",
        "BRL",
        "MXN",
    }
)

#: Documented, case-insensitive debit/credit vocabulary. A raw value
#: outside this set is preserved only as `direction_raw` — never guessed.
_DIRECTION_MAP: dict[str, str] = {
    "debit": "debit",
    "dr": "debit",
    "d": "debit",
    "credit": "credit",
    "cr": "credit",
    "c": "credit",
}


def _normalize_direction(raw: str) -> str | None:
    return _DIRECTION_MAP.get(raw.strip().lower())


def _resolve_alias(record: RawRecord, canonical: str) -> tuple[str, str] | None:
    normalized_map = {
        normalize_header(header): (header, value) for header, value in record.values.items()
    }
    for alias in FINANCIAL_TRANSACTION_GENERIC_V1.field_aliases.get(canonical, ()):
        if alias in normalized_map:
            return normalized_map[alias]
    return None


def _normalize_amount(raw: str) -> Decimal | None:
    """Parse a monetary string as an exact `Decimal`, or `None` if it can't be.

    A leading currency symbol/code and thousands separators are stripped
    defensively (real-world exports sometimes embed them in the amount
    column despite a separate currency field), but the numeric value
    itself is never rounded or passed through binary floating point.
    """
    cleaned = _CURRENCY_PREFIX.sub("", raw.strip()).replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def normalize_financial_records(records: list[RawRecord]) -> list[RawMention]:
    """Normalize financial transaction records into `financial_*`/`amount_mention` mentions.

    Raises `required_field_missing` for any record lacking `amount` or
    `currency`, or whose amount can't be parsed as a decimal number.
    """
    mentions: list[RawMention] = []

    for record in records:
        amount_field = _resolve_alias(record, "amount")
        currency_field = _resolve_alias(record, "currency")

        missing = [
            name
            for name, field in (("amount", amount_field), ("currency", currency_field))
            if field is None
        ]
        if missing:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} is missing required field(s): {', '.join(missing)}",
            )
        assert amount_field is not None and currency_field is not None

        normalized_amount = _normalize_amount(amount_field[1])
        if normalized_amount is None:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} amount is not a parseable decimal value",
            )

        currency_raw = currency_field[1].strip()
        if not currency_raw:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING, f"record {record.index} currency is empty"
            )
        currency = currency_raw.upper()

        record_attrs: dict[str, JsonValue] = {
            "amount": str(normalized_amount),  # exact decimal string, never a float
            "amount_raw": amount_field[1],
            "currency": currency,
            "currency_raw": currency_raw,
            "currency_is_known_iso4217": bool(_ISO4217_SHAPE.match(currency))
            and currency in _COMMON_ISO4217_CODES,
        }

        timestamp_field = _resolve_alias(record, "timestamp")
        if timestamp_field is not None:
            timezone_field = _resolve_alias(record, "source_timezone")
            parsed = parse_record_timestamp(
                timestamp_field[1], timezone_field[1] if timezone_field else None
            )
            record_attrs["timestamp_raw"] = timestamp_field[1]
            if parsed is not None:
                utc_timestamp, resolved_tz_name, utc_offset = parsed
                record_attrs["timestamp"] = utc_timestamp.isoformat()
                record_attrs["timestamp_source_timezone"] = resolved_tz_name
                record_attrs["timestamp_source_utc_offset"] = utc_offset or None
            # else: timestamp is optional for this profile -- the raw value
            # above is preserved, but no canonical value is fabricated.

        direction_field = _resolve_alias(record, "direction")
        if direction_field is not None:
            record_attrs["direction_raw"] = direction_field[1]
            normalized_direction = _normalize_direction(direction_field[1])
            if normalized_direction is not None:
                record_attrs["direction"] = normalized_direction

        for canonical in (
            "transaction_id",
            "reference",
            "channel",
            "status",
            "balance",
            "counterparty",
        ):
            field = _resolve_alias(record, canonical)
            if field is not None:
                record_attrs[canonical] = field[1]

        mentions.append(
            RawMention(
                observation_type="financial_transaction_record",
                text=f"financial_transaction_record row {record.index}",
                locator=record.locator_for(None),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes=record_attrs,
            )
        )

        mentions.append(
            RawMention(
                observation_type="amount_mention",
                text=amount_field[1],
                locator=record.locator_for(amount_field[0]),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                entity_type_hint="amount",
                attributes={"currency": currency, "amount": str(normalized_amount)},
            )
        )

        for canonical, observation_type in (
            ("sender_account", "financial_account_mention"),
            ("receiver_account", "financial_account_mention"),
        ):
            field = _resolve_alias(record, canonical)
            if field is None:
                continue
            header, value = field
            mentions.append(
                RawMention(
                    observation_type=observation_type,
                    text=value,
                    locator=record.locator_for(header),
                    confidence=CONFIDENCE_NORMALIZATION_CONSERVATIVE,
                    entity_type_hint="account",
                    attributes={"account_role": canonical},
                )
            )

        reference_field = _resolve_alias(record, "transaction_id") or _resolve_alias(
            record, "reference"
        )
        if reference_field is not None:
            header, value = reference_field
            mentions.append(
                RawMention(
                    observation_type="transaction_reference_mention",
                    text=value,
                    locator=record.locator_for(header),
                    confidence=CONFIDENCE_NORMALIZATION_CONSERVATIVE,
                    entity_type_hint="transaction_reference",
                    attributes={},
                )
            )

    return mentions
