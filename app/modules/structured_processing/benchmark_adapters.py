"""Phase 7 Part 2 benchmark adapters: OCR, CDR, and finance.

Every adapter here wraps *existing* extraction/normalization code
(`document.fir_report.extract_fir_mentions`,
`structured.chunked_processing.normalize_chunk`,
`structured.cdr.normalize_cdr_records`,
`structured.finance.normalize_financial_records`) rather than
reimplementing it -- this module never rewrites a production processor,
only measures it (see this task's "extend only through additive benchmark
seams" rule).

No adapter here ever persists raw document text, raw OCR text, a raw CDR
row, a raw transaction row, a phone number, an account value, or a full
private local path in its returned `BenchmarkRunV1` -- every aggregate is
either a count, a rate, a latency/memory measurement, or a hash. See each
adapter's own docstring for exactly which fields are safe and why.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.modules.evaluation.models import BenchmarkRunStatus, BenchmarkRunV1, SplitId
from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.errors import ProcessingError
from app.modules.structured_processing.models import TextSegment
from app.modules.structured_processing.structured.chunked_processing import (
    assess_schema,
    iter_csv_record_chunks,
    iter_xlsx_record_chunks,
    normalize_chunk,
    peek_csv_header,
    peek_xlsx_header,
)
from app.modules.structured_processing.structured.json_parser import parse_json_records
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
)

from app.modules.structured_processing.benchmark_metrics import (  # isort: skip
    character_error_rate,
    field_extraction_prf,
    median,
    peak_memory_mb,
    percentile,
    word_error_rate,
)
from app.modules.structured_processing.benchmark_validation import (  # isort: skip
    benchmark_inference_config_hash,
)

RUNTIME_ENVIRONMENT = "phase7-part2-benchmark-cli-v1"


class BenchmarkArtifactUnavailableError(Exception):
    """A required local dataset/model artifact could not be found or loaded.

    Never constructed with a raw file path outside the approved benchmark
    roots, a stack trace, or file content -- only a short, safe description
    of *what* is missing.
    """


class OcrEngineError(Exception):
    """One document's OCR recognition failed in a named, safe way.

    `category` is a small, fixed vocabulary (`unreadable`,
    `unsupported_format`, `execution_failure`, `runtime_unavailable`) --
    never a raw exception message from a third-party OCR library, which
    could itself echo file paths or content.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


# --- OCR ---------------------------------------------------------------


@dataclass(frozen=True)
class OcrEngineResult:
    """What one document's OCR pass produced -- text is consumed immediately
    for metric computation and is never itself stored on a `BenchmarkRunV1`."""

    text: str
    backend: str
    model_name: str
    model_version: str
    model_sha256: str | None = None


class OcrEngine(Protocol):
    """The injectable OCR boundary both `FakeOcrEngine` (tests) and a real
    PaddleOCR-backed engine implement identically."""

    def recognize(self, image_bytes: bytes) -> OcrEngineResult: ...


@dataclass
class FakeOcrEngine:
    """A deterministic test double -- never requires PaddleOCR, a GPU, or a
    model download. `image_bytes` equal to one of the three marker
    constants below simulates the matching safe per-document failure
    category; any other bytes succeed with `fixed_text`.
    """

    fixed_text: str = "FIR No. TEST/2026/001 Police Station: Test PS"
    backend: str = "fake-cpu"
    model_name: str = "fake-ocr-engine"
    model_version: str = "0.0.0-test"
    model_sha256: str = "0" * 64

    UNREADABLE: bytes = b"__BENCHMARK_FAKE_UNREADABLE__"
    UNSUPPORTED_FORMAT: bytes = b"__BENCHMARK_FAKE_UNSUPPORTED__"
    EXECUTION_FAILURE: bytes = b"__BENCHMARK_FAKE_EXEC_FAIL__"

    def recognize(self, image_bytes: bytes) -> OcrEngineResult:
        if image_bytes == self.UNREADABLE:
            raise OcrEngineError("unreadable", "document image could not be decoded")
        if image_bytes == self.UNSUPPORTED_FORMAT:
            raise OcrEngineError("unsupported_format", "image format is not supported")
        if image_bytes == self.EXECUTION_FAILURE:
            raise OcrEngineError("execution_failure", "OCR engine raised during inference")
        return OcrEngineResult(
            text=self.fixed_text,
            backend=self.backend,
            model_name=self.model_name,
            model_version=self.model_version,
            model_sha256=self.model_sha256,
        )


