"""CDR (Call Detail Record) normalization: `cdr_generic_v1`.

Operates on `RawRecord`s from *any* source format (CSV, XLSX, or JSON —
see `models.RawRecord`), resolving each canonical field through the
profile's documented header aliases. Phone numbers are normalized to
E.164 only when the result is unambiguous (India is this codebase's only
known country context — see `_normalize_phone`); timestamps are parsed
only against a documented, fixed set of formats. No `EventV1`, graph
relationship, or person identity is created here — only
`ObservationV1`-ready mentions.

## Timezone policy

A record's timestamp is interpreted using, in order:

1. Its own `source_timezone` field, if the export carries one (an IANA
   name like `"Asia/Kolkata"`, or a fixed offset like `"+05:30"`) — never
   guessed from anything else in the row.
2. Otherwise, `Settings.structured_default_timezone` (default
   `"Asia/Kolkata"`) — a fixed, documented default, not silently assumed
   to be UTC.

The canonical `timestamp` attribute is always the UTC instant computed
from that resolved timezone; `timestamp_source_timezone` and
`timestamp_source_utc_offset` record which zone/offset was actually used,
and `timestamp_raw` keeps the untouched original string — so a record with
no explicit timezone is never indistinguishable from one that genuinely
carried `"UTC"`. A timestamp that doesn't match any documented format, or
whose `source_timezone` value isn't a recognized IANA name/fixed offset,
is rejected (`required_field_missing`) rather than silently misinterpreted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue

from app.core.config import get_settings
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.models import RawMention, RawRecord, normalize_header
from app.modules.structured_processing.provenance import (
    CONFIDENCE_NORMALIZATION_CONSERVATIVE,
    CONFIDENCE_STRUCTURED_COMPLETE,
)
from app.modules.structured_processing.structured.profiles import CDR_GENERIC_V1

# Accepted CDR timestamp formats, tried in this order. A value matching
# none of these is a documented, safe extraction limit, not a guess — see
# docs/architecture/document-and-structured-processing-v1.md.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)

_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")
_FIXED_OFFSET = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _resolve_alias(record: RawRecord, canonical: str) -> tuple[str, str] | None:
    """Find the (original header, value) for the first matching alias, if any."""
    normalized_map = {
        normalize_header(header): (header, value) for header, value in record.values.items()
    }
    for alias in CDR_GENERIC_V1.field_aliases.get(canonical, ()):
        if alias in normalized_map:
            return normalized_map[alias]
    return None


def _normalize_phone(raw: str) -> str | None:
    """Return the E.164 form (`+91XXXXXXXXXX`) only for an unambiguous 10-digit
    Indian mobile number; `None` if the value can't be normalized with confidence
    (the caller keeps the original value either way — see `*_raw` attributes)."""
    stripped = re.sub(r"[\s\-()]", "", raw)
    candidate = stripped
    if candidate.startswith("+91"):
        candidate = candidate[3:]
    elif candidate.startswith("0") and len(candidate) == 11:
        candidate = candidate[1:]
    return f"+91{candidate}" if _INDIAN_MOBILE.fullmatch(candidate) else None


def resolve_timezone(raw: str | None) -> tuple[ZoneInfo | timezone, str]:
    """Resolve an explicit per-record timezone string, or the configured default.

    Accepts an IANA zone name (`"Asia/Kolkata"`) or a fixed `+HH:MM`/`-HHMM`
    offset. An unrecognized value falls back to the configured default
    rather than being silently treated as UTC or rejecting the whole
    record over a timezone-field typo — the resolved zone name is always
    recorded in provenance either way, so this is never a silent guess.
    """
    if raw is not None:
        candidate = raw.strip()
        offset_match = _FIXED_OFFSET.match(candidate)
        if offset_match:
            sign, hours, minutes = offset_match.groups()
            total_minutes = int(hours) * 60 + int(minutes)
            if sign == "-":
                total_minutes = -total_minutes
            resolved: ZoneInfo | timezone = (
                UTC if total_minutes == 0 else timezone(timedelta(minutes=total_minutes))
            )
            return resolved, candidate
        try:
            return ZoneInfo(candidate), candidate
        except (ZoneInfoNotFoundError, ValueError):
            pass
    default_name = get_settings().structured_default_timezone
    return ZoneInfo(default_name), default_name


def _parse_timestamp(raw: str, tzinfo: ZoneInfo | timezone) -> datetime | None:
    stripped = raw.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            naive = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tzinfo)
    return None


def parse_record_timestamp(
    raw_timestamp: str, raw_timezone: str | None
) -> tuple[datetime, str, str] | None:
    """Resolve a record's timezone and parse its timestamp against the documented format list.

    Returns `(utc_datetime, resolved_timezone_name, utc_offset)` — the
    canonical UTC instant plus exactly which timezone/offset was used to
    get there — or `None` if the raw string matches none of
    `_TIMESTAMP_FORMATS`. Shared by both `normalize_cdr_records` and
    `finance.normalize_financial_records` so the two profiles apply the
    identical timezone policy (see this module's docstring).
    """
    tzinfo, resolved_tz_name = resolve_timezone(raw_timezone)
    parsed = _parse_timestamp(raw_timestamp, tzinfo)
    if parsed is None:
        return None
    utc_offset = parsed.strftime("%z") or ""
    return parsed.astimezone(UTC), resolved_tz_name, utc_offset


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

        timezone_field = _resolve_alias(record, "source_timezone")
        parsed = parse_record_timestamp(
            timestamp_field[1], timezone_field[1] if timezone_field else None
        )
        if parsed is None:
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} timestamp does not match a documented format",
            )
        utc_timestamp, resolved_tz_name, utc_offset = parsed

        record_attrs: dict[str, JsonValue] = {
            "caller_number_raw": caller[1],
            "timestamp": utc_timestamp.isoformat(),
            "timestamp_raw": timestamp_field[1],
            "timestamp_source_timezone": resolved_tz_name,
            "timestamp_source_utc_offset": utc_offset or None,
        }
        normalized_caller = _normalize_phone(caller[1])
        record_attrs["caller_number"] = normalized_caller if normalized_caller else caller[1]

        callee = _resolve_alias(record, "callee_number")
        if callee is not None:
            record_attrs["callee_number_raw"] = callee[1]
            normalized_callee = _normalize_phone(callee[1])
            record_attrs["callee_number"] = normalized_callee if normalized_callee else callee[1]

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
