"""Safe JSON parsing via the stdlib `json` module.

Two entry points, matching the two ways JSON shows up in this phase:

- `parse_json_records`: a top-level array of objects (or a single
  top-level `{"key": [...]}` wrapper) — CDR/financial data delivered as
  JSON instead of CSV/XLSX. Produces the same `RawRecord` shape the CSV/
  XLSX parsers do, so `cdr.py`/`finance.py` don't need to know which
  format the data arrived in.
- `traverse_json_scalars`: generic recursive traversal for JSON that
  doesn't match a record-array shape, used by `generic_json_v1`. Every
  scalar leaf becomes one `(json_path, value)` pair; containers themselves
  are never captured as a value (the whole document is never serialized
  into a single observation).

`json.loads` never executes anything — JSON has no formula/code concept.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.contracts.common import SourceLocator
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import (
    MAX_CELL_TEXT_LENGTH,
    MAX_INPUT_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_JSON_STRING_LENGTH,
    MAX_ROWS,
)
from app.modules.structured_processing.models import RawRecord

JsonScalar = str | int | float | bool


def _load(data: bytes) -> Any:
    if len(data) > MAX_INPUT_BYTES:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"JSON exceeds the {MAX_INPUT_BYTES}-byte limit"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProcessingError(ErrorCode.MALFORMED_JSON, "JSON is not valid UTF-8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProcessingError(ErrorCode.MALFORMED_JSON, "JSON could not be parsed") from exc


def parse_json_records(data: bytes) -> list[RawRecord]:
    """Parse a top-level JSON array of objects into `RawRecord`s.

    A single top-level `{"key": [...]}` wrapper is unwrapped automatically
    when there is exactly one array-valued key. Non-object array elements
    are skipped (not a hard failure) since a record-shaped array may
    legitimately contain the occasional malformed entry.
    """
    parsed = _load(data)

    if isinstance(parsed, dict):
        array_candidates = [value for value in parsed.values() if isinstance(value, list)]
        if len(array_candidates) == 1:
            parsed = array_candidates[0]

    if not isinstance(parsed, list):
        raise ProcessingError(ErrorCode.MALFORMED_JSON, "expected a JSON array of records")

    records: list[RawRecord] = []
    for index, item in enumerate(parsed):
        if index >= MAX_ROWS:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED, f"JSON array exceeds the {MAX_ROWS}-record limit"
            )
        if not isinstance(item, dict):
            continue

        values: dict[str, str] = {}
        for key, value in item.items():
            if isinstance(value, str | int | float | bool):
                text_value = str(value)
                if len(text_value) > MAX_CELL_TEXT_LENGTH:
                    raise ProcessingError(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        f"record {index} field '{key}' exceeds {MAX_CELL_TEXT_LENGTH} characters",
                    )
                values[key] = text_value

        records.append(
            RawRecord(index=index, values=values, locator_for=_make_record_locator(index))
        )
    return records


def _make_record_locator(index: int) -> Callable[[str | None], SourceLocator]:
    def locator_for(field_name: str | None) -> SourceLocator:
        json_path = f"$[{index}]" if field_name is None else f"$[{index}].{field_name}"
        return SourceLocator(json_path=json_path)

    return locator_for


def traverse_json_scalars(data: bytes) -> list[tuple[str, JsonScalar]]:
    """Recursively walk any JSON document, yielding `(json_path, value)` per scalar leaf.

    Enforces `MAX_JSON_DEPTH` (nesting) and `MAX_JSON_NODES` (total nodes
    visited) as it goes, so a pathological document fails fast rather than
    consuming unbounded memory/CPU. `null` values are skipped: there is no
    meaningful scalar to observe there.
    """
    parsed = _load(data)
    results: list[tuple[str, JsonScalar]] = []
    node_count = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED, f"JSON exceeds the {MAX_JSON_NODES}-node limit"
            )
        if depth > MAX_JSON_DEPTH:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                f"JSON exceeds the {MAX_JSON_DEPTH}-level nesting limit",
            )

        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}", depth + 1)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]", depth + 1)
        elif isinstance(node, str | int | float | bool):
            if isinstance(node, str) and len(node) > MAX_JSON_STRING_LENGTH:
                raise ProcessingError(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    f"JSON string at {path} exceeds {MAX_JSON_STRING_LENGTH} characters",
                )
            results.append((path, node))
        # None (JSON null): no meaningful scalar to observe; skipped.

    walk(parsed, "$", 0)
    return results