@dataclass(frozen=True)
class RealOcrEngineConfig:
    """Verified model metadata plus the actual recognition callable.

    `model_name`/`model_version`/`model_sha256` must come from artifacts
    Aditya's MacBook pre-flight actually verified -- never invented here.
    `recognize_fn`/`backend_label` are supplied by the caller (a thin
    PaddleOCR wrapper written once the exact installed API is known) so
    this module never imports `paddleocr` itself and never hardcodes an
    API shape this session cannot verify. See
    `docs/runbooks/local-development.md`'s MacBook pre-flight section.
    """

    model_name: str
    model_version: str
    model_sha256: str
    backend_label: str
    recognize_fn: Callable[[bytes], str]


class ConfiguredOcrEngine:
    """Adapts a caller-supplied real OCR callable to the `OcrEngine` protocol."""

    def __init__(self, config: RealOcrEngineConfig) -> None:
        self._config = config

    def recognize(self, image_bytes: bytes) -> OcrEngineResult:
        try:
            text = self._config.recognize_fn(image_bytes)
        except Exception as exc:  # noqa: BLE001 - a third-party engine's own exception type
            raise OcrEngineError("execution_failure", "OCR engine raised during inference") from exc
        return OcrEngineResult(
            text=text,
            backend=self._config.backend_label,
            model_name=self._config.model_name,
            model_version=self._config.model_version,
            model_sha256=self._config.model_sha256,
        )


@dataclass(frozen=True)
class OcrBenchmarkSample:
    """One local benchmark input unit: an image plus optional ground truth.

    `reference_text`/`expected_fields` are ground truth loaded from the
    local benchmark manifest (see `load_ocr_benchmark_manifest`) -- `None`
    for either is a normal, expected outcome when the source dataset
    doesn't supply that kind of label, never an error.
    """

    sample_id: str
    image_bytes: bytes
    reference_text: str | None = None
    expected_fields: Mapping[str, str] | None = None


def _extracted_fields_from_text(text: str) -> dict[str, str]:
    """Run the existing, real deterministic FIR regex extractor over OCR output.

    Reuses `document.fir_report.extract_fir_mentions` unchanged -- the
    field-extraction task measures OCR quality by how well it preserves
    the same regex-matchable structure the production FIR pipeline already
    depends on, never a bespoke benchmark-only extraction rule.
    """
    mentions = extract_fir_mentions([TextSegment(page=1, text=text)])
    fields: dict[str, str] = {}
    for mention in mentions:
        if mention.entity_type_hint is not None:
            fields.setdefault(mention.entity_type_hint, mention.text)
    return fields


@dataclass(frozen=True)
class _OcrSampleOutcome:
    latency_ms: float
    engine_result: OcrEngineResult | None = None
    character_error_rate: float | None = None
    word_error_rate: float | None = None
    field_precision: float | None = None
    field_recall: float | None = None
    field_f1: float | None = None
    failure_category: str | None = None


