"""CDR (Call Detail Record) normalization: `cdr_generic_v1`.

Operates on `RawRecord`s from *any* source format (CSV, XLSX, or JSON —
see `models.RawRecord`), resolving each canonical field through the
profile's documented header aliases. Phone numbers are normalized only
when the result is unambiguous; timestamps are parsed only against a
documented, fixed set of formats. No `EventV1`, graph relationship, or
person identity is created here — only `ObservationV1`-ready mentions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import JsonValue

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.models import RawMention, RawRecord, normalize_header
from app.modules.structured_processing.provenance import (
    CONFIDENCE_NORMALIZATION_CONSERVATIVE,
    CONFIDENCE_STRUCTURED_COMPLETE,
)
from app.modules.structured_processing.structured.profiles import CDR_GENERIC_V1

# Accepted CDR timestamp formats, tried in this order. Naive values are
# interpreted as UTC (documented; CDR exports rarely carry timezone info).
# A value matching none of these is a documented, safe extraction limit,
# not a guess — see docs/architecture/document-and-structured-processing-v1.md.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)

_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")


def _resolve_alias(record: RawRecord, canonical: str) -> tuple[str, str] | None:
    """Find the (original header, value) for the first matching alias, if any."""
    normalized_map = {
        normalize_header(header): (header, value) for header, value in record.values.items()
    }
    for alias in CDR_GENERIC_V1.field_aliases.get(canonical, ()):
        if alias in normalized_map:
            return normalized_map[alias]
    return None


def _normalize_phone(raw: str) -> str:
    """Strip separators and a country/trunk prefix only if the result is an
    unambiguous 10-digit Indian mobile number; otherwise keep the original."""
    stripped = re.sub(r"[\s\-()]", "", raw)
    candidate = stripped
    if candidate.startswith("+91"):
        candidate = candidate[3:]
    elif candidate.startswith("0") and len(candidate) == 11:
        candidate = candidate[1:]
    return candidate if _INDIAN_MOBILE.fullmatch(candidate) else raw


def _parse_timestamp(raw: str) -> datetime | None:
    stripped = raw.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(stripped, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def normalize_cdr_records(records: list[RawRecord]) -> list[RawMention]:
    """Normalize CDR records into `cdr_*` mentions.

    Raises `required_field_missing` for any record lacking `caller_number`
    or `timestamp`, or whose timestamp doesn't match a documented format.
    """
    mentions: list[RawMention] = []

    for record in records:
        caller = _resolve_alias(record, "caller_number")
        timestamp_field = _resolve_alias(record, "timestamp")

        missing = [
            name
            for name, field in (("caller_number", caller), ("timestamp", timestamp_field))
            if field is None
        ]
        if missing:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} is missing required field(s): {', '.join(missing)}",
            )
        assert caller is not None and timestamp_field is not None  # narrowed by the check above

        parsed_ts = _parse_timestamp(timestamp_field[1])
        if parsed_ts is None:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} timestamp does not match a documented format",
            )

        record_attrs: dict[str, JsonValue] = {
            "caller_number": _normalize_phone(caller[1]),
            "timestamp": parsed_ts.isoformat(),
            "timestamp_raw": timestamp_field[1],
        }

        callee = _resolve_alias(record, "callee_number")
        if callee is not None:
            record_attrs["callee_number"] = _normalize_phone(callee[1])

        call_type_field = _resolve_alias(record, "call_type")
        if call_type_field is not None:
            record_attrs["call_type"] = call_type_field[1]

        duration_field = _resolve_alias(record, "duration_seconds")
        if duration_field is not None:
            record_attrs["duration_seconds_raw"] = duration_field[1]
            try:
                duration = float(duration_field[1])
                if duration >= 0:
                    record_attrs["duration_seconds"] = duration
            except ValueError:
                pass  # not a valid number: keep only the raw value above

        mentions.append(
            RawMention(
                observation_type="cdr_call_record",
                text=f"cdr_call_record row {record.index}",
                locator=record.locator_for(None),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes=record_attrs,
            )
        )

        for canonical, observation_type, entity_type_hint in (
            ("imei", "cdr_device_identifier_mention", "imei"),
            ("imsi", "cdr_subscriber_identifier_mention", "imsi"),
            ("cell_tower_id", "cdr_tower_mention", "cell_tower_id"),
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
                    entity_type_hint=entity_type_hint,
                    attributes={"identifier_kind": canonical},
                )
            )

    return mentions
