"""Documented, enforced input-safety limits for source processing.

Every limit here exists to stop a hostile or malformed input file from
exhausting memory/CPU (zip bombs, deeply nested JSON, absurdly wide
spreadsheets) before it ever reaches a parser. Limits are deliberately
conservative constants, not configurable per-request: Phase 1 processes
investigative evidence offline, not user-tunable uploads.
"""

from __future__ import annotations

import io
import zipfile

from app.modules.structured_processing.errors import ProcessingError

# --- Raw input size ---
MAX_INPUT_BYTES = 50 * 1024 * 1024  # 50 MiB per source file

# --- PDF ---
MAX_PDF_PAGES = 2000

# --- DOCX / XLSX (OOXML zip containers) ---
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MiB expanded
MAX_ZIP_COMPRESSION_RATIO = (
    100  # uncompressed/compressed per entry; above this, reject as a likely zip bomb
)

# --- CSV / XLSX tabular data ---
MAX_ROWS = 200_000
MAX_COLUMNS = 500
MAX_CELL_TEXT_LENGTH = 10_000

# --- JSON ---
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 200_000
MAX_JSON_STRING_LENGTH = 10_000

# --- Free text (DOCX/TXT/PDF page text) ---
MAX_TEXT_LENGTH = 5 * 1024 * 1024  # 5 MiB of extracted text per document

# --- FIR/report regex extraction ---
MAX_MENTION_TEXT_LENGTH = 300  # a single matched mention (phone, amount, ...) is always short


def validate_zip_container(data: bytes, *, error_code: str) -> None:
    """Reject a ZIP-based document (DOCX/XLSX) that looks like a zip bomb.

    Checked *before* handing bytes to `python-docx`/`openpyxl`: entry
    count, total uncompressed size, and per-entry compression ratio must
    all stay within bounds. `error_code` lets the caller report this as
    `invalid_docx` or `invalid_xlsx` as appropriate.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ProcessingError(
                    error_code, f"archive has more than {MAX_ZIP_ENTRIES} entries"
                )

            total_uncompressed = 0
            for info in infos:
                total_uncompressed += info.file_size
                if (
                    info.compress_size > 0
                    and (info.file_size / info.compress_size) > MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise ProcessingError(
                        error_code, "archive entry has a suspiciously high compression ratio"
                    )

            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ProcessingError(
                    error_code,
                    f"archive uncompressed size exceeds {MAX_ZIP_UNCOMPRESSED_BYTES} bytes",
                )
    except zipfile.BadZipFile as exc:
        raise ProcessingError(error_code, "not a valid zip-based document") from exc
