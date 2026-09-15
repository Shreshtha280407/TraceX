"""Operator CLI for Phase 6 integrity checkpoints.

    uv run python -m app.modules.integrity.cli generate-key
    uv run python -m app.modules.integrity.cli build-checkpoint --case-id <uuid> \\
        --start-sequence 1 --end-sequence 50
    uv run python -m app.modules.integrity.cli verify --case-id <uuid> \\
        --checkpoint-id <uuid>
    uv run python -m app.modules.integrity.cli export --case-id <uuid> \\
        --checkpoint-id <uuid> [--out bundle.json]

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
import sys
from collections.abc import Sequence
from uuid import UUID

from app.core.config import get_settings
from app.modules.integrity.repository import IntegrityRepository, create_engine
from app.modules.integrity.service import IntegrityService, IntegrityValidationError
from app.modules.integrity.signing import (
    SigningKeyInvalidError,
    SigningKeyNotConfiguredError,
    generate_signing_key_b64,
)


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

    build = subparsers.add_parser("build-checkpoint", help="Seal a case-scoped event range")
    build.add_argument("--case-id", type=UUID, required=True)
    build.add_argument("--start-sequence", type=int, required=True)
    build.add_argument("--end-sequence", type=int, required=True)

    verify = subparsers.add_parser("verify", help="Independently verify a checkpoint")
    verify.add_argument("--case-id", type=UUID, required=True)
    verify.add_argument("--checkpoint-id", type=UUID, required=True)

    export = subparsers.add_parser("export", help="Export a safe verification bundle")
    export.add_argument("--case-id", type=UUID, required=True)
    export.add_argument("--checkpoint-id", type=UUID, required=True)
    export.add_argument("--out", type=str, default=None)

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "generate-key":
        return asyncio.run(_run_generate_key())
    if args.command == "build-checkpoint":
        return asyncio.run(
            _run_build_checkpoint(args.case_id, args.start_sequence, args.end_sequence)
        )
    if args.command == "verify":
        return asyncio.run(_run_verify(args.case_id, args.checkpoint_id))
    if args.command == "export":
        return asyncio.run(_run_export(args.case_id, args.checkpoint_id, args.out))
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