def _run_one_ocr_sample(engine: OcrEngine, sample: OcrBenchmarkSample) -> _OcrSampleOutcome:
    start = time.monotonic()
    try:
        result = engine.recognize(sample.image_bytes)
    except OcrEngineError as exc:
        latency_ms = (time.monotonic() - start) * 1000.0
        return _OcrSampleOutcome(latency_ms=latency_ms, failure_category=exc.category)
    latency_ms = (time.monotonic() - start) * 1000.0

    cer = (
        character_error_rate(sample.reference_text, result.text)
        if sample.reference_text is not None
        else None
    )
    wer = (
        word_error_rate(sample.reference_text, result.text)
        if sample.reference_text is not None
        else None
    )
    precision = recall = f1 = None
    if sample.expected_fields is not None:
        extracted = _extracted_fields_from_text(result.text)
        precision, recall, f1 = field_extraction_prf(sample.expected_fields, extracted)
    return _OcrSampleOutcome(
        latency_ms=latency_ms,
        engine_result=result,
        character_error_rate=cer,
        word_error_rate=wer,
        field_precision=precision,
        field_recall=recall,
        field_f1=f1,
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def run_ocr_benchmark(
    *,
    engine: OcrEngine,
    samples: Sequence[OcrBenchmarkSample],
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config: dict[str, object],
    now: datetime | None = None,
) -> BenchmarkRunV1:
    """Run one OCR candidate over already-loaded local samples.

    Requires at least one sample; an empty `samples` sequence is a caller
    error (there is nothing to benchmark), not a document-level failure --
    callers should have already produced a safe `UNAVAILABLE` result
    upstream (see `benchmark.py`) if no local samples could be loaded at
    all.
    """
    if not samples:
        raise ValueError("run_ocr_benchmark requires at least one sample")
    now = now or datetime.now(UTC)
    started_at = now

    outcomes = [_run_one_ocr_sample(engine, sample) for sample in samples]
    succeeded = [o for o in outcomes if o.failure_category is None]
    failed_by_category: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.failure_category is not None:
            failed_by_category[outcome.failure_category] = (
                failed_by_category.get(outcome.failure_category, 0) + 1
            )

    latencies = [o.latency_ms for o in outcomes]
    cers = [o.character_error_rate for o in succeeded if o.character_error_rate is not None]
    wers = [o.word_error_rate for o in succeeded if o.word_error_rate is not None]
    precisions = [o.field_precision for o in succeeded if o.field_precision is not None]
    recalls = [o.field_recall for o in succeeded if o.field_recall is not None]
    f1s = [o.field_f1 for o in succeeded if o.field_f1 is not None]

    # Every sample that succeeded shares one candidate/model identity (the
    # engine is fixed for the whole run), so any one already-computed
    # outcome names the actual observed backend/model metadata -- never
    # re-run OCR a second time just to read it back.
    metadata_result = next((o.engine_result for o in outcomes if o.engine_result is not None), None)

    completed_at = datetime.now(UTC)
    config_hash = benchmark_inference_config_hash(inference_config)

    if metadata_result is None:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=f"ocr-{candidate_id}-{uuid4().hex[:12]}",
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task="ocr",  # type: ignore[arg-type]
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="unknown-see-failure-reason",
            inference_config_hash=config_hash,
            artifact_sha256=None,
            metrics=_ocr_metrics(
                document_count=len(samples),
                succeeded_count=0,
                failed_by_category=failed_by_category,
                latencies=latencies,
                cers=cers,
                wers=wers,
                precisions=precisions,
                recalls=recalls,
                f1s=f1s,
            ),
            started_at=started_at,
            completed_at=completed_at,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe="every document failed OCR recognition",
        )

    return BenchmarkRunV1(
        schema_version="v1",
        run_id=f"ocr-{candidate_id}-{uuid4().hex[:12]}",
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task="ocr",  # type: ignore[arg-type]
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile=metadata_result.backend,
        inference_config_hash=config_hash,
        artifact_sha256=metadata_result.model_sha256,
        metrics=_ocr_metrics(
            document_count=len(samples),
            succeeded_count=len(succeeded),
            failed_by_category=failed_by_category,
            latencies=latencies,
            cers=cers,
            wers=wers,
            precisions=precisions,
            recalls=recalls,
            f1s=f1s,
        ),
        started_at=started_at,
        completed_at=completed_at,
        status=BenchmarkRunStatus.SUCCEEDED,
    )


