"""CDR (Call Detail Record) normalization: `cdr_generic_v1`.

Operates on `RawRecord`s from *any* source format (CSV, XLSX, or JSON —
see `models.RawRecord`), resolving each canonical field through the
profile's documented header aliases. Participant values stay source-local:
they are whitespace-trimmed but no country code, name, or counterparty is
invented. Timestamps are parsed only against a documented, fixed set of
formats. No `EventV1`, graph relationship, or person identity is created
here — only `ObservationV1`-ready mentions.

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

import math
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
from app.modules.structured_processing.signal_validation import SignalValidationResult
from app.modules.structured_processing.structured.profiles import CDR_GENERIC_V1

# Accepted CDR timestamp formats, tried in this order. A value matching
# none of these is a documented, safe extraction limit, not a guess — see
# docs/architecture/document-and-structured-processing-v1.md.
#
# `%Y-%m-%dT%H:%M:%S%z` (Gap-Closure follow-up) accepts trailing-Z ISO 8601
# (`2032-01-01T00:10:00Z`) alongside a numeric offset (`+05:30`/`+0530`) --
# Python's `%z` directive has parsed a literal `Z` as UTC since 3.7. Still a
# fixed, documented list, just a longer one -- an unmatched string is still
# rejected, never guessed.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
)

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


def _participant_value(record: RawRecord, canonical: str) -> tuple[str, str]:
    """Return `(raw, trimmed)` for a required source-local participant.

    A non-empty printable value is a valid opaque identifier.  This is
    intentionally not country-code reconciliation: `9876543210` and
    `+919876543210` remain differently sourced values unless another phase
    explicitly establishes a reconciliation policy.
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


def _parse_timestamp(raw: str, tzinfo: ZoneInfo | timezone) -> tuple[datetime, bool] | None:
    """Returns `(datetime, self_describing)`, or `None` if no format matches.

    `self_describing=True` only for a format carrying its own explicit
    offset (currently just `%z`, e.g. a trailing `Z` or `+05:30`) -- that
    parsed offset wins outright, never reinterpreted against `tzinfo`
    (resolved from `source_timezone`/the configured default), since the
    raw string itself already said what zone it's in. Every other,
    naive-format match applies `tzinfo` exactly as before.
    """
    stripped = raw.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed, True
        return parsed.replace(tzinfo=tzinfo), False
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
    result = _parse_timestamp(raw_timestamp, tzinfo)
    if result is None:
        return None
    parsed, self_describing = result
    if self_describing:
        # The raw timestamp carried its own explicit offset (e.g. a
        # trailing "Z"), which _parse_timestamp already honored over the
        # separately-resolved source_timezone/default -- reflect the zone
        # actually used, not the one that was resolved but never applied.
        resolved_tz_name = "UTC" if parsed.utcoffset() == timedelta(0) else parsed.strftime("%z")
    utc_offset = parsed.strftime("%z") or ""
    return parsed.astimezone(UTC), resolved_tz_name, utc_offset


def normalize_cdr_records(records: list[RawRecord]) -> list[RawMention]:
    """Normalize CDR records into `cdr_*` mentions.

    Raises a safe, deterministic validation error for a record without both
    participants, a usable timestamp, a malformed duration, or an impossible
    source-supplied time range.  Invalid records yield no observations.
    """
    mentions: list[RawMention] = []

    for record in records:
        caller_raw, caller = _participant_value(record, "caller_number")
        callee_raw, callee = _participant_value(record, "callee_number")
        timestamp_field = _resolve_alias(record, "timestamp")

        if timestamp_field is None or not timestamp_field[1].strip():
            raise ProcessingError(
                ErrorCode.REQUIRED_FIELD_MISSING,
                f"record {record.index} is missing required field 'timestamp'",
            )
        assert timestamp_field is not None

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
            "caller_number_raw": caller_raw,
            "caller_number": caller,
            "callee_number_raw": callee_raw,
            "callee_number": callee,
            "participants": [
                {"role": "caller", "identifier": caller},
                {"role": "callee", "identifier": callee},
            ],
            "timestamp": utc_timestamp.isoformat(),
            "timestamp_raw": timestamp_field[1],
            "timestamp_source_timezone": resolved_tz_name,
            "timestamp_source_utc_offset": utc_offset or None,
        }

        end_timestamp_field = _resolve_alias(record, "end_timestamp")
        if end_timestamp_field is not None and end_timestamp_field[1].strip():
            parsed_end = parse_record_timestamp(
                end_timestamp_field[1], timezone_field[1] if timezone_field else None
            )
            if parsed_end is None:
                raise ProcessingError(
                    ErrorCode.INVALID_SOURCE_SIGNAL,
                    f"record {record.index} end_timestamp does not match a documented format",
                )
            utc_end, _, _ = parsed_end
            if utc_end < utc_timestamp:
                raise ProcessingError(
                    ErrorCode.INVALID_SOURCE_SIGNAL,
                    f"record {record.index} has an impossible call time range",
                )
            record_attrs["timestamp_end"] = utc_end.isoformat()

        call_type_field = _resolve_alias(record, "call_type")
        if call_type_field is not None and call_type_field[1].strip():
            record_attrs["call_type"] = call_type_field[1].strip()

        call_id_field = _resolve_alias(record, "call_id")
        if call_id_field is not None and call_id_field[1].strip():
            record_attrs["call_id"] = call_id_field[1].strip()

        tower_field = _resolve_alias(record, "cell_tower_id")
        if tower_field is not None:
            record_attrs["cell_tower_id"] = tower_field[1]

        duration_field = _resolve_alias(record, "duration_seconds")
        if duration_field is not None:
            record_attrs["duration_seconds_raw"] = duration_field[1]
            try:
                duration = float(duration_field[1])
            except ValueError as exc:
                raise ProcessingError(
                    ErrorCode.INVALID_SOURCE_SIGNAL,
                    f"record {record.index} duration_seconds is malformed",
                ) from exc
            if not math.isfinite(duration) or duration < 0:
                raise ProcessingError(
                    ErrorCode.INVALID_SOURCE_SIGNAL,
                    f"record {record.index} duration_seconds is invalid",
                )
            record_attrs["duration_seconds"] = duration

        record_attrs["source_signal_quality"] = SignalValidationResult.accepted(
            profile=CDR_GENERIC_V1, source_locator=record.locator_for(None)
        ).attribute_value()

        mentions.append(
            RawMention(
                observation_type="cdr_call_record",
                text=f"cdr_call_record row {record.index}",
                locator=record.locator_for(None),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes=record_attrs,
                event_time=utc_timestamp,
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
