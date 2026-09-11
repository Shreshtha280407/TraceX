"""Trusted-operator worker-credential provisioning: hashing, pepper policy, and CLI.

Replaces the temporary `WORKER_SHARED_SECRET` boundary (see
`docs/architecture/worker-job-lifecycle.md`'s now-resolved "Worker identity"
section) with revocable, per-worker service identities. This module is the
*only* way a worker credential is ever created, rotated, or revoked in this
codebase -- there is deliberately no public HTTP API for any of it (see
`docs/architecture/worker-identity-and-security.md`).

    uv run python -m app.modules.access_control.worker_credentials create \\
        --name structured-worker \\
        --processor fir_report_text_v1 --processor cdr_generic_v1

Trust model: this CLI runs wherever the server's own PostgreSQL
configuration is already available (a developer machine, a deployment
bastion, CI provisioning step) -- the same trust boundary an operator
running `alembic upgrade head` already has. It is not, and must never
become, a network-reachable endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.config import AppEnv, Settings, get_settings
from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.errors import WorkerSecurityConfigurationError
from app.modules.access_control.models import (
    AuditOutcome,
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)
from app.modules.access_control.repository import AccessControlRepository, create_engine

#: Bytes of entropy for a generated worker credential (256 bits) -- mirrors
#: `access_control.tokens.REFRESH_TOKEN_BYTES`'s exact reasoning: a
#: high-entropy opaque secret, not a human-chosen password.
WORKER_TOKEN_BYTES = 32


def generate_worker_token() -> str:
    """A fresh, high-entropy worker credential. Never stored raw -- see `hash_worker_credential`.

    Callers must treat the return value the same as a password: print it to
    the local terminal exactly once, never log it, never persist it.
    """
    return secrets.token_urlsafe(WORKER_TOKEN_BYTES)


def resolve_worker_pepper(settings: Settings) -> str | None:
    """The effective server-side pepper for worker-credential digests, or `None`.

    `None` is an accepted, documented state in local/development/test
    environments (`hash_worker_credential` falls back to an unkeyed
    SHA-256 digest -- the same "high-entropy secret, fast hash is fine"
    reasoning `access_control.tokens.hash_refresh_token` already
    documents). In a production-like environment, a missing pepper is a
    deployment misconfiguration, not an acceptable default: raises
    `WorkerSecurityConfigurationError` with a clear, non-secret message
    naming the missing setting, never a stack trace or a guessed fallback.
    """
    pepper = settings.worker_credential_pepper
    if pepper is not None:
        return pepper.get_secret_value()
    if settings.app_env is AppEnv.PRODUCTION:
        raise WorkerSecurityConfigurationError(
            "WORKER_CREDENTIAL_PEPPER is required when APP_ENV=production but is not configured"
        )
    return None


def hash_worker_credential(token: str, pepper: str | None) -> str:
    """`HMAC-SHA256(pepper, token)` hex digest, or plain SHA-256 when no pepper is configured.

    Peppering (rather than a plain unsalted/unkeyed hash, as
    `access_control.tokens.hash_refresh_token` uses for refresh tokens) is
    the one extra defense-in-depth layer worker credentials specifically
    get: a leaked `worker_credentials.credential_digest` column alone is
    not enough to impersonate a worker without also knowing the
    server-side pepper, which never leaves `Settings`/environment
    configuration. This function must be called identically at credential
    creation/rotation time and at authentication-verification time -- see
    `docs/architecture/worker-identity-and-security.md`'s note on why
    changing pepper policy invalidates every existing credential.
    """
    if pepper:
        return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_worker_credential(
    repository: AccessControlRepository,
    *,
    display_name: str,
    allowed_processor_names: Sequence[str],
    pepper: str | None,
    now: datetime,
) -> tuple[WorkerCredentialRecord, str]:
    """Provision one new worker credential. Returns `(record, plaintext_token)`.

    The plaintext token is returned exactly once, to the immediate caller
    only -- never persisted, logged, or returned a second time by any
    other function in this module.
    """
    token = generate_worker_token()
    record = WorkerCredentialRecord(
        worker_id=uuid4(),
        display_name=display_name,
        status=WorkerCredentialStatus.ACTIVE,
        allowed_processor_names=tuple(allowed_processor_names),
        credential_digest=hash_worker_credential(token, pepper),
        created_at=now,
        rotated_at=None,
        revoked_at=None,
    )
    await repository.create_worker_credential(record)
    return record, token


async def rotate_worker_credential(
    repository: AccessControlRepository, *, worker_id: UUID, pepper: str | None, now: datetime
) -> str:
    """Issue a fresh token for an existing, active worker identity.

    Overwrites `credential_digest` -- the old token's digest immediately
    stops matching anything, so it can never authenticate again the moment
    this returns. `worker_id`, `display_name`, and
    `allowed_processor_names` are all unchanged, so any job already bound
    to this `worker_id` (`worker_jobs.claimed_by_worker_id`) stays valid
    across the rotation.

    Raises `LookupError` for an unknown `worker_id`, or `ValueError` for a
    revoked one (rotating a revoked credential is almost certainly a
    mistake -- provision a new one instead). Records a `worker_credential_rotated`
    audit event (safe fields only: `worker_id`, never the old or new token)
    once the rotation itself has succeeded.
    """
    existing = await repository.get_worker_credential_by_id(worker_id)
    if existing is None:
        raise LookupError(f"no worker credential found for worker_id {worker_id}")
    if existing.status is WorkerCredentialStatus.REVOKED:
        raise ValueError(f"worker_id {worker_id} is revoked -- provision a new credential instead")
    token = generate_worker_token()
    await repository.rotate_worker_credential(
        worker_id, credential_digest=hash_worker_credential(token, pepper), rotated_at=now
    )
    await record_audit_event(
        repository,
        event_type="worker_credential_rotated",
        outcome=AuditOutcome.SUCCESS,
        now=now,
        metadata={"worker_id": str(worker_id)},
    )
    return token


async def revoke_worker_credential(
    repository: AccessControlRepository, *, worker_id: UUID, now: datetime
) -> None:
    """Immediately deny a worker credential. Idempotent -- safe to call more than once.

    Records a `worker_credential_revoked` audit event (safe fields only:
    `worker_id`) every time this is called, including a redundant call
    against an already-revoked credential -- an operator re-running
    `revoke` is itself worth an audit trail entry, even though the
    underlying database state doesn't change.
    """
    await repository.revoke_worker_credential(worker_id, now)
    await record_audit_event(
        repository,
        event_type="worker_credential_revoked",
        outcome=AuditOutcome.SUCCESS,
        now=now,
        metadata={"worker_id": str(worker_id)},
    )


async def list_worker_credentials(
    repository: AccessControlRepository,
) -> list[WorkerCredentialRecord]:
    """Every provisioned worker credential -- never a plaintext token or digest to the caller.

    `WorkerCredentialRecord.credential_digest` is present on the returned
    records (it is a full row, for internal use), but the CLI's `list`
    output built from it deliberately omits that field -- see `_print_list`.
    """
    return await repository.list_worker_credentials()


def _print_created(record: WorkerCredentialRecord, token: str) -> None:
    print(f"worker_id:                {record.worker_id}")
    print(f"display_name:             {record.display_name}")
    print(f"allowed_processor_names:  {', '.join(record.allowed_processor_names)}")
    print("token (shown once -- store it now, never in Git/.env.example/logs):")
    print(f"  {token}")


def _print_rotated(worker_id: UUID, token: str) -> None:
    print(f"worker_id: {worker_id}")
    print("new token (shown once -- store it now, never in Git/.env.example/logs):")
    print(f"  {token}")


def _print_list(records: list[WorkerCredentialRecord]) -> None:
    if not records:
        print("no worker credentials provisioned")
        return
    for record in records:
        rotated = record.rotated_at.isoformat() if record.rotated_at else "-"
        revoked = record.revoked_at.isoformat() if record.revoked_at else "-"
        print(
            f"{record.worker_id}  status={record.status.value:<8}  "
            f"name={record.display_name!r}  "
            f"processors=[{','.join(record.allowed_processor_names)}]  "
            f"created_at={record.created_at.isoformat()}  rotated_at={rotated}  "
            f"revoked_at={revoked}"
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.access_control.worker_credentials",
        description=(
            "Trusted-operator worker-credential provisioning. Never exposed via a public "
            "API -- run only where the server's own PostgreSQL configuration is available."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Provision a new worker credential.")
    create_parser.add_argument("--name", required=True, help="Human-readable display name.")
    create_parser.add_argument(
        "--processor",
        action="append",
        required=True,
        dest="processors",
        metavar="PROCESSOR_NAME",
        help="A processor_name this worker may claim. Repeatable.",
    )

    rotate_parser = subparsers.add_parser(
        "rotate", help="Issue a new token for an existing worker, invalidating the old one."
    )
    rotate_parser.add_argument("--worker-id", required=True, type=UUID)

    revoke_parser = subparsers.add_parser(
        "revoke", help="Immediately deny a worker credential. Idempotent."
    )
    revoke_parser.add_argument("--worker-id", required=True, type=UUID)

    subparsers.add_parser(
        "list", help="List provisioned worker credentials (never a token or digest)."
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        pepper = resolve_worker_pepper(settings)
    except WorkerSecurityConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    repository = AccessControlRepository(create_engine(settings))
    now = datetime.now(UTC)
    try:
        if args.command == "create":
            record, token = await create_worker_credential(
                repository,
                display_name=args.name,
                allowed_processor_names=args.processors,
                pepper=pepper,
                now=now,
            )
            _print_created(record, token)
            return 0
        if args.command == "rotate":
            try:
                token = await rotate_worker_credential(
                    repository, worker_id=args.worker_id, pepper=pepper, now=now
                )
            except (LookupError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            _print_rotated(args.worker_id, token)
            return 0
        if args.command == "revoke":
            await revoke_worker_credential(repository, worker_id=args.worker_id, now=now)
            print(f"worker_id {args.worker_id}: revoked (idempotent)")
            return 0
        if args.command == "list":
            _print_list(await list_worker_credentials(repository))
            return 0
        return 1  # pragma: no cover - argparse subparsers(required=True) makes this unreachable
    finally:
        await repository.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    sys.exit(main())