def _ocr_metrics(
    *,
    document_count: int,
    succeeded_count: int,
    failed_by_category: dict[str, int],
    latencies: list[float],
    cers: list[float],
    wers: list[float],
    precisions: list[float],
    recalls: list[float],
    f1s: list[float],
) -> dict[str, float | int | None]:
    metrics: dict[str, float | int | None] = {
        # Required by benchmark-metrics.v1.json's "ocr" metric group -- a
        # key that can't be computed for this run is still present, valued
        # `None`, never omitted (frozen rule: required key, optional value).
        "character_error_rate": _mean(cers),
        "word_error_rate": _mean(wers),
        "field_extraction_precision": _mean(precisions),
        "field_extraction_recall": _mean(recalls),
        "field_extraction_f1": _mean(f1s),
        "latency_ms": median(latencies),
        "ram_mb": peak_memory_mb(),
        "vram_mb": None,  # never measured by this CPU-side benchmark harness
        # Additional, non-required aggregate detail the task explicitly asks
        # for beyond the frozen minimum metric-group keys.
        "document_count": document_count,
        "document_success_count": succeeded_count,
        "document_failure_count": document_count - succeeded_count,
        "latency_p50_ms": median(latencies),
        "latency_p95_ms": percentile(latencies, 95.0),
        "latency_p99_ms": percentile(latencies, 99.0),
    }
    for category, count in sorted(failed_by_category.items()):
        metrics[f"document_failure_count_{category}"] = count
    return metrics


def load_ocr_benchmark_manifest(dataset_dir: Path) -> list[OcrBenchmarkSample]:
    """Load samples from this task's own local benchmark manifest format.

    This is deliberately *not* the raw FIR ICDAR 2023 release's own
    annotation format (unknown to this session -- see
    `docs/runbooks/local-development.md`'s MacBook pre-flight section,
    which is where the real ICDAR annotations get converted into this
    manifest, once their exact shape is verified). The manifest itself is
    `dataset_dir/benchmark_manifest.jsonl`: one JSON object per line with
    `sample_id` (str), `image_path` (a filename relative to `dataset_dir`),
    and optionally `reference_text` (str) and `expected_fields` (a flat
    string-to-string mapping).

    Raises `BenchmarkArtifactUnavailableError` if the manifest file itself
    is missing -- never silently returns an empty benchmark.
    """
    import json

    manifest_path = dataset_dir / "benchmark_manifest.jsonl"
    if not manifest_path.is_file():
        raise BenchmarkArtifactUnavailableError(
            "no benchmark manifest found under the configured data root for this dataset "
            "(expected 'benchmark_manifest.jsonl')"
        )
    samples: list[OcrBenchmarkSample] = []
    for line_number, line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise BenchmarkArtifactUnavailableError(
                f"benchmark manifest line {line_number} is not valid JSON"
            ) from exc
        sample_id = entry.get("sample_id")
        image_path = entry.get("image_path")
        if not isinstance(sample_id, str) or not isinstance(image_path, str):
            raise BenchmarkArtifactUnavailableError(
                f"benchmark manifest line {line_number} is missing 'sample_id'/'image_path'"
            )
        resolved_image_path = (dataset_dir / image_path).resolve()
        if dataset_dir.resolve() not in resolved_image_path.parents:
            raise BenchmarkArtifactUnavailableError(
                f"benchmark manifest line {line_number} names an image path outside the "
                f"dataset directory"
            )
        if not resolved_image_path.is_file():
            raise BenchmarkArtifactUnavailableError(
                f"benchmark manifest line {line_number} names an image file that does not exist"
            )
        reference_text = entry.get("reference_text")
        expected_fields = entry.get("expected_fields")
        samples.append(
            OcrBenchmarkSample(
                sample_id=sample_id,
                image_bytes=resolved_image_path.read_bytes(),
                reference_text=reference_text if isinstance(reference_text, str) else None,
                expected_fields=expected_fields if isinstance(expected_fields, dict) else None,
            )
        )
    return samples


# --- CDR / finance -------------------------------------------------------


_NORMALIZE_TASK_PROFILE = {
    "cdr": CDR_GENERIC_V1,
    "finance": FINANCIAL_TRANSACTION_GENERIC_V1,
}


