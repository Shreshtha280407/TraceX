"""Typed async PostgreSQL repository for access control.

This is the only module in `app/modules/access_control/` that touches
SQLAlchemy/PostgreSQL directly. Every statement is built with SQLAlchemy
Core's expression language (`sa.select`/`sa.insert`/`sa.update`) -- values
are always bound parameters, never interpolated into SQL text.

The `Table` objects below intentionally duplicate the column list in
`migrations/versions/7e8499f34f29_access_control_foundation.py` by hand
(this module does not import the migration, and `migrations/env.py` keeps
`target_metadata = None`, Nipun's baseline decision) -- see the note at the
top of that migration. Keep the two in sync when either changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    SecurityAuditEventRecord,
    SessionRecord,
    UserRecord,
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)


def _dump_for_insert(model: BaseModel, enum_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    """`model.model_dump(mode="python")`, with named enum fields reduced to their plain `.value`.

    `mode="python"` is otherwise exactly what asyncpg wants (native
    `datetime`/`UUID` objects, not ISO/hex strings) -- but it leaves `StrEnum`
    fields as enum members. They *are* `str` instances, so this may be
    unnecessary in practice, but binding the plain string explicitly avoids
    depending on that being true across asyncpg/SQLAlchemy versions.
    """
    values = model.model_dump(mode="python")
    for field in enum_fields:
        values[field] = values[field].value
    return values


metadata = sa.MetaData()

users_table = sa.Table(
    "users",
    metadata,
    sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("email_normalized", sa.Text(), nullable=False),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("password_hash", sa.Text(), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("system_role", sa.Text(), nullable=True),
    sa.Column("must_change_password", sa.Boolean(), nullable=False),
    sa.Column("totp_secret", sa.Text(), nullable=True),
    sa.Column("totp_enabled", sa.Boolean(), nullable=False),
)

cases_table = sa.Table(
    "cases",
    metadata,
    sa.Column("case_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_reference", sa.Text(), nullable=False),
    sa.Column("classification", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

case_memberships_table = sa.Table(
    "case_memberships",
    metadata,
    sa.Column("membership_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("role", sa.Text(), nullable=False),
    sa.Column("clearance", sa.Text(), nullable=False),
    sa.Column("is_active", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

auth_sessions_table = sa.Table(
    "auth_sessions",
    metadata,
    sa.Column("session_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("refresh_token_hash", sa.Text(), nullable=False),
    sa.Column("token_family_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("replaced_by_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
)

worker_credentials_table = sa.Table(
    "worker_credentials",
    metadata,
    sa.Column("worker_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("display_name", sa.Text(), nullable=False),
    sa.Column("status", sa.Text(), nullable=False),
    sa.Column("allowed_processor_names", postgresql.JSONB(), nullable=False),
    sa.Column("credential_digest", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
)

security_audit_events_table = sa.Table(
    "security_audit_events",
    metadata,
    sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("event_type", sa.Text(), nullable=False),
    sa.Column("outcome", sa.Text(), nullable=False),
    sa.Column("request_id", sa.Text(), nullable=True),
    sa.Column("user_id_nullable", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("case_id_nullable", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column("ip_hash_or_safe_network_marker", sa.Text(), nullable=True),
    sa.Column("metadata_safe_json", postgresql.JSONB(), nullable=False),
)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async PostgreSQL engine from application configuration."""
    return create_async_engine(str(settings.postgres_dsn))


def _user_from_row(row: sa.RowMapping) -> UserRecord:
    return UserRecord.model_validate(dict(row))


def _case_from_row(row: sa.RowMapping) -> CaseRecord:
    return CaseRecord.model_validate(dict(row))


def _membership_from_row(row: sa.RowMapping) -> CaseMembershipRecord:
    return CaseMembershipRecord.model_validate(dict(row))


def _session_from_row(row: sa.RowMapping) -> SessionRecord:
    return SessionRecord.model_validate(dict(row))


def _audit_event_from_row(row: sa.RowMapping) -> SecurityAuditEventRecord:
    return SecurityAuditEventRecord.model_validate(dict(row))


def _worker_credential_from_row(row: sa.RowMapping) -> WorkerCredentialRecord:
    return WorkerCredentialRecord.model_validate(dict(row))


