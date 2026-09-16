"""Phase 7 Part 4 audio/social-benchmark CLI: list candidates, validate, run.

    uv run python -m app.modules.communication_processing.audio_social_benchmark_cli list-candidates
    uv run python -m app.modules.communication_processing.audio_social_benchmark_cli validate \\
        --dataset-id common_voice_indic --candidate-id faster-whisper-small
    uv run python -m app.modules.communication_processing.audio_social_benchmark_cli run \\
        --dataset-id vast_social_text --candidate-id existing-deterministic-social-parsers \\
        --data-root "$TRACEX_BENCHMARK_DATA_ROOT" \\
        --output-root "$TRACEX_BENCHMARK_OUTPUT_ROOT"

Every subcommand's output is aggregate/safe JSON only -- never a raw
transcript, message, participant name, phone number, handle, speaker
label, credential, Hugging Face token, evidence URI, or full local path.
`run`'s exit code is `0` for a `succeeded` result, `1` for a truthful
`unavailable`/`failed` result, and `2` when the request itself is
rejected (unknown ID, unsupported pairing, or missing local-root
configuration) -- a caller bug, not a benchmark outcome. Runs as a local
host process -- never through Docker Compose.

`fasttext-lid176` additionally requires `--transcription-stage-model-name`/
`--transcription-stage-model-version`/`--transcription-stage-model-sha256`
(its chained faster-whisper transcription stage, see
`docs/runbooks/local-development.md`) -- a completed result is rejected
without them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.modules.communication_processing.audio_social_benchmark import (
    VerifiedModelArtifact,
    run_benchmark,
)
from app.modules.communication_processing.audio_social_benchmark_validation import (
    ALLOWED_CANDIDATE_IDS,
    ALLOWED_DATASET_CANDIDATE_PAIRS,
    ALLOWED_DATASET_IDS,
    BenchmarkConfigError,
    LicenseNotClearedError,
    require_cleared_for_real_execution,
    resolve_benchmark_data_root,
    resolve_model_cache_root,
    validate_candidate_id,
    validate_dataset_candidate_pair,
    validate_dataset_id,
)
from app.modules.evaluation.models import SplitId


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.communication_processing.audio_social_benchmark_cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list-candidates", help="List this harness's approved dataset/candidate pairs."
    )

    def _add_request_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--dataset-id", required=True)
        sub.add_argument("--candidate-id", required=True)
        sub.add_argument("--data-root", type=Path, default=None)
        sub.add_argument("--model-cache-root", type=Path, default=None)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate the request and local configuration without running anything."
    )
    _add_request_args(validate_parser)

    run_parser = subparsers.add_parser("run", help="Run one local benchmark.")
    _add_request_args(run_parser)
    run_parser.add_argument("--output-root", type=Path, default=None)
    run_parser.add_argument(
        "--split-id", default=SplitId.DEVELOPMENT.value, choices=[s.value for s in SplitId]
    )
    run_parser.add_argument("--model-name", default=None)
    run_parser.add_argument("--model-version", default=None)
    run_parser.add_argument("--model-sha256", default=None)
    run_parser.add_argument(
        "--transcription-stage-model-name",
        default=None,
        help="Only used by fasttext-lid176's chained transcription stage.",
    )
    run_parser.add_argument("--transcription-stage-model-version", default=None)
    run_parser.add_argument("--transcription-stage-model-sha256", default=None)
    return parser


def _list_candidates() -> int:
    payload = {
        "allowed_dataset_ids": sorted(ALLOWED_DATASET_IDS),
        "allowed_candidate_ids": sorted(ALLOWED_CANDIDATE_IDS),
        "allowed_dataset_candidate_pairs": sorted(ALLOWED_DATASET_CANDIDATE_PAIRS),
    }
    print(json.dumps(payload, indent=2))
    return 0


def _validate(args: argparse.Namespace) -> int:
    from app.modules.evaluation.catalog import load_model_candidate_catalog
    from app.modules.evaluation.manifest import load_dataset_manifest

    try:
        manifest = load_dataset_manifest()
        catalog = load_model_candidate_catalog()
        dataset = validate_dataset_id(manifest, args.dataset_id)
        candidate = validate_candidate_id(catalog, args.candidate_id)
        validate_dataset_candidate_pair(dataset, candidate)
        data_root = resolve_benchmark_data_root(args.data_root)
        model_cache_root = resolve_model_cache_root(args.model_cache_root)
    except BenchmarkConfigError as exc:
        print(f"request rejected: {exc}", file=sys.stderr)
        return 2

    cleared = True
    reason: str | None = None
    try:
        require_cleared_for_real_execution(dataset, candidate)
    except LicenseNotClearedError as exc:
        cleared = False
        reason = str(exc)

    from app.modules.communication_processing.audio_social_benchmark import dataset_subdirectory

    dataset_dir = dataset_subdirectory(dataset, data_root)
    payload = {
        "dataset_id": dataset.dataset_id,
        "candidate_id": candidate.candidate_id,
        "task": candidate.task.value,
        "dataset_directory_exists": dataset_dir.is_dir(),
        "model_cache_root_configured": model_cache_root is not None,
        "license_and_conditional_status_cleared_for_real_execution": cleared,
        "block_reason_safe": reason,
    }
    print(json.dumps(payload, indent=2))
    return 0 if cleared and dataset_dir.is_dir() else 1


def _run(args: argparse.Namespace) -> int:
    verified_model_artifact = None
    if args.model_name and args.model_version and args.model_sha256:
        verified_model_artifact = VerifiedModelArtifact(
            model_name=args.model_name,
            model_version=args.model_version,
            model_sha256=args.model_sha256,
        )
    verified_transcription_stage_artifact = None
    if (
        args.transcription_stage_model_name
        and args.transcription_stage_model_version
        and args.transcription_stage_model_sha256
    ):
        verified_transcription_stage_artifact = VerifiedModelArtifact(
            model_name=args.transcription_stage_model_name,
            model_version=args.transcription_stage_model_version,
            model_sha256=args.transcription_stage_model_sha256,
        )
    try:
        run = run_benchmark(
            dataset_id=args.dataset_id,
            candidate_id=args.candidate_id,
            data_root=args.data_root,
            model_cache_root=args.model_cache_root,
            output_root=args.output_root,
            split_id=SplitId(args.split_id),
            verified_model_artifact=verified_model_artifact,
            verified_transcription_stage_artifact=verified_transcription_stage_artifact,
        )
    except BenchmarkConfigError as exc:
        print(f"request rejected: {exc}", file=sys.stderr)
        return 2

    print(run.model_dump_json(indent=2))
    if run.status.value == "succeeded":
        return 0
    print(f"benchmark {run.status.value}: {run.failure_reason_safe}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-candidates":
        return _list_candidates()
    if args.command == "validate":
        return _validate(args)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
