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
`Settings.structured_default_timezone`); correlation-ready transfer records
require a documented, parseable timestamp and both source-backed parties.
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
from app.modules.structured_processing.signal_validation import SignalValidationResult
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

_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


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
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _required_party(record: RawRecord, canonical: str) -> tuple[str, str]:
    """Return `(raw, trimmed)` for a required source-backed transfer party.

    Values are intentionally opaque; validation only rejects missing, blank,
    and control-character-bearing values.  It never swaps parties or infers
    who owns an account.
    """
    resolved = _resolve_alias(record, canonical)
    if resolved is None:
        raise ProcessingError(
            ErrorCode.REQUIRED_FIELD_MISSING,
            f"record {record.index} is missing required field '{canonical}'",
        )
    raw = resolved[1]
    value = raw.strip()
    if not value:
        raise ProcessingError(
            ErrorCode.REQUIRED_FIELD_MISSING,
            f"record {record.index} required field '{canonical}' is blank",
        )
    if any(ord(char) < 32 for char in value):
        raise ProcessingError(
            ErrorCode.INVALID_SOURCE_SIGNAL,
            f"record {record.index} required field '{canonical}' is malformed",
        )
    return raw, value


def _safe_optional_identifier(raw: str) -> str | None:
    """Preserve an identifier-shaped optional reference, never narration text."""
    value = raw.strip()
    return value if _SAFE_REFERENCE.fullmatch(value) else None


def _safe_optional_channel(raw: str) -> str | None:
    """Keep a short source-supplied channel label, never free-form narration."""
    value = raw.strip()
    if not value or len(value) > 64 or any(ord(char) < 32 for char in value):
        return None
    return value


def normalize_financial_records(records: list[RawRecord]) -> list[RawMention]:
    """Normalize financial transaction records into `financial_*`/`amount_mention` mentions.

    Raises a safe, deterministic validation error for a record without both
    transfer parties, amount/currency/timestamp, a usable amount/currency, or
    an invalid timestamp. Invalid records yield no observations.
    """
    mentions: list[RawMention] = []

    for record in records:
        sender_raw, sender = _required_party(record, "sender_account")
        receiver_raw, receiver = _required_party(record, "receiver_account")
        amount_field = _resolve_alias(record, "amount")
        currency_field = _resolve_alias(record, "currency")
        timestamp_field = _resolve_alias(record, "timestamp")

        missing = [
            name
            for name, field in (
                ("amount", amount_field),
                ("currency", currency_field),
                ("timestamp", timestamp_field),
            )
            if field is None
        ]
        if missing:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} is missing required field(s): {', '.join(missing)}",
            )
        assert (
            amount_field is not None and currency_field is not None and timestamp_field is not None
        )

        normalized_amount = _normalize_amount(amount_field[1])
        if normalized_amount is None:
            raise ProcessingError(
                ErrorCode.INVALID_SOURCE_SIGNAL,
                f"record {record.index} amount is not a parseable decimal value",
            )
        if normalized_amount < 0:
            raise ProcessingError(
                ErrorCode.INVALID_SOURCE_SIGNAL,
                f"record {record.index} amount must not be negative for a transfer event",
            )

        currency_raw = currency_field[1].strip()
        if not currency_raw:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING, f"record {record.index} currency is empty"
            )
        currency = currency_raw.upper()
        if not _ISO4217_SHAPE.fullmatch(currency):
            raise ProcessingError(
                ErrorCode.INVALID_SOURCE_SIGNAL,
                f"record {record.index} currency is malformed",
            )

        timezone_field = _resolve_alias(record, "source_timezone")
        parsed = parse_record_timestamp(
            timestamp_field[1], timezone_field[1] if timezone_field else None
        )
        if parsed is None:
            raise ProcessingError(
                ErrorCode.INVALID_SOURCE_SIGNAL,
                f"record {record.index} timestamp does not match a documented format",
            )
        utc_timestamp, resolved_tz_name, utc_offset = parsed

        record_attrs: dict[str, JsonValue] = {
            "amount": str(normalized_amount),  # exact decimal string, never a float
            "amount_raw": amount_field[1],
            "currency": currency,
            "currency_raw": currency_raw,
            "currency_is_known_iso4217": bool(_ISO4217_SHAPE.match(currency))
            and currency in _COMMON_ISO4217_CODES,
            "sender_account_raw": sender_raw,
            "sender_account": sender,
            "receiver_account_raw": receiver_raw,
            "receiver_account": receiver,
            "participants": [
                {"role": "sender", "identifier": sender},
                {"role": "receiver", "identifier": receiver},
            ],
            "timestamp": utc_timestamp.isoformat(),
            "timestamp_raw": timestamp_field[1],
            "timestamp_source_timezone": resolved_tz_name,
            "timestamp_source_utc_offset": utc_offset or None,
        }

        direction_field = _resolve_alias(record, "direction")
        if direction_field is not None:
            record_attrs["direction_raw"] = direction_field[1]
            normalized_direction = _normalize_direction(direction_field[1])
            if normalized_direction is not None:
                record_attrs["direction"] = normalized_direction

        for canonical in ("transaction_id", "reference"):
            field = _resolve_alias(record, canonical)
            if field is not None:
                safe_identifier = _safe_optional_identifier(field[1])
                if safe_identifier is not None:
                    record_attrs[canonical] = safe_identifier
                elif field[1].strip():
                    # `reference` aliases include narration/description in
                    # legacy exports. Do not copy arbitrary source content
                    # into graph-facing attributes.
                    record_attrs[f"{canonical}_unusable"] = True

        channel_field = _resolve_alias(record, "channel")
        if channel_field is not None:
            channel = _safe_optional_channel(channel_field[1])
            if channel is not None:
                record_attrs["channel"] = channel
            elif channel_field[1].strip():
                record_attrs["channel_unusable"] = True

        record_attrs["source_signal_quality"] = SignalValidationResult.accepted(
            profile=FINANCIAL_TRANSACTION_GENERIC_V1, source_locator=record.locator_for(None)
        ).attribute_value()

        mentions.append(
            RawMention(
                observation_type="financial_transaction_record",
                text=f"financial_transaction_record row {record.index}",
                locator=record.locator_for(None),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes=record_attrs,
                event_time=utc_timestamp,
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
            header, _ = field
            value = sender if canonical == "sender_account" else receiver
            mentions.append(
                RawMention(
                    observation_type=observation_type,
                    text=value,
                    locator=record.locator_for(header),
                    confidence=CONFIDENCE_NORMALIZATION_CONSERVATIVE,
                    entity_type_hint="account",
                    attributes={
                        "account_role": canonical,
                        "participant_role": "sender"
                        if canonical == "sender_account"
                        else "receiver",
                    },
                )
            )

        reference_field = _resolve_alias(record, "transaction_id") or _resolve_alias(
            record, "reference"
        )
        if reference_field is not None:
            header, raw_value = reference_field
            safe_reference = _safe_optional_identifier(raw_value)
            if safe_reference is None:
                continue
            mentions.append(
                RawMention(
                    observation_type="transaction_reference_mention",
                    text=safe_reference,
                    locator=record.locator_for(header),
                    confidence=CONFIDENCE_NORMALIZATION_CONSERVATIVE,
                    entity_type_hint="transaction_reference",
                    attributes={},
                )
            )

    return mentions
