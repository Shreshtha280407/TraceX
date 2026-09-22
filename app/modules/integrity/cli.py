"""Operator CLI for Phase 6 integrity checkpoints.

    uv run python -m app.modules.integrity.cli generate-key
    uv run python -m app.modules.integrity.cli rotate-key
    uv run python -m app.modules.integrity.cli list-keys
    uv run python -m app.modules.integrity.cli build-checkpoint --case-id <uuid> \\
        --start-sequence 1 --end-sequence 50
    uv run python -m app.modules.integrity.cli checkpoint-once
    uv run python -m app.modules.integrity.cli checkpoint-loop \\
        [--poll-interval-seconds 60] [--limit 100]
    uv run python -m app.modules.integrity.cli verify --case-id <uuid> \\
        --checkpoint-id <uuid>
    uv run python -m app.modules.integrity.cli export --case-id <uuid> \\
        --checkpoint-id <uuid> [--out bundle.json]
    uv run python -m app.modules.integrity.cli archive --case-id <uuid> \\
        --checkpoint-id <uuid> [--sink filesystem|minio]

`archive` durably writes the same verification bundle `export` prints, to
a write-once sink (see `manifest_sink.py`) -- for an offsite/immutable copy
independent of PostgreSQL. Refuses to overwrite an existing archived
checkpoint. `--sink filesystem` (default) writes under `Settings.
integrity_manifest_filesystem_root`; `--sink minio` writes to `Settings.
integrity_manifest_bucket` (a bucket separate from raw evidence).

Rotating a key is two steps, deliberately: `generate-key` prints new private
key material for the operator to place in a git-ignored `.env` (along with
a bumped `INTEGRITY_SIGNING_KEY_ID` -- a rotation always uses a new key_id,
never reuses one). Once the new key is live in the running process's
config, `rotate-key` registers *that now-configured* key's public identity
into the durable `signing_keys_public` registry -- it never touches the
private key file/env itself. `list-keys` prints every key_id ever
registered, oldest first, for audit.

`checkpoint-once`/`checkpoint-loop` discover cases with events sealed past
their latest checkpoint (or never checkpointed) and seal each one, one
checkpoint per case per sweep -- the scheduled counterpart to a manual
`build-checkpoint` call. `checkpoint-loop` mirrors `graph.intelligence_
worker.replay_loop`'s shape (bounded exponential backoff, hard stop after
repeated failure, graceful SIGINT/SIGTERM shutdown).

This is the only verification/export surface Phase 6 Part 1 ships. Per the
task's own preference, no broad HTTP endpoint is opened here -- protected
API exposure is Aditya's later work (see
`docs/architecture/phase-6-integrity.md`). `verify` and `export` never print
raw evidence content or private key material; `verify` exits non-zero on a
failed check so it composes in a script or CI gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress
from uuid import UUID

from app.core.config import get_settings
from app.modules.integrity.manifest_sink import (
    FilesystemManifestSink,
    ManifestAlreadyExistsError,
    ManifestSinkError,
    MinioManifestSink,
)
from app.modules.integrity.repository import IntegrityRepository, create_engine
from app.modules.integrity.service import (
    IntegrityService,
    IntegrityValidationError,
    SigningKeyConflictError,
)
from app.modules.integrity.signing import (
    SigningKeyInvalidError,
    SigningKeyNotConfiguredError,
    generate_signing_key_b64,
)

#: Loop-mode-only tuning -- an operator running `checkpoint-once`
#: repeatedly (e.g. from cron) never hits these; they only bound
#: `checkpoint-loop`'s own backoff.
_MAX_CONSECUTIVE_LOOP_FAILURES = 5
_MAX_LOOP_BACKOFF_SECONDS = 300.0


def _build_service() -> tuple[IntegrityService, IntegrityRepository]:
    settings = get_settings()
    engine = create_engine(settings)
    repository = IntegrityRepository(engine)
    return IntegrityService(repository, settings), repository


async def _run_generate_key() -> int:
    print(generate_signing_key_b64())
    print(
        "Copy the line above into your local, git-ignored .env as "
        "INTEGRITY_SIGNING_KEY. Never commit it or log it elsewhere.",
        file=sys.stderr,
    )
    return 0


async def _run_rotate_key() -> int:
    service, repository = _build_service()
    try:
        record, is_new = await service.register_current_signing_key()
    except (SigningKeyNotConfiguredError, SigningKeyInvalidError) as exc:
        print(f"key registration failed: {exc}", file=sys.stderr)
        return 1
    except SigningKeyConflictError as exc:
        print(f"key registration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await repository.close()
    print(
        json.dumps(
            {
                "key_id": record.key_id,
                "public_key_fingerprint": record.public_key_fingerprint,
                "registered_at": record.registered_at.isoformat(),
                "newly_registered": is_new,
            },
            indent=2,
        )
    )
    if not is_new:
        print(
            "This key_id was already registered with this same public key "
            "(no-op). For a real rotation, generate a new key and configure "
            "a new INTEGRITY_SIGNING_KEY_ID first.",
            file=sys.stderr,
        )
    return 0


async def _run_list_keys() -> int:
    service, repository = _build_service()
    try:
        records = await service.list_signing_keys()
    finally:
        await repository.close()
    print(
        json.dumps(
            [
                {
                    "key_id": r.key_id,
                    "algorithm": r.algorithm,
                    "public_key_fingerprint": r.public_key_fingerprint,
                    "registered_at": r.registered_at.isoformat(),
                }
                for r in records
            ],
            indent=2,
        )
    )
    return 0


async def _run_archive(case_id: UUID, checkpoint_id: UUID, sink_name: str) -> int:
    settings = get_settings()
    service, repository = _build_service()
    sink: FilesystemManifestSink | MinioManifestSink
    if sink_name == "filesystem":
        sink = FilesystemManifestSink(settings.integrity_manifest_filesystem_root)
    else:
        minio_sink = MinioManifestSink(settings)
        await minio_sink.ensure_bucket()
        sink = minio_sink
    try:
        locator = await service.archive_verification_bundle(
            checkpoint_id, case_id=case_id, sink=sink
        )
    except IntegrityValidationError as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        return 1
    except ManifestAlreadyExistsError as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        return 1
    except ManifestSinkError as exc:
        print(f"archive failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await repository.close()
    print(json.dumps({"locator": locator}, indent=2))
    return 0


async def _run_build_checkpoint(case_id: UUID, start_sequence: int, end_sequence: int) -> int:
    service, repository = _build_service()
    try:
        receipt = await service.build_checkpoint(
            case_id=case_id, start_sequence=start_sequence, end_sequence=end_sequence
        )
    except (IntegrityValidationError, SigningKeyNotConfiguredError, SigningKeyInvalidError) as exc:
        print(f"checkpoint build failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await repository.close()
    print(
        json.dumps(
            {
                "checkpoint_id": str(receipt.checkpoint.checkpoint_id),
                "root_hash": receipt.checkpoint.root_hash,
                "leaf_count": receipt.checkpoint.leaf_count,
                "start_sequence": receipt.checkpoint.start_sequence,
                "end_sequence": receipt.checkpoint.end_sequence,
                "key_id": receipt.signature.key_id,
                "public_key_fingerprint": receipt.signature.public_key_fingerprint,
                "replayed": receipt.replayed,
            },
            indent=2,
        )
    )
    return 0


async def _run_checkpoint_once(limit: int) -> int:
    service, repository = _build_service()
    try:
        receipts = await service.build_pending_checkpoints(limit=limit)
    except (IntegrityValidationError, SigningKeyNotConfiguredError, SigningKeyInvalidError) as exc:
        print(f"checkpoint sweep failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await repository.close()
    print(
        json.dumps(
            {
                "sealed": [
                    {
                        "case_id": str(r.checkpoint.case_id),
                        "checkpoint_id": str(r.checkpoint.checkpoint_id),
                        "start_sequence": r.checkpoint.start_sequence,
                        "end_sequence": r.checkpoint.end_sequence,
                    }
                    for r in receipts
                ],
                "count": len(receipts),
            },
            indent=2,
        )
    )
    return 0


async def _run_checkpoint_loop(poll_interval_seconds: float, limit: int) -> int:
    """Continuously seal pending checkpoints until SIGINT/SIGTERM.

    Mirrors `graph.intelligence_worker.replay_loop`'s shape: bounded
    exponential backoff on repeated failure, a hard stop after
    `_MAX_CONSECUTIVE_LOOP_FAILURES` so a persistent outage (e.g. a broken
    signing key) doesn't spin the process forever -- an operator restart
    is required after that, exactly like the graph replay loop.
    """
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        print(f"received {sig.name}; stopping after the current sweep", file=sys.stderr)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle, sig)

    consecutive_failures = 0
    while not shutdown_event.is_set():
        service, repository = _build_service()
        try:
            receipts = await service.build_pending_checkpoints(limit=limit)
            consecutive_failures = 0
            if receipts:
                print(f"sealed {len(receipts)} checkpoint(s)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - a scheduled loop must never crash the process
            consecutive_failures += 1
            print(f"checkpoint sweep iteration failed: {exc}", file=sys.stderr)
            if consecutive_failures >= _MAX_CONSECUTIVE_LOOP_FAILURES:
                print("too many consecutive failures; stopping", file=sys.stderr)
                await repository.close()
                return 1
        finally:
            await repository.close()
        backoff = (
            min(poll_interval_seconds * (2**consecutive_failures), _MAX_LOOP_BACKOFF_SECONDS)
            if consecutive_failures
            else poll_interval_seconds
        )
        with suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
    return 0


async def _run_verify(case_id: UUID, checkpoint_id: UUID) -> int:
    service, repository = _build_service()
    try:
        result = await service.verify_checkpoint(checkpoint_id, case_id=case_id)
    finally:
        await repository.close()
    print(
        json.dumps(
            {
                "checkpoint_id": str(result.checkpoint_id),
                "ok": result.ok,
                "leaf_count_matches": result.leaf_count_matches,
                "root_matches": result.root_matches,
                "signature_valid": result.signature_valid,
                "reason": result.reason,
            },
            indent=2,
        )
    )
    return 0 if result.ok else 1


async def _run_export(case_id: UUID, checkpoint_id: UUID, out: str | None) -> int:
    service, repository = _build_service()
    try:
        bundle = await service.export_verification_bundle(checkpoint_id, case_id=case_id)
    except IntegrityValidationError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await repository.close()
    payload = bundle.model_dump(mode="json")
    text = json.dumps(payload, indent=2)
    if out:
        with open(out, "w") as handle:
            handle.write(text)
    else:
        print(text)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("generate-key", help="Generate a dev-only Ed25519 signing key")
    subparsers.add_parser(
        "rotate-key", help="Register the currently configured key's public identity"
    )
    subparsers.add_parser("list-keys", help="List every registered signing key_id")

    build = subparsers.add_parser("build-checkpoint", help="Seal a case-scoped event range")
    build.add_argument("--case-id", type=UUID, required=True)
    build.add_argument("--start-sequence", type=int, required=True)
    build.add_argument("--end-sequence", type=int, required=True)

    checkpoint_once = subparsers.add_parser(
        "checkpoint-once", help="Seal every case with pending events, once"
    )
    checkpoint_once.add_argument("--limit", type=int, default=100)

    checkpoint_loop = subparsers.add_parser(
        "checkpoint-loop", help="Seal pending checkpoints continuously until SIGINT/SIGTERM"
    )
    checkpoint_loop.add_argument("--poll-interval-seconds", type=float, default=60.0)
    checkpoint_loop.add_argument("--limit", type=int, default=100)

    verify = subparsers.add_parser("verify", help="Independently verify a checkpoint")
    verify.add_argument("--case-id", type=UUID, required=True)
    verify.add_argument("--checkpoint-id", type=UUID, required=True)

    export = subparsers.add_parser("export", help="Export a safe verification bundle")
    export.add_argument("--case-id", type=UUID, required=True)
    export.add_argument("--checkpoint-id", type=UUID, required=True)
    export.add_argument("--out", type=str, default=None)

    archive = subparsers.add_parser(
        "archive", help="Durably, write-once archive a verification bundle"
    )
    archive.add_argument("--case-id", type=UUID, required=True)
    archive.add_argument("--checkpoint-id", type=UUID, required=True)
    archive.add_argument("--sink", choices=("filesystem", "minio"), default="filesystem")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "generate-key":
        return asyncio.run(_run_generate_key())
    if args.command == "rotate-key":
        return asyncio.run(_run_rotate_key())
    if args.command == "list-keys":
        return asyncio.run(_run_list_keys())
    if args.command == "build-checkpoint":
        return asyncio.run(
            _run_build_checkpoint(args.case_id, args.start_sequence, args.end_sequence)
        )
    if args.command == "checkpoint-once":
        return asyncio.run(_run_checkpoint_once(args.limit))
    if args.command == "checkpoint-loop":
        return asyncio.run(_run_checkpoint_loop(args.poll_interval_seconds, args.limit))
    if args.command == "verify":
        return asyncio.run(_run_verify(args.case_id, args.checkpoint_id))
    if args.command == "export":
        return asyncio.run(_run_export(args.case_id, args.checkpoint_id, args.out))
    if args.command == "archive":
        return asyncio.run(_run_archive(args.case_id, args.checkpoint_id, args.sink))
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
