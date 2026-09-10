"""Safe CSV parsing via the stdlib `csv` module.

CSV has no formula/macro concept, so there is nothing to "not execute"
here — the safety concerns are purely about bounding size (rows, columns,
cell length) and decoding text the same conservative way `document/txt.py`
does. Header names are preserved exactly as written; alias resolution
(matching a header like `"Caller Number"` to the canonical `caller_number`
field) is the caller's job (`cdr.py`/`finance.py`), not this module's.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable

from app.contracts.common import SourceLocator
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import MAX_CELL_TEXT_LENGTH, MAX_COLUMNS, MAX_ROWS
from app.modules.structured_processing.models import RawRecord


def parse_csv(data: bytes) -> list[RawRecord]:
    """Parse CSV bytes into `RawRecord`s, one per data row (header excluded).

    Raises `malformed_csv` for unparsable content or a missing header row,
    and `input_limit_exceeded` if rows, columns, or a cell's text exceed
    the documented limits.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ProcessingError(ErrorCode.MALFORMED_CSV, "CSV has no header row") from exc
    except csv.Error as exc:
        raise ProcessingError(
            ErrorCode.MALFORMED_CSV, "CSV header row could not be parsed"
        ) from exc

    if len(header) > MAX_COLUMNS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"CSV exceeds the {MAX_COLUMNS}-column limit"
        )

    column_index: dict[str, int] = {}
    for idx, name in enumerate(header):
        key = name.strip()
        if key and key not in column_index:
            column_index[key] = idx

    records: list[RawRecord] = []
    try:
        for row_number, row in enumerate(reader, start=2):  # row 1 is the header
            data_row_index = row_number - 2
            if data_row_index >= MAX_ROWS:
                raise ProcessingError(
                    ErrorCode.INPUT_LIMIT_EXCEEDED, f"CSV exceeds the {MAX_ROWS}-row limit"
                )
            if len(row) > MAX_COLUMNS:
                raise ProcessingError(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    f"CSV row {row_number} exceeds the {MAX_COLUMNS}-column limit",
                )

            values: dict[str, str] = {}
            for name, idx in column_index.items():
                if idx < len(row):
                    cell = row[idx]
                    if len(cell) > MAX_CELL_TEXT_LENGTH:
                        raise ProcessingError(
                            ErrorCode.INPUT_LIMIT_EXCEEDED,
                            f"CSV row {row_number} has a cell exceeding "
                            f"{MAX_CELL_TEXT_LENGTH} characters",
                        )
                    values[name] = cell

            records.append(
                RawRecord(
                    index=data_row_index,
                    values=values,
                    locator_for=_make_locator(row_number, column_index),
                )
            )
    except csv.Error as exc:
        raise ProcessingError(ErrorCode.MALFORMED_CSV, "CSV could not be parsed") from exc

    return records


def _make_locator(
    row_number: int, column_index: dict[str, int]
) -> Callable[[str | None], SourceLocator]:
    def locator_for(field_name: str | None) -> SourceLocator:
        if field_name is None:
            return SourceLocator(row=row_number)
        idx = column_index.get(field_name)
        if idx is None:
            return SourceLocator(row=row_number)
        return SourceLocator(row=row_number, column=idx + 1)

    return locator_for
