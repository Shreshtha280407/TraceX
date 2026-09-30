"""Streaming CSV, NDJSON, JSON-array and XML source adapters with stable locators."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ijson
from defusedxml import ElementTree as SafeElementTree


class SourceParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedRow:
    logical_record: int
    locator_type: str
    locator: str
    value: dict[str, Any]


def _mapping(value: Any, *, locator: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceParseError(f"{locator}: each source record must be an object")
    return value


def _csv_rows(path: Path) -> Iterator[ParsedRow]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SourceParseError("CSV header is required")
        for index, row in enumerate(reader, 1):
            yield ParsedRow(index, "csv_logical_record", f"record:{index}", dict(row))


def _ndjson_rows(path: Path) -> Iterator[ParsedRow]:
    logical_record = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            logical_record += 1
            locator = f"record:{logical_record}"
            try:
                yield ParsedRow(
                    logical_record,
                    "json_pointer",
                    f"/{logical_record - 1}",
                    _mapping(json.loads(line), locator=locator),
                )
            except json.JSONDecodeError as exc:
                raise SourceParseError(f"line:{line_number}: invalid NDJSON: {exc.msg}") from exc


def _json_array_rows(path: Path) -> Iterator[ParsedRow]:
    with path.open("rb") as probe:
        prefix = probe.read(4096).lstrip()
    if not prefix.startswith(b"["):
        raise SourceParseError("JSON import must be a top-level array; use .ndjson for line-delimited JSON")
    with path.open("rb") as handle:
        try:
            for index, value in enumerate(ijson.items(handle, "item")):
                locator = f"/{index}"
                yield ParsedRow(index + 1, "json_pointer", locator, _mapping(value, locator=locator))
        except (ijson.JSONError, UnicodeDecodeError) as exc:
            raise SourceParseError(f"invalid JSON array: {exc}") from exc


def _element_value(element) -> Any:
    if list(element):
        result: dict[str, Any] = dict(element.attrib)
        for child in element:
            key = child.tag.rsplit("}", 1)[-1]
            value = _element_value(child)
            if key in result:
                result[key] = result[key] if isinstance(result[key], list) else [result[key]]
                result[key].append(value)
            else:
                result[key] = value
        return result
    if element.attrib:
        value = dict(element.attrib)
        if element.text and element.text.strip():
            value["value"] = element.text.strip()
        return value
    return (element.text or "").strip()


def _xml_rows(path: Path) -> Iterator[ParsedRow]:
    with path.open("rb") as probe:
        prefix = probe.read(65536).upper()
    if b"<!DOCTYPE" in prefix or b"<!ENTITY" in prefix:
        raise SourceParseError("XML DTDs and entities are forbidden")
    index = 0
    try:
        for _, element in SafeElementTree.iterparse(path, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1].lower()
            if tag not in {"transaction", "record", "row"}:
                continue
            index += 1
            value = _element_value(element)
            yield ParsedRow(index, "xml_element_path", f"/{tag}[{index}]", _mapping(value, locator=f"/{tag}[{index}]"))
            element.clear()
    except SafeElementTree.ParseError as exc:
        raise SourceParseError(f"invalid XML: {exc}") from exc
    if index == 0:
        raise SourceParseError("XML contains no transaction, record, or row elements")


#: Start tags the XML adapter yields as records (see `_xml_rows`): any element
#: whose local name is transaction/record/row, case-insensitively, with an
#: optional namespace prefix. `<records>` is excluded by the lookahead.
_XML_RECORD_START = re.compile(rb"<(?:[A-Za-z_][\w.\-]*:)?(?:transaction|record|row)(?=[\s/>])", re.IGNORECASE)
_XML_SCAN_OVERLAP = 256


def _count_csv(path: Path) -> int:
    """Logical CSV records, matching `csv.DictReader`: a newline inside a quoted
    field does not end a record, and fully blank lines are skipped.

    Quote parity is enough to know whether a physical line ends inside a quoted
    field, because an escaped quote (`""`) adds two and never flips parity.
    """
    records = 0
    in_quotes = False
    record_has_content = False
    header_seen = False
    with path.open("rb") as handle:
        for line in handle:
            if line.count(b'"') % 2:
                in_quotes = not in_quotes
            if line.strip():
                record_has_content = True
            if in_quotes:
                continue
            if record_has_content:
                if header_seen:
                    records += 1
                else:
                    header_seen = True
            record_has_content = False
    if in_quotes and record_has_content and header_seen:
        # Unterminated quote at EOF: the reader still yields that final record.
        records += 1
    return records


def _count_ndjson(path: Path) -> int:
    # Blank lines are skipped by `_ndjson_rows`, so they are not records.
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _count_json_array(path: Path) -> int:
    """Top-level array elements via ijson's event stream (C backend when
    available) -- no Python objects are built for the records themselves."""
    count = 0
    with path.open("rb") as handle:
        for prefix, event, _ in ijson.parse(handle):
            if prefix == "item" and event in {"start_map", "start_array", "string", "number", "boolean", "null"}:
                count += 1
    return count


def _count_xml(path: Path) -> int:
    count = 0
    pending = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 22):
            data = pending + chunk
            cut = max(0, len(data) - _XML_SCAN_OVERLAP)
            # A tag that starts in the overlap is counted on the next pass, once
            # its whole name is guaranteed to be in the buffer.
            count += sum(1 for match in _XML_RECORD_START.finditer(data) if match.start() < cut)
            pending = data[cut:]
    return count + len(_XML_RECORD_START.findall(pending))


def count_records(path: Path, source_format: str) -> int | None:
    """Record count used as the progress denominator, or None if it cannot be
    determined (unreadable or malformed file -- the parse itself reports why).

    Every format is counted with the same record semantics its row adapter
    uses, so `rows_seen` reaches exactly this number on a clean import:
    CSV respects quoted multi-line fields, NDJSON skips blank lines, JSON arrays
    count top-level elements, and XML counts transaction/record/row elements.
    """
    counters = {"csv": _count_csv, "ndjson": _count_ndjson, "json": _count_json_array, "xml": _count_xml}
    counter = counters.get(source_format)
    if counter is None:
        return None
    try:
        return counter(path)
    except (OSError, ijson.JSONError, ValueError):
        return None


def rows_for_source(path: Path, source_format: str) -> Iterator[ParsedRow]:
    if source_format == "csv":
        return _csv_rows(path)
    if source_format == "ndjson":
        return _ndjson_rows(path)
    if source_format == "json":
        return _json_array_rows(path)
    if source_format == "xml":
        return _xml_rows(path)
    raise SourceParseError(f"unsupported source format: {source_format}")
