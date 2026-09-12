"""Vectorized, bounded-memory CDR/finance record reading, in micro-batches.

`csv_parser.py`/`xlsx_parser.py`/`json_parser.py` remain unchanged and are
still exactly right for the fallback (`generic_tabular_v1`/
`generic_json_v1`) profiles and for small/moderate files — they parse a
whole source into one `list[RawRecord]` in memory. For CDR/finance, which
this project expects to see at real-world scale (a CDR export can run to
hundreds of thousands of rows), this module instead reads a bounded number
of rows at a time and hands each chunk to the caller before reading the
next one, so a `worker.py` orchestration loop can submit a micro-batch,
emit progress, and move on without ever holding more than one chunk's rows
in memory at once.

- **CSV**: `polars.scan_csv(...).collect_batches(...)` — a true lazy,
  chunked reader; rows past the current chunk are never touched until the
  next chunk is requested.
- **XLSX**: `openpyxl`'s `read_only` row iterator (already a lazy,
  streaming reader over the underlying ZIP/XML — see `xlsx_parser.py`'s
  own docstring) is consumed in bounded groups and each group is passed
  through a `pyarrow.Table` before being handed back out as `RawRecord`s —
  genuinely vectorized batch handling, even though (honestly, since
  neither Polars nor PyArrow has a native chunked XLSX reader without an
  extra optional engine this project does not depend on) the disk-level
  read remains row-by-row via `openpyxl`.
- **JSON**: a JSON array has no streaming-friendly structure the way a
  line-delimited format does, so `json_parser.parse_json_records` still
  parses the whole (size-bounded, `MAX_INPUT_BYTES`/`MAX_ROWS`-limited)
  array at once; this module only chunks its *output* into
  batch-submission-sized groups.

Schema assessment (`assess_schema`) runs once, before any chunk is read,
against the header row alone — an ambiguous/incompatible schema is
rejected immediately (`ambiguous_schema`), never discovered gradually
partway through a large file. Per-row malformed data, in contrast, is a
*row-level*, not *schema-level*, problem: `normalize_chunk` reports each
malformed row safely (row number/location, a field-name-only reason) and
keeps normalizing the rows that are valid, per this module's documented
partial-success policy.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import polars as pl
import pyarrow as pa
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.contracts.common import SourceLocator
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import (
    MAX_CELL_TEXT_LENGTH,
    MAX_COLUMNS,
    MAX_ROWS,
    validate_zip_container,
)
from app.modules.structured_processing.models import (
    ParserProfile,
    RawMention,
    RawRecord,
    normalize_header,
)
from app.modules.structured_processing.structured.cdr import normalize_cdr_records
from app.modules.structured_processing.structured.finance import normalize_financial_records
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
)

_NORMALIZE_FN: dict[str, Callable[[list[RawRecord]], list[RawMention]]] = {
    CDR_GENERIC_V1.name: normalize_cdr_records,
    FINANCIAL_TRANSACTION_GENERIC_V1.name: normalize_financial_records,
}


@dataclass(frozen=True)
class MalformedRow:
    """One row that failed row-level normalization -- safe, never the raw offending value."""

    row_index: int
    locator: SourceLocator
    error_code: str
    reason: str


@dataclass(frozen=True)
class ChunkResult:
    """The outcome of normalizing one bounded chunk of records."""

    mentions: list[RawMention]
    malformed_rows: list[MalformedRow]
    valid_row_count: int


def assess_schema(profile: ParserProfile, header_columns: Sequence[str]) -> None:
    """Reject a header that cannot resolve this profile's required fields, before reading any rows.

    Raises `ambiguous_schema` naming only the missing canonical field names
    and the actual column headers present (safe schema metadata, never a
    row value) -- exactly the safe information the task's malformed-schema
    policy calls for.
    """
    normalized_header = {normalize_header(h) for h in header_columns}
    missing = [
        field
        for field in profile.required_fields
        if not any(alias in normalized_header for alias in profile.field_aliases.get(field, ()))
    ]
    if missing:
        raise ProcessingError(
            ErrorCode.AMBIGUOUS_SCHEMA,
            f"header does not resolve required field(s) {sorted(missing)} for profile "
            f"'{profile.name}' -- columns present: {sorted(header_columns)}",
        )


def normalize_chunk(profile: ParserProfile, records: list[RawRecord]) -> ChunkResult:
    """Normalize one chunk's records, tolerating row-level failures.

    Each record is normalized independently (one `normalize_fn` call per
    record) so a single malformed row can never abort the rest of the
    chunk -- the documented partial-success policy. `profile` must be
    `cdr_generic_v1` or `financial_transaction_generic_v1`; any other
    profile name is a caller bug, not a data problem, and raises
    `KeyError` rather than being silently accepted.
    """
    normalize_fn = _NORMALIZE_FN[profile.name]
    mentions: list[RawMention] = []
    malformed: list[MalformedRow] = []
    for record in records:
        try:
            mentions.extend(normalize_fn([record]))
        except ProcessingError as exc:
            malformed.append(
                MalformedRow(
                    row_index=record.index,
                    locator=record.locator_for(None),
                    error_code=exc.code,
                    reason=exc.message,
                )
            )
    return ChunkResult(
        mentions=mentions, malformed_rows=malformed, valid_row_count=len(records) - len(malformed)
    )


def peek_csv_header(data: bytes) -> list[str]:
    header_df = pl.read_csv(io.BytesIO(data), n_rows=0, infer_schema_length=0)
    return list(header_df.columns)


def iter_csv_record_chunks(data: bytes, *, batch_size: int) -> Iterator[list[RawRecord]]:
    """Yield bounded chunks of `RawRecord`s read lazily from CSV bytes.

    `infer_schema_length=0` forces every column to be read as a string
    (never type-coerced) -- consistent with `csv_parser.parse_csv`'s own
    "never silently coerce an ambiguous format" policy; normalization
    (phone/timestamp/amount parsing) happens explicitly downstream, never
    implicitly during the read.
    """
    lazy_frame = pl.scan_csv(io.BytesIO(data), infer_schema_length=0)
    columns = lazy_frame.collect_schema().names()
    if len(columns) > MAX_COLUMNS:
        raise ProcessingError(
            ErrorCode.INPUT_LIMIT_EXCEEDED, f"CSV exceeds the {MAX_COLUMNS}-column limit"
        )

    row_cursor = 0
    for batch in lazy_frame.collect_batches(chunk_size=batch_size):
        if row_cursor + batch.height > MAX_ROWS:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED, f"CSV exceeds the {MAX_ROWS}-row limit"
            )
        records: list[RawRecord] = []
        for local_index, row in enumerate(batch.iter_rows(named=True)):
            row_number = row_cursor + local_index + 2  # +1 header, +1 for 1-based numbering
            values: dict[str, str] = {}
            for name, value in row.items():
                if value is None:
                    continue
                if len(value) > MAX_CELL_TEXT_LENGTH:
                    raise ProcessingError(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        f"CSV row {row_number} has a cell exceeding "
                        f"{MAX_CELL_TEXT_LENGTH} characters",
                    )
                values[name] = value
            records.append(
                RawRecord(
                    index=row_cursor + local_index,
                    values=values,
                    locator_for=_make_flat_locator(row_number, columns),
                )
            )
        row_cursor += batch.height
        yield records


def peek_xlsx_header(data: bytes) -> list[str]:
    validate_zip_container(data, error_code=ErrorCode.INVALID_XLSX)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except (InvalidFileException, KeyError, OSError) as exc:
        raise ProcessingError(ErrorCode.INVALID_XLSX, "XLSX could not be parsed") from exc
    try:
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=False)
        header_row = next(rows_iter)
    except (StopIteration, IndexError) as exc:
        raise ProcessingError(ErrorCode.INVALID_XLSX, "sheet has no header row") from exc
    finally:
        workbook.close()
    return [str(cell.value).strip() if cell.value is not None else "" for cell in header_row]


def iter_xlsx_record_chunks(data: bytes, *, batch_size: int) -> Iterator[list[RawRecord]]:
    """Yield bounded chunks of `RawRecord`s from XLSX, batched through PyArrow.

    `openpyxl(read_only=True)` remains the disk-level reader (already a
    lazy row iterator, per `xlsx_parser.py`'s own docstring) -- there is no
    native chunked-XLSX reader in Polars/PyArrow without an extra optional
    engine this project does not depend on. Each accumulated group of rows
    is assembled into a `pyarrow.Table` before being handed back out as
    `RawRecord`s, so the actual per-chunk data handling is genuinely
    columnar/vectorized, not a plain Python list accumulation.
    """
    validate_zip_container(data, error_code=ErrorCode.INVALID_XLSX)
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except (InvalidFileException, KeyError, OSError) as exc:
        raise ProcessingError(ErrorCode.INVALID_XLSX, "XLSX could not be parsed") from exc

    try:
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=False)
        try:
            header_row = next(rows_iter)
        except StopIteration as exc:
            raise ProcessingError(ErrorCode.INVALID_XLSX, "sheet has no header row") from exc

        if len(header_row) > MAX_COLUMNS:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED, f"sheet exceeds the {MAX_COLUMNS}-column limit"
            )
        column_names: list[str] = []
        for cell in header_row:
            name = str(cell.value).strip() if cell.value is not None else ""
            if name and name not in column_names:
                column_names.append(name)

        sheet_title = sheet.title
        buffer: list[tuple[int, dict[str, str]]] = []
        data_row_index = 0

        for row_number, row in enumerate(rows_iter, start=2):
            if all(cell.value is None for cell in row):
                continue  # fully empty row: nothing to observe
            if data_row_index >= MAX_ROWS:
                raise ProcessingError(
                    ErrorCode.INPUT_LIMIT_EXCEEDED, f"sheet exceeds the {MAX_ROWS}-row limit"
                )
            values: dict[str, str] = {}
            for idx, name in enumerate(column_names):
                if idx >= len(row):
                    continue
                raw_value = row[idx].value
                if raw_value is None:
                    continue
                text_value = str(raw_value)
                if len(text_value) > MAX_CELL_TEXT_LENGTH:
                    raise ProcessingError(
                        ErrorCode.INPUT_LIMIT_EXCEEDED,
                        f"sheet row {row_number} has a cell exceeding "
                        f"{MAX_CELL_TEXT_LENGTH} characters",
                    )
                values[name] = text_value
            buffer.append((row_number, values))
            data_row_index += 1

            if len(buffer) >= batch_size:
                yield _flush_xlsx_buffer(buffer, sheet_title, column_names)
                buffer = []

        if buffer:
            yield _flush_xlsx_buffer(buffer, sheet_title, column_names)
    finally:
        workbook.close()


def _flush_xlsx_buffer(
    buffer: list[tuple[int, dict[str, str]]], sheet_title: str, column_names: list[str]
) -> list[RawRecord]:
    """Assemble one buffered group of rows into a `pyarrow.Table`, then back into `RawRecord`s."""
    columns: dict[str, list[str | None]] = {name: [] for name in column_names}
    for _, values in buffer:
        for name in column_names:
            columns[name].append(values.get(name))
    table = pa.table(columns)

    records: list[RawRecord] = []
    for local_index, row_number in enumerate((row_number for row_number, _ in buffer)):
        row_values = {
            name: table.column(name)[local_index].as_py()
            for name in column_names
            if table.column(name)[local_index].as_py() is not None
        }
        records.append(
            RawRecord(
                index=local_index,
                values=row_values,
                locator_for=_make_sheet_locator(sheet_title, row_number, column_names),
            )
        )
    return records


def _make_flat_locator(
    row_number: int, columns: Sequence[str]
) -> Callable[[str | None], SourceLocator]:
    index_of = {name: idx for idx, name in enumerate(columns)}

    def locator_for(field_name: str | None) -> SourceLocator:
        if field_name is None:
            return SourceLocator(row=row_number)
        idx = index_of.get(field_name)
        return (
            SourceLocator(row=row_number)
            if idx is None
            else SourceLocator(row=row_number, column=idx + 1)
        )

    return locator_for


def _make_sheet_locator(
    sheet_title: str, row_number: int, columns: Sequence[str]
) -> Callable[[str | None], SourceLocator]:
    index_of = {name: idx for idx, name in enumerate(columns)}

    def locator_for(field_name: str | None) -> SourceLocator:
        if field_name is None:
            return SourceLocator(sheet=sheet_title, row=row_number)
        idx = index_of.get(field_name)
        if idx is None:
            return SourceLocator(sheet=sheet_title, row=row_number)
        return SourceLocator(sheet=sheet_title, row=row_number, column=idx + 1)

    return locator_for


__all__ = [
    "ChunkResult",
    "MalformedRow",
    "assess_schema",
    "iter_csv_record_chunks",
    "iter_xlsx_record_chunks",
    "normalize_chunk",
    "peek_csv_header",
    "peek_xlsx_header",
]
