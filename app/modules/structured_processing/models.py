"""Shared types for the structured-processing module.

Kept here (rather than duplicated per-format) because the same shapes are
used across document/ and structured/: a `SourceResolver` is how *any*
parser gets bytes without touching MinIO; a `RawRecord` is how CSV, XLSX,
and JSON-array sources all feed the same CDR/financial normalization code
without that code needing to know which file format it came from.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from app.contracts.common import SourceLocator, TimeWindow

_HEADER_SEPARATORS = re.compile(r"[\s\-]+")


def normalize_header(name: str) -> str:
    """Normalize a source header/key for alias matching.

    Lowercases and collapses whitespace/hyphens to a single underscore, so
    a real-world header like `"Caller Number"` or `"caller-number"`
    matches the canonical alias `"caller_number"` without every profile
    having to enumerate every casing/spacing variant explicitly.
    """
    return _HEADER_SEPARATORS.sub("_", name.strip().lower())


@runtime_checkable
class SourceResolver(Protocol):
    """Resolves an evidence object URI to bytes, without any storage credentials.

    A worker never talks to MinIO directly (see `docs/architecture/
    phase-1-decisions.md`); it is handed something that implements this
    protocol instead. Actual MinIO-backed resolution is later-phase
    (evidence-lifecycle integration) work — Phase 1 tests use
    `StaticBytesResolver` or a local-file resolver.
    """

    def read_bytes(self, object_uri: str) -> bytes: ...


@dataclass(frozen=True)
class StaticBytesResolver:
    """A `SourceResolver` that always returns one fixed byte payload.

    The simplest possible testable boundary: tests construct evidence
    fixtures in memory and never need a real object store.
    """

    payload: bytes

    def read_bytes(self, object_uri: str) -> bytes:  # noqa: ARG002 - protocol conformance
        return self.payload


@dataclass(frozen=True)
class TextSegment:
    """A contiguous span of extracted text plus its page, if the source has pages.

    `page` is 1-based and `None` for formats with no page concept (DOCX,
    TXT). Span offsets in every downstream mention are relative to
    `text`, i.e. they restart at 0 for each segment/page — this keeps PDF
    provenance exact per-page without needing a whole-document offset.
    """

    page: int | None
    text: str


@dataclass(frozen=True)
class RawRecord:
    """One structured record (a CSV/XLSX row, or one object in a JSON array).

    `values` maps the *original* header/key text (as it appeared in the
    source, not yet alias-resolved) to its raw string value. `locator_for`
    builds the exact `SourceLocator` for one field of this record — pass
    `None` for a locator covering the whole record (used by
    record-level observations like `cdr_call_record`).
    """

    index: int
    values: Mapping[str, str]
    locator_for: Callable[[str | None], SourceLocator]


@dataclass(frozen=True)
class RawMention:
    """One extracted, unresolved fact, ready to become one `ObservationV1`.

    Shared by FIR/report regex extraction, CDR normalization, and
    financial normalization — every one of these profiles ends with "here
    is a small piece of text/value, here is exactly where it came from,
    here is how confident we are it was read correctly," which is exactly
    this shape.
    """

    observation_type: str
    text: str
    locator: SourceLocator
    confidence: float
    entity_type_hint: str | None = None
    attributes: dict[str, JsonValue] = field(default_factory=dict)
    # Record-level event observations can carry a source-backed time without
    # changing the frozen ObservationV1 contract.  Document mentions leave
    # both unset.
    event_time: datetime | None = None
    time_window: TimeWindow | None = None


@dataclass(frozen=True)
class ParserProfile:
    """Versioned metadata for one explicit parser profile.

    This is metadata only — the extraction logic that reads it lives in
    `document/fir_report.py` (for `fir_report_text_v1`) or `structured/
    {cdr,finance,csv_parser,xlsx_parser,json_parser}.py`. Centralizing the
    metadata in `structured/profiles.py` gives one place to see every
    profile's accepted types, field aliases, and confidence rule at once.
    """

    name: str
    version: str
    description: str
    accepted_content_types: frozenset[str]
    observation_types: tuple[str, ...]
    confidence_rule: str
    field_aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    required_fields: tuple[str, ...] = ()
