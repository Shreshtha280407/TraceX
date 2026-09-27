"""Streaming CSV, NDJSON, JSON-array and XML source adapters with stable locators."""

from __future__ import annotations

import csv
import json
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
