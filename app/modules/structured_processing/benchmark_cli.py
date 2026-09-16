"""Phase 7 Part 2 safe local benchmark CLI.

    uv run python -m app.modules.structured_processing.benchmark_cli \\
        --dataset-id fir_icdar_2023 \\
        --candidate-id paddleocr-ppocrv5-mobile \\
        --data-root "$TRACEX_BENCHMARK_DATA_ROOT" \\
        --model-cache-root "$TRACEX_MODEL_CACHE_ROOT" \\
        --output-root "$TRACEX_BENCHMARK_OUTPUT_ROOT"

Every `--*-root` flag is optional -- when omitted, the matching
`TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/
`TRACEX_BENCHMARK_OUTPUT_ROOT` environment variable is used instead (see
`benchmark_validation.py`). Neither this CLI nor anything it calls ever
hardcodes a local path, downloads a dataset or model, or writes to Neo4j.
Mirrors `app.modules.media_processing.benchmark`'s existing "measured
results only" CLI convention, adapted to Phase 7 Part 1's typed
`BenchmarkRunV1` contract.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.modules.evaluation.models import BenchmarkRunStatus, SplitId
from app.modules.structured_processing.benchmark import OcrCandidateArtifact, run_benchmark
from app.modules.structured_processing.benchmark_validation import (
    BenchmarkConfigError,
    UnknownCandidateError,
    UnknownDatasetError,
    UnsupportedCombinationError,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.structured_processing.benchmark_cli",
        description=(
            "Run one Phase 7 Part 2 local benchmark (FIR ICDAR 2023 OCR, GoMask "
            "Voice CDR, or IBM AMLSim finance) and write a safe, git-reviewable "
            "aggregate JSON result. Never fabricates a result for a missing local "
            "dataset/model artifact -- reports it as 'unavailable' instead."
        ),
    )
    parser.add_argument("--dataset-id", required=True, help="A Phase 7 Part 2 dataset_id.")
    parser.add_argument("--candidate-id", required=True, help="A Phase 7 Part 2 candidate_id.")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Local dataset root. Defaults to $TRACEX_BENCHMARK_DATA_ROOT.",
    )
    parser.add_argument(
        "--model-cache-root",
        type=Path,
        default=None,
        help="Local model-weight cache root. Defaults to $TRACEX_MODEL_CACHE_ROOT.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Where to write the safe result JSON. Defaults to $TRACEX_BENCHMARK_OUTPUT_ROOT.",
    )
    parser.add_argument(
        "--split-id",
        choices=[member.value for member in SplitId],
        default=SplitId.DEVELOPMENT.value,
        help="Which frozen split this run measures (default: development).",
    )
    parser.add_argument(
        "--model-name",
        default=None,
        help="Verified OCR model name (required for a real, non-unavailable OCR run).",
    )
    parser.add_argument(
        "--model-version",
        default=None,
        help="Verified OCR model version (required for a real, non-unavailable OCR run).",
    )
    parser.add_argument(
        "--model-sha256",
        default=None,
        help="Verified OCR model weight SHA-256 (required for a real, non-unavailable OCR run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    ocr_candidate_artifact: OcrCandidateArtifact | None = None
    if args.model_name and args.model_version and args.model_sha256:
        ocr_candidate_artifact = OcrCandidateArtifact(
            model_name=args.model_name,
            model_version=args.model_version,
            model_sha256=args.model_sha256,
        )

    try:
        run = run_benchmark(
            dataset_id=args.dataset_id,
            candidate_id=args.candidate_id,
            data_root=args.data_root,
            model_cache_root=args.model_cache_root,
            output_root=args.output_root,
            split_id=SplitId(args.split_id),
            ocr_candidate_artifact=ocr_candidate_artifact,
        )
    except (
        UnknownDatasetError,
        UnknownCandidateError,
        UnsupportedCombinationError,
        BenchmarkConfigError,
    ) as exc:
        print(f"benchmark request rejected: {exc}", file=sys.stderr)
        return 2

    print(run.model_dump_json(indent=2))
    if run.status == BenchmarkRunStatus.SUCCEEDED:
        return 0
    if run.status == BenchmarkRunStatus.UNAVAILABLE:
        print(f"\nbenchmark unavailable: {run.failure_reason_safe}", file=sys.stderr)
        return 1
    print(f"\nbenchmark failed: {run.failure_reason_safe}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
