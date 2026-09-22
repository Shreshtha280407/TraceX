"""First-admin bootstrap CLI (G5).

    uv run python -m app.modules.access_control.cli create-admin --email a@example.test

Reads the password from `TRACEX_ADMIN_BOOTSTRAP_PASSWORD` if set, otherwise
prompts interactively via `getpass` -- never accepted as a CLI argument and
never logged. Idempotent: refuses (exit 1, no user created) if an active
admin already exists, unless `--force` is passed. Prints only the created
user's ID on success -- never the password or its hash.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import get_settings
from app.modules.access_control.audit import record_audit_event
from app.modules.access_control.models import AuditOutcome, SystemRole, UserRecord, normalize_email
from app.modules.access_control.password import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
)
from app.modules.access_control.repository import AccessControlRepository, create_engine

_PASSWORD_ENV_VAR = "TRACEX_ADMIN_BOOTSTRAP_PASSWORD"


async def provision_admin(
    repository: AccessControlRepository,
    email: str,
    password: str,
    display_name: str,
    *,
    force: bool,
) -> int:
    """Injectable core logic (a real repository in production, a fake in tests)."""
    existing_admins = await repository.count_users_with_system_role(SystemRole.ADMIN.value)
    if existing_admins > 0 and not force:
        print(
            f"refused: {existing_admins} active admin(s) already exist; "
            "pass --force to add another",
            file=sys.stderr,
        )
        return 1

    try:
        normalized_email = normalize_email(email)
    except ValueError:
        print("refused: invalid email address", file=sys.stderr)
        return 1

    if await repository.get_user_by_email(normalized_email) is not None:
        print("refused: a user with this email already exists", file=sys.stderr)
        return 1

    if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
        print(
            f"refused: password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(UTC)
    user = UserRecord(
        user_id=uuid4(),
        email_normalized=normalized_email,
        display_name=display_name,
        password_hash=hash_password(password),
        is_active=True,
        created_at=now,
        updated_at=now,
        system_role=SystemRole.ADMIN,
    )
    try:
        await repository.create_user(user)
    except Exception:  # noqa: BLE001 - a safe CLI failure code, never a raw exception to stdout
        print("refused: could not create user (see server logs)", file=sys.stderr)
        return 1

    await record_audit_event(
        repository,
        event_type="admin.bootstrap_create_admin",
        outcome=AuditOutcome.SUCCESS,
        now=now,
        user_id=user.user_id,
    )
    print(str(user.user_id))
    return 0


async def _create_admin(email: str, password: str, display_name: str, *, force: bool) -> int:
    settings = get_settings()
    engine = create_engine(settings)
    repository = AccessControlRepository(engine)
    try:
        return await provision_admin(repository, email, password, display_name, force=force)
    finally:
        await repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.access_control.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin = subparsers.add_parser("create-admin", help="Bootstrap the first admin user.")
    create_admin.add_argument("--email", required=True)
    create_admin.add_argument("--display-name", default="Administrator")
    create_admin.add_argument(
        "--force",
        action="store_true",
        help="Create another admin even if one already exists.",
    )

    args = parser.parse_args(argv)
    if args.command != "create-admin":
        parser.error(f"unknown command: {args.command}")

    password = os.environ.get(_PASSWORD_ENV_VAR)
    if not password:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("refused: passwords did not match", file=sys.stderr)
            return 1

    return asyncio.run(_create_admin(args.email, password, args.display_name, force=args.force))


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "provision_admin"]
