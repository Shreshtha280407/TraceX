"""Synthetic, non-sensitive fixture builders for structured-processing tests.

No `reportlab` (not an allowed dependency for this phase): PDFs are built
by hand-assembling a minimal, valid single-font PDF byte stream directly,
which is enough to exercise real `pypdf` extraction without adding a new
dependency. DOCX/XLSX fixtures use `python-docx`/`openpyxl` directly, the
same libraries the app code parses them with.
"""

from __future__ import annotations

import io

from docx import Document
from openpyxl import Workbook


def build_minimal_pdf(page_texts: list[str | None]) -> bytes:
    """A minimal, valid multi-page PDF. `None` for a page means an empty
    content stream (simulates a scanned/image-only page)."""
    n_pages = len(page_texts)
    catalog_num = 1
    pages_num = 2
    first_page_num = 3
    first_content_num = first_page_num + n_pages
    font_num = first_content_num + n_pages

    page_nums = list(range(first_page_num, first_page_num + n_pages))
    content_nums = list(range(first_content_num, first_content_num + n_pages))

    objects: list[bytes] = []
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    objects.append(f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode())
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())

    for page_num in page_nums:
        content_num = content_nums[page_nums.index(page_num)]
        objects.append(
            (
                f"<< /Type /Page /Parent {pages_num} 0 R "
                f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
                f"/MediaBox [0 0 612 792] /Contents {content_num} 0 R >>"
            ).encode()
        )

    for text in page_texts:
        if text:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream_body = f"BT /F1 12 Tf 72 700 Td ({escaped}) Tj ET".encode()
        else:
            stream_body = b""
        objects.append(
            f"<< /Length {len(stream_body)} >>\nstream\n".encode() + stream_body + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buf = bytearray()
    buf += b"%PDF-1.4\n"
    offsets = [0]
    for idx, obj_body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{idx} 0 obj\n".encode() + obj_body + b"\nendobj\n"

    xref_offset = len(buf)
    total_objs = len(objects) + 1
    buf += f"xref\n0 {total_objs}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {total_objs} /Root {catalog_num} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode()

    return bytes(buf)


def build_encrypted_pdf(password: str = "secret123") -> bytes:
    """A single blank page PDF, encrypted with `pypdf`'s own writer."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def build_docx(paragraphs: list[str], table_rows: list[list[str]] | None = None) -> bytes:
    """A DOCX with the given paragraphs, plus an optional table."""
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table_rows:
        table = document.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row_values in enumerate(table_rows):
            for c, value in enumerate(row_values):
                table.cell(r, c).text = value
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def build_xlsx(
    headers: list[str],
    rows: list[list[object]],
    *,
    sheet_name: str = "Sheet1",
    formula_cell: tuple[int, int, str] | None = None,
) -> bytes:
    """An XLSX with one sheet. `formula_cell` is `(row_index, col_index, formula)`,
    0-based over `rows`, written as an actual Excel formula (e.g. "=1+1")."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = sheet_name
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    if formula_cell is not None:
        row_idx, col_idx, formula = formula_cell
        # +2: row 1 is the header, openpyxl rows are 1-based.
        sheet.cell(row=row_idx + 2, column=col_idx + 1, value=formula)
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()
