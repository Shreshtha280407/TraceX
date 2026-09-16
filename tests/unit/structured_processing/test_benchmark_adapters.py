"""Phase 7 Part 2 benchmark adapter tests.

Every test here uses `FakeOcrEngine` or a small synthetic CSV/XLSX/JSON
file built in a pytest `tmp_path` -- never PaddleOCR, a GPU, a real model
download, or any fragment of the real FIR ICDAR 2023/GoMask/AMLSim
datasets.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.modules.evaluation.models import BenchmarkRunStatus, SplitId
from app.modules.structured_processing.benchmark_adapters import (
    BenchmarkArtifactUnavailableError,
    FakeOcrEngine,
    OcrBenchmarkSample,
    discover_single_input_file,
    load_ocr_benchmark_manifest,
    run_ocr_benchmark,
    run_structured_benchmark,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
# `police_station`'s regex captures up to the next comma/semicolon/newline --
# a newline before "Phone:" keeps that mention from greedily swallowing the
# phone number, so only `phone_number` is extracted here for a clean,
# exact-match field-extraction test.
_FIR_TEXT = "FIR No. TEST/2026/001\nPolice Station: Test PS\nPhone: 9876543210"


#: Exactly what `extract_fir_mentions` finds in `_FIR_TEXT` -- verified
#: directly against the real deterministic extractor, not guessed, so a
#: "perfect" sample genuinely scores a perfect field-extraction F1.
_FIR_TEXT_EXPECTED_FIELDS = {
    "fir_number": "TEST/2026/001",
    "police_station": "Test PS",
    "phone_number": "9876543210",
}


def _ocr_sample(sample_id: str = "doc-1", **overrides: object) -> OcrBenchmarkSample:
    defaults: dict[str, object] = {
        "sample_id": sample_id,
        "image_bytes": b"synthetic-fixture-bytes",
        "reference_text": _FIR_TEXT,
        "expected_fields": dict(_FIR_TEXT_EXPECTED_FIELDS),
    }
    defaults.update(overrides)
    return OcrBenchmarkSample(**defaults)  # type: ignore[arg-type]


# --- OCR: fakes produce valid typed results (proof point 9) -----------------


def test_fake_ocr_engine_produces_a_valid_succeeded_run() -> None:
    engine = FakeOcrEngine(fixed_text=_FIR_TEXT)
    run = run_ocr_benchmark(
        engine=engine,
        samples=[_ocr_sample()],
        candidate_id="paddleocr-ppocrv5-mobile",
        dataset_id="fir_icdar_2023",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"variant": "mobile"},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.artifact_sha256 == "0" * 64
    assert run.metrics["character_error_rate"] == 0.0
    assert run.metrics["field_extraction_f1"] == 1.0
    assert run.metrics["document_success_count"] == 1


def test_ocr_run_requires_at_least_one_sample() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        run_ocr_benchmark(
            engine=FakeOcrEngine(),
            samples=[],
            candidate_id="paddleocr-ppocrv5-mobile",
            dataset_id="fir_icdar_2023",
            split_id=SplitId.DEVELOPMENT,
            inference_config={},
            now=_NOW,
        )


# --- OCR: safe per-document failure classification (task requirement) -------


def test_ocr_per_document_failures_are_safely_categorized_without_content() -> None:
    engine = FakeOcrEngine(fixed_text=_FIR_TEXT)
    samples = [
        _ocr_sample("doc-ok"),
        OcrBenchmarkSample(sample_id="doc-unreadable", image_bytes=FakeOcrEngine.UNREADABLE),
        OcrBenchmarkSample(
            sample_id="doc-unsupported", image_bytes=FakeOcrEngine.UNSUPPORTED_FORMAT
        ),
        OcrBenchmarkSample(sample_id="doc-exec-fail", image_bytes=FakeOcrEngine.EXECUTION_FAILURE),
    ]
    run = run_ocr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id="paddleocr-ppocrv5-mobile",
        dataset_id="fir_icdar_2023",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED  # one document did succeed
    assert run.metrics["document_count"] == 4
    assert run.metrics["document_success_count"] == 1
    assert run.metrics["document_failure_count_unreadable"] == 1
    assert run.metrics["document_failure_count_unsupported_format"] == 1
    assert run.metrics["document_failure_count_execution_failure"] == 1


def test_ocr_run_fails_safely_when_every_document_fails() -> None:
    engine = FakeOcrEngine()
    samples = [OcrBenchmarkSample(sample_id="doc-1", image_bytes=FakeOcrEngine.UNREADABLE)]
    run = run_ocr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id="paddleocr-ppocrv5-mobile",
        dataset_id="fir_icdar_2023",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.artifact_sha256 is None
    assert run.failure_reason_safe is not None
    assert "unreadable" not in run.failure_reason_safe or "document" in run.failure_reason_safe


# --- OCR: never leaks raw text (proof point 6) -------------------------------


def test_ocr_metrics_never_contain_raw_ocr_text_or_reference_text() -> None:
    secret_text = "FIR No. SECRET/9999/999 Phone: 1234567890 [do-not-leak-this-marker]"
    engine = FakeOcrEngine(fixed_text=secret_text)
    run = run_ocr_benchmark(
        engine=engine,
        samples=[_ocr_sample(reference_text=secret_text)],
        candidate_id="paddleocr-ppocrv5-mobile",
        dataset_id="fir_icdar_2023",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    serialized = run.model_dump_json()
    assert "do-not-leak-this-marker" not in serialized
    assert "1234567890" not in serialized
    assert all(isinstance(v, (int, float, type(None))) for v in run.metrics.values())


# --- OCR local benchmark manifest loading ------------------------------------


def test_load_ocr_benchmark_manifest_reads_samples(tmp_path: Path) -> None:
    image_path = tmp_path / "sample-1.png"
    image_path.write_bytes(b"not-a-real-image-just-bytes")
    manifest_path = tmp_path / "benchmark_manifest.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "sample_id": "sample-1",
                "image_path": "sample-1.png",
                "reference_text": "hello",
                "expected_fields": {"phone_number": "9876543210"},
            }
        )
        + "\n"
    )
    samples = load_ocr_benchmark_manifest(tmp_path)
    assert len(samples) == 1
    assert samples[0].sample_id == "sample-1"
    assert samples[0].image_bytes == b"not-a-real-image-just-bytes"
    assert samples[0].reference_text == "hello"


def test_load_ocr_benchmark_manifest_missing_file_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkArtifactUnavailableError):
        load_ocr_benchmark_manifest(tmp_path)


def test_load_ocr_benchmark_manifest_rejects_path_outside_dataset_dir(tmp_path: Path) -> None:
    manifest_path = tmp_path / "benchmark_manifest.jsonl"
    manifest_path.write_text(json.dumps({"sample_id": "s1", "image_path": "../outside.png"}) + "\n")
    with pytest.raises(BenchmarkArtifactUnavailableError, match="outside the dataset directory"):
        load_ocr_benchmark_manifest(tmp_path)


# --- CDR / finance: reused deterministic pipeline ----------------------------

_CDR_HEADER = "caller_number,callee_number,timestamp,duration_seconds\n"
_FINANCE_HEADER = "sender_account,receiver_account,amount,currency,timestamp\n"


def _write_csv(tmp_path: Path, name: str, header: str, rows: list[str]) -> Path:
    path = tmp_path / name
    path.write_text(header + "\n".join(rows) + "\n")
    return path


def test_cdr_benchmark_accepts_valid_rows_and_rejects_malformed_ones(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        "cdr.csv",
        _CDR_HEADER,
        [
            "9876543210,9123456780,2026-01-01 10:00:00,60",
            "9876543211,,2026-01-01 10:05:00,30",  # missing callee_number
            "9876543212,9123456782,not-a-timestamp,45",  # bad timestamp
        ],
    )
    run = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"profile": "cdr_generic_v1"},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["input_row_count"] == 3
    assert run.metrics["accepted_row_count"] == 1
    assert run.metrics["rejected_row_count"] == 2
    assert run.artifact_sha256 is not None and len(run.artifact_sha256) == 64


def test_finance_benchmark_accepts_valid_rows_and_rejects_malformed_ones(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        "finance.csv",
        _FINANCE_HEADER,
        [
            "ACC001,ACC002,100.50,INR,2026-01-01 10:00:00",
            "ACC003,ACC004,not-a-number,INR,2026-01-01 10:05:00",  # bad amount
            "ACC005,,50.00,INR,2026-01-01 10:10:00",  # missing receiver_account
        ],
    )
    run = run_structured_benchmark(
        task="finance",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="ibm_amlsim",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"profile": "financial_transaction_generic_v1"},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.SUCCEEDED
    assert run.metrics["input_row_count"] == 3
    assert run.metrics["accepted_row_count"] == 1
    assert run.metrics["rejected_row_count"] == 2


# --- CDR / finance: malformed input cannot inflate counts (proof point 10) --


def test_malformed_rows_never_inflate_accepted_or_emitted_counts(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path,
        "cdr.csv",
        _CDR_HEADER,
        ["bad,bad,bad,bad" for _ in range(5)],
    )
    run = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    # Every row is malformed (missing a valid timestamp/participant) -> the
    # whole run reports a safe FAILED status, never a fabricated success.
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.metrics["accepted_row_count"] == 0
    assert run.metrics["mentions_emitted_count"] == 0


def test_ambiguous_schema_produces_a_failed_run_not_a_crash(tmp_path: Path) -> None:
    path = _write_csv(tmp_path, "cdr.csv", "not_a_real_column\n", ["value1"])
    run = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    assert run.status == BenchmarkRunStatus.FAILED
    assert run.failure_reason_safe is not None
    assert "ambiguous_schema" in run.failure_reason_safe


# --- CDR / finance: never leak raw identifiers (proof points 7, 8) ---------


def test_cdr_metrics_never_contain_raw_phone_or_caller_callee_values(tmp_path: Path) -> None:
    secret_phone = "9876543210"
    path = _write_csv(
        tmp_path, "cdr.csv", _CDR_HEADER, [f"{secret_phone},9123456780,2026-01-01 10:00:00,60"]
    )
    run = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    serialized = run.model_dump_json()
    assert secret_phone not in serialized
    assert "9123456780" not in serialized
    assert all(isinstance(v, (int, float, type(None))) for v in run.metrics.values())


def test_finance_metrics_never_contain_raw_account_amount_or_reference_values(
    tmp_path: Path,
) -> None:
    path = _write_csv(
        tmp_path,
        "finance.csv",
        "sender_account,receiver_account,amount,currency,timestamp,reference\n",
        ["ACC-SECRET-001,ACC-SECRET-002,123456.78,INR,2026-01-01 10:00:00,do-not-leak-ref"],
    )
    run = run_structured_benchmark(
        task="finance",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="ibm_amlsim",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    serialized = run.model_dump_json()
    assert "ACC-SECRET-001" not in serialized
    assert "ACC-SECRET-002" not in serialized
    assert "123456.78" not in serialized
    assert "do-not-leak-ref" not in serialized


# --- discover_single_input_file ---------------------------------------------


def test_discover_single_input_file_returns_the_one_match(tmp_path: Path) -> None:
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    assert discover_single_input_file(tmp_path) == tmp_path / "data.csv"


def test_discover_single_input_file_is_none_when_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("a\n1\n")
    (tmp_path / "b.csv").write_text("a\n1\n")
    assert discover_single_input_file(tmp_path) is None


def test_discover_single_input_file_is_none_when_absent(tmp_path: Path) -> None:
    assert discover_single_input_file(tmp_path) is None


# --- configuration hash affects the result (proof point 13) ----------------


def test_different_inference_config_changes_the_hash(tmp_path: Path) -> None:
    path = _write_csv(
        tmp_path, "cdr.csv", _CDR_HEADER, ["9876543210,9123456780,2026-01-01 10:00:00,60"]
    )
    run_a = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"variant": "a"},
        now=_NOW,
    )
    run_b = run_structured_benchmark(
        task="cdr",
        input_path=path,
        candidate_id="existing-deterministic-parsers",
        dataset_id="gomask_voice_cdr",
        split_id=SplitId.DEVELOPMENT,
        inference_config={"variant": "b"},
        now=_NOW,
    )
    assert run_a.inference_config_hash != run_b.inference_config_hash


# --- failures never leak stack traces or content (proof point 14) ----------


def test_ocr_engine_error_message_never_contains_the_image_bytes() -> None:
    engine = FakeOcrEngine()
    samples = [OcrBenchmarkSample(sample_id="d1", image_bytes=FakeOcrEngine.EXECUTION_FAILURE)]
    run = run_ocr_benchmark(
        engine=engine,
        samples=samples,
        candidate_id="paddleocr-ppocrv5-mobile",
        dataset_id="fir_icdar_2023",
        split_id=SplitId.DEVELOPMENT,
        inference_config={},
        now=_NOW,
    )
    assert run.failure_reason_safe is not None
    assert "Traceback" not in run.failure_reason_safe
    assert "__BENCHMARK_FAKE" not in run.failure_reason_safe