def discover_single_input_file(dataset_dir: Path) -> Path | None:
    """The one CSV/XLSX/JSON file directly under `dataset_dir`, or `None`.

    Deliberately conservative: if zero or more than one candidate file is
    present, this returns `None` rather than guessing which one is the
    real dataset export -- the caller reports a safe `UNAVAILABLE` result
    naming the ambiguity, never silently picks one.
    """
    candidates = [
        path
        for path in sorted(dataset_dir.glob("*"))
        if path.is_file() and path.suffix.lower() in {".csv", ".xlsx", ".json"}
    ]
    return candidates[0] if len(candidates) == 1 else None


def _content_kind_for(path: Path) -> str:
    return {".csv": "csv", ".xlsx": "xlsx", ".json": "json"}[path.suffix.lower()]


@dataclass(frozen=True)
class _StructuredRunAccumulator:
    total_rows: int = 0
    valid_rows: int = 0
    malformed_by_code: dict[str, int] = field(default_factory=dict)
    mentions_emitted: int = 0


def run_structured_benchmark(
    *,
    task: str,
    input_path: Path,
    candidate_id: str,
    dataset_id: str,
    split_id: SplitId,
    inference_config: dict[str, object],
    now: datetime | None = None,
) -> BenchmarkRunV1:
    """Benchmark the existing deterministic CDR/finance pipeline against one local file.

    Reuses `structured.chunked_processing.normalize_chunk` row-by-row so a
    malformed row is safely categorized (by `ProcessingError.code`, a small
    fixed vocabulary -- never the offending value) without inflating or
    discarding the rest of the file, exactly matching production's own
    partial-success policy (`worker.run_structured_batches_job`).
    """
    if task not in _NORMALIZE_TASK_PROFILE:
        raise ValueError(f"unsupported structured benchmark task '{task}'")
    profile = _NORMALIZE_TASK_PROFILE[task]
    now = now or datetime.now(UTC)
    started_at = now
    config_hash = benchmark_inference_config_hash(inference_config)
    data = input_path.read_bytes()
    artifact_sha256 = hashlib.sha256(data).hexdigest()
    kind = _content_kind_for(input_path)

    started_monotonic = time.monotonic()
    try:
        if kind == "csv":
            header = peek_csv_header(data)
            assess_schema(profile, header)
            chunks = iter_csv_record_chunks(data, batch_size=1_000)
        elif kind == "xlsx":
            header = peek_xlsx_header(data)
            assess_schema(profile, header)
            chunks = iter_xlsx_record_chunks(data, batch_size=1_000)
        else:
            records = parse_json_records(data)
            header = list(records[0].values.keys()) if records else []
            assess_schema(profile, header)
            chunks = iter([records]) if records else iter(())

        total_rows = 0
        valid_rows = 0
        mentions_emitted = 0
        malformed_by_code: dict[str, int] = {}
        for chunk in chunks:
            result = normalize_chunk(profile, chunk)
            total_rows += len(chunk)
            valid_rows += result.valid_row_count
            mentions_emitted += len(result.mentions)
            for row in result.malformed_rows:
                malformed_by_code[row.error_code] = malformed_by_code.get(row.error_code, 0) + 1
        accumulator = _StructuredRunAccumulator(
            total_rows=total_rows,
            valid_rows=valid_rows,
            malformed_by_code=malformed_by_code,
            mentions_emitted=mentions_emitted,
        )
    except ProcessingError as exc:
        completed_at = datetime.now(UTC)
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=f"{task}-{candidate_id}-{uuid4().hex[:12]}",
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task="fir_cdr_finance_extraction",  # type: ignore[arg-type]
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="cpu",
            inference_config_hash=config_hash,
            artifact_sha256=None,
            metrics=_structured_metrics(
                accumulator=_StructuredRunAccumulator(),
                latency_ms=(time.monotonic() - started_monotonic) * 1000.0,
            ),
            started_at=started_at,
            completed_at=completed_at,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe=f"schema assessment failed: {exc.code}",
        )
    latency_ms = (time.monotonic() - started_monotonic) * 1000.0
    completed_at = datetime.now(UTC)

    # Mirrors `worker.run_structured_batches_job`'s own partial-success
    # policy exactly: a file with zero rows, or a file where *every* row
    # failed row-level normalization, is a safe FAILED result -- never a
    # fabricated SUCCEEDED with zero accepted rows.
    if accumulator.total_rows == 0:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=f"{task}-{candidate_id}-{uuid4().hex[:12]}",
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task="fir_cdr_finance_extraction",  # type: ignore[arg-type]
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="cpu",
            inference_config_hash=config_hash,
            artifact_sha256=None,
            metrics=_structured_metrics(accumulator=accumulator, latency_ms=latency_ms),
            started_at=started_at,
            completed_at=completed_at,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe="input file contained no rows",
        )
    if accumulator.valid_rows == 0:
        return BenchmarkRunV1(
            schema_version="v1",
            run_id=f"{task}-{candidate_id}-{uuid4().hex[:12]}",
            candidate_id=candidate_id,
            dataset_id=dataset_id,
            split_id=split_id,
            task="fir_cdr_finance_extraction",  # type: ignore[arg-type]
            runtime_environment=RUNTIME_ENVIRONMENT,
            hardware_profile="cpu",
            inference_config_hash=config_hash,
            artifact_sha256=None,
            metrics=_structured_metrics(accumulator=accumulator, latency_ms=latency_ms),
            started_at=started_at,
            completed_at=completed_at,
            status=BenchmarkRunStatus.FAILED,
            failure_reason_safe=(
                f"all {accumulator.total_rows} row(s) failed row-level normalization"
            ),
        )

    return BenchmarkRunV1(
        schema_version="v1",
        run_id=f"{task}-{candidate_id}-{uuid4().hex[:12]}",
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        split_id=split_id,
        task="fir_cdr_finance_extraction",  # type: ignore[arg-type]
        runtime_environment=RUNTIME_ENVIRONMENT,
        hardware_profile="cpu",
        inference_config_hash=config_hash,
        artifact_sha256=artifact_sha256,
        metrics=_structured_metrics(accumulator=accumulator, latency_ms=latency_ms),
        started_at=started_at,
        completed_at=completed_at,
        status=BenchmarkRunStatus.SUCCEEDED,
    )