class AccessControlRepository:
    """Typed async persistence for users, cases, memberships, sessions, and audit events.

    Accepts an injected `AsyncEngine` (see `create_engine`) so tests can
    point it at a real ephemeral test database; there is no in-memory fake
    for this repository -- the integration tests self-skip instead when
    PostgreSQL isn't reachable (see `tests/integration/access_control/`).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def close(self) -> None:
        await self._engine.dispose()

    # --- users ---------------------------------------------------------

    async def create_user(self, user: UserRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(users_table).values(**user.model_dump(mode="python")))

    async def get_user_by_email(self, email_normalized: str) -> UserRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(users_table).where(
                            users_table.c.email_normalized == email_normalized
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _user_from_row(row) if row is not None else None

    async def get_user_by_id(self, user_id: UUID) -> UserRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(sa.select(users_table).where(users_table.c.user_id == user_id)))
                .mappings()
                .first()
            )
        return _user_from_row(row) if row is not None else None

    async def list_users(self, *, limit: int, offset: int) -> list[UserRecord]:
        """Admin-only account listing (`GET /api/v1/admin/users`), oldest first.

        Bounded like every other list method in this module (`list_case_audit_events`,
        `list_worker_credentials`) -- never an unbounded `SELECT *`.
        """
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(users_table)
                        .order_by(users_table.c.created_at.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
        return [_user_from_row(row) for row in rows]

    async def update_password(
        self, user_id: UUID, *, password_hash: str, must_change_password: bool, updated_at: datetime
    ) -> None:
        """Set a new password hash and the forced-change flag together (`change_password`,
        `admin_reset_credentials`) -- always updated atomically, never one without the other."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(users_table)
                .where(users_table.c.user_id == user_id)
                .values(
                    password_hash=password_hash,
                    must_change_password=must_change_password,
                    updated_at=updated_at,
                )
            )

    async def update_totp(
        self, user_id: UUID, *, totp_secret: str | None, totp_enabled: bool, updated_at: datetime
    ) -> None:
        """Set the TOTP secret and enabled flag together (`enroll_mfa`,
        `confirm_mfa_enrollment`, `admin_reset_credentials`)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(users_table)
                .where(users_table.c.user_id == user_id)
                .values(totp_secret=totp_secret, totp_enabled=totp_enabled, updated_at=updated_at)
            )

    async def count_users_with_system_role(self, system_role: str) -> int:
        """Bootstrap-CLI idempotency check: how many active admins already exist."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(sa.func.count())
                .select_from(users_table)
                .where(users_table.c.system_role == system_role, users_table.c.is_active.is_(True))
            )
            return int(result.scalar_one())

    # --- cases / memberships (minimal access-control anchor; no case CRUD API) --

    async def create_case(self, case: CaseRecord) -> None:
        values = _dump_for_insert(case, ("classification", "status"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(cases_table).values(**values))

    async def get_case(self, case_id: UUID) -> CaseRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(sa.select(cases_table).where(cases_table.c.case_id == case_id)))
                .mappings()
                .first()
            )
        return _case_from_row(row) if row is not None else None

    async def create_membership(self, membership: CaseMembershipRecord) -> None:
        values = _dump_for_insert(membership, ("role", "clearance"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(case_memberships_table).values(**values))

    async def get_active_membership(
        self, case_id: UUID, user_id: UUID
    ) -> CaseMembershipRecord | None:
        """The exact-case, exact-user, active membership row -- or `None`.

        Deliberately requires an exact `(case_id, user_id)` match: there is
        no notion of a globally selected "current case" or a membership
        that applies across cases (see `policy.py`).
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(case_memberships_table).where(
                            case_memberships_table.c.case_id == case_id,
                            case_memberships_table.c.user_id == user_id,
                            case_memberships_table.c.is_active.is_(True),
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _membership_from_row(row) if row is not None else None

    async def list_active_memberships_for_user(self, user_id: UUID) -> list[CaseMembershipRecord]:
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(case_memberships_table).where(
                            case_memberships_table.c.user_id == user_id,
                            case_memberships_table.c.is_active.is_(True),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_membership_from_row(row) for row in rows]

    # --- sessions --------------------------------------------------------

    async def create_session(self, session: SessionRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(auth_sessions_table).values(**session.model_dump(mode="python"))
            )

    async def get_session_by_id(self, session_id: UUID) -> SessionRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(auth_sessions_table).where(
                            auth_sessions_table.c.session_id == session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _session_from_row(row) if row is not None else None

    async def get_session_by_refresh_token_hash(
        self, refresh_token_hash: str
    ) -> SessionRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(auth_sessions_table).where(
                            auth_sessions_table.c.refresh_token_hash == refresh_token_hash
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _session_from_row(row) if row is not None else None

    async def rotate_session(
        self,
        *,
        old_session_id: UUID,
        new_session: SessionRecord,
        revoked_at: datetime,
    ) -> None:
        """Atomically insert the replacement session, then revoke `old_session_id` onto it.

        Insert must happen first: `auth_sessions.replaced_by_session_id`
        has a foreign key onto `auth_sessions.session_id`, checked
        immediately (this table declares no `DEFERRABLE` constraints), so
        pointing the old row at a not-yet-inserted new row would fail even
        inside the same transaction.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(auth_sessions_table).values(**new_session.model_dump(mode="python"))
            )
            await conn.execute(
                sa.update(auth_sessions_table)
                .where(auth_sessions_table.c.session_id == old_session_id)
                .values(revoked_at=revoked_at, replaced_by_session_id=new_session.session_id)
            )

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(auth_sessions_table)
                .where(auth_sessions_table.c.session_id == session_id)
                .where(auth_sessions_table.c.revoked_at.is_(None))
                .values(revoked_at=revoked_at)
            )

    async def revoke_all_sessions_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        """Lost-device recovery (`admin_reset_credentials`): kill every live session for
        this user immediately, regardless of token family -- a reset must not leave an
        old, already-issued access/refresh token pair usable after the fact."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(auth_sessions_table)
                .where(auth_sessions_table.c.user_id == user_id)
                .where(auth_sessions_table.c.revoked_at.is_(None))
                .values(revoked_at=revoked_at)
            )

    async def revoke_family(self, token_family_id: UUID, revoked_at: datetime) -> None:
        """Revoke every not-yet-revoked session in a token family (reuse-detection response)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(auth_sessions_table)
                .where(auth_sessions_table.c.token_family_id == token_family_id)
                .where(auth_sessions_table.c.revoked_at.is_(None))
                .values(revoked_at=revoked_at)
            )

    async def touch_session_last_used(self, session_id: UUID, last_used_at: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(auth_sessions_table)
                .where(auth_sessions_table.c.session_id == session_id)
                .values(last_used_at=last_used_at)
            )

    # --- audit -------------------------------------------------------------

    async def record_audit_event(self, event: SecurityAuditEventRecord) -> None:
        values = _dump_for_insert(event, ("outcome",))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(security_audit_events_table).values(**values))

    async def get_audit_event_by_id(self, event_id: UUID) -> SecurityAuditEventRecord | None:
        """Test/inspection helper."""
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(security_audit_events_table).where(
                            security_audit_events_table.c.event_id == event_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _audit_event_from_row(row) if row is not None else None

    async def list_audit_events_for_case(
        self, case_id: UUID, *, limit: int | None = None
    ) -> list[SecurityAuditEventRecord]:
        """Gap-Closure WP-4 (G7): the durable boundary behind `GET /cases/{id}/audit`.

        Bounded, case-scoped, newest-first -- `metadata_safe_json` is
        already write-time-scrubbed by `audit.record_audit_event`'s own
        contract, so nothing here needs to filter it again.
        """
        if limit is not None and not 1 <= limit <= 200:
            raise ValueError("audit event query limit must be between 1 and 200")
        statement = (
            sa.select(security_audit_events_table)
            .where(security_audit_events_table.c.case_id_nullable == case_id)
            .order_by(security_audit_events_table.c.occurred_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(statement)).mappings().all()
        return [_audit_event_from_row(row) for row in rows]

    # --- worker credentials --------------------------------------------------

    async def create_worker_credential(self, credential: WorkerCredentialRecord) -> None:
        values = _dump_for_insert(credential, ("status",))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(worker_credentials_table).values(**values))

    async def get_worker_credential_by_id(self, worker_id: UUID) -> WorkerCredentialRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_credentials_table).where(
                            worker_credentials_table.c.worker_id == worker_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _worker_credential_from_row(row) if row is not None else None

    async def get_worker_credential_by_digest(
        self, credential_digest: str
    ) -> WorkerCredentialRecord | None:
        """Plain equality lookup on an already-indexed digest column.

        No `hmac.compare_digest`-style constant-time comparison is needed
        here: the digest is a lookup key into a database index, not a
        caller-observable timing channel the way comparing a submitted
        value directly against a stored secret would be -- the identical
        reasoning `get_session_by_refresh_token_hash` already applies.
        """
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(worker_credentials_table).where(
                            worker_credentials_table.c.credential_digest == credential_digest
                        )
                    )
                )
                .mappings()
                .first()
            )
        return _worker_credential_from_row(row) if row is not None else None

    async def touch_worker_last_seen(self, worker_id: UUID, seen_at: datetime) -> None:
        """Gap-Closure WP-6 (G16): called best-effort by `require_worker_
        principal` on every successful authentication -- a heartbeat
        registry with no separate heartbeat endpoint to forget to call.
        Never raises past a caller expecting best-effort semantics is the
        caller's job (mirrors `record_audit_event_safely`'s contract)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(worker_credentials_table)
                .where(worker_credentials_table.c.worker_id == worker_id)
                .values(last_seen_at=seen_at)
            )

    async def list_worker_credentials(self) -> list[WorkerCredentialRecord]:
        """Trusted-operator inspection: CLI (`cli.py`) or the admin-gated
        `GET /api/v1/admin/workers` route (Gap-Closure WP-6, G16) -- never an
        unauthenticated or non-admin-gated public surface."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(worker_credentials_table).order_by(
                            worker_credentials_table.c.created_at.asc()
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_worker_credential_from_row(row) for row in rows]

    async def rotate_worker_credential(
        self, worker_id: UUID, *, credential_digest: str, rotated_at: datetime
    ) -> None:
        """Overwrite the stored digest -- the old token's digest no longer matches anything."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(worker_credentials_table)
                .where(worker_credentials_table.c.worker_id == worker_id)
                .values(credential_digest=credential_digest, rotated_at=rotated_at)
            )

    async def revoke_worker_credential(self, worker_id: UUID, revoked_at: datetime) -> None:
        """Idempotent: revoking an already-revoked (or unknown) worker is not an error."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(worker_credentials_table)
                .where(worker_credentials_table.c.worker_id == worker_id)
                .where(worker_credentials_table.c.status != WorkerCredentialStatus.REVOKED.value)
                .values(status=WorkerCredentialStatus.REVOKED.value, revoked_at=revoked_at)
            )
