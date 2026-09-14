"""End-to-end pipeline test using real files on disk via a `SourceResolver`.

Unlike the unit tests (which use `StaticBytesResolver` with in-memory
bytes), this exercises the `SourceResolver` protocol against actual files
written to a temporary directory — the "local files" path the task brief
calls out, and the shape a later-phase file-backed resolver would take.
No external service is involved, so this needs no self-skip guard.
"""

from __future__ import annotations

from pathlib import Path

from app.contracts.worker import WorkerStatus
from app.modules.structured_processing.worker import process_job
from tests.fixtures.structured_processing.builders import build_docx, build_xlsx
from tests.fixtures.structured_processing.factory import make_evidence_and_job


class LocalFileResolver:
    """A `SourceResolver` backed by real files on disk under `root`."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read_bytes(self, object_uri: str) -> bytes:
        filename = object_uri.removeprefix("local://")
        return (self._root / filename).read_bytes()


def test_full_pipeline_across_file_types_via_local_file_resolver(tmp_path: Path) -> None:
    (tmp_path / "fir.txt").write_bytes(b"FIR No: 501/2026, phone 9876543210")
    (tmp_path / "fir.docx").write_bytes(build_docx(["FIR No: 502/2026 in a docx file"]))
    (tmp_path / "cdr.csv").write_bytes(
        b"caller_number,callee_number,timestamp\n9876543210,9123456789,2026-01-01 10:00:00\n"
    )
    (tmp_path / "data.xlsx").write_bytes(build_xlsx(headers=["a", "b"], rows=[["1", "2"]]))
    (tmp_path / "data.json").write_bytes(b'{"a": 1, "b": [2, 3]}')

    resolver = LocalFileResolver(tmp_path)

    cases = [
        ("text/plain", "fir.txt", "fir_report_text_v1"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "fir.docx",
            "fir_report_text_v1",
        ),
        ("text/csv", "cdr.csv", "cdr_generic_v1"),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data.xlsx",
            "generic_tabular_v1",
        ),
        ("application/json", "data.json", "generic_json_v1"),
    ]

    for content_type, filename, processor_name in cases:
        evidence, job = make_evidence_and_job(
            content_type=content_type, filename=filename, processor_name=processor_name
        )
        result = process_job(job, evidence, resolver)
        assert result.status == WorkerStatus.SUCCEEDED, (filename, result.error)
        assert result.observations