def _structured_metrics(
    *, accumulator: _StructuredRunAccumulator, latency_ms: float
) -> dict[str, float | int | None]:
    total = accumulator.total_rows
    valid = accumulator.valid_rows
    malformed_total = sum(accumulator.malformed_by_code.values())
    metrics: dict[str, float | int | None] = {
        # Required by benchmark-metrics.v1.json's "fir_cdr_finance_extraction" group.
        "normalization_accuracy": (valid / total) if total else None,
        "schema_validation_error_rate": (malformed_total / total) if total else None,
        "event_coverage": float(accumulator.mentions_emitted),
        "latency_ms": latency_ms,
        # Additional safe aggregate detail this task's brief requires.
        "input_row_count": total,
        "accepted_row_count": valid,
        "rejected_row_count": malformed_total,
        "mentions_emitted_count": accumulator.mentions_emitted,
        "ram_mb": peak_memory_mb(),
    }
    for code, count in sorted(accumulator.malformed_by_code.items()):
        metrics[f"rejected_row_count_{code}"] = count
    return metrics


__all__ = [
    "RUNTIME_ENVIRONMENT",
    "BenchmarkArtifactUnavailableError",
    "ConfiguredOcrEngine",
    "FakeOcrEngine",
    "OcrBenchmarkSample",
    "OcrEngine",
    "OcrEngineError",
    "OcrEngineResult",
    "RealOcrEngineConfig",
    "discover_single_input_file",
    "load_ocr_benchmark_manifest",
    "run_ocr_benchmark",
    "run_structured_benchmark",
]
