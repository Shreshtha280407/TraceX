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

from collections.abc import Mapping
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


def _user_from_row(row: sa.RowMapping | Mapping[str, Any]) -> UserRecord:
    return UserRecord.model_validate(dict(row))


def _case_from_row(row: sa.RowMapping | Mapping[str, Any]) -> CaseRecord:
    return CaseRecord.model_validate(dict(row))


def _membership_from_row(row: sa.RowMapping | Mapping[str, Any]) -> CaseMembershipRecord:
    return CaseMembershipRecord.model_validate(dict(row))


def _session_from_row(row: sa.RowMapping | Mapping[str, Any]) -> SessionRecord:
    return SessionRecord.model_validate(dict(row))


def _audit_event_from_row(row: sa.RowMapping | Mapping[str, Any]) -> SecurityAuditEventRecord:
    return SecurityAuditEventRecord.model_validate(dict(row))


def _worker_credential_from_row(row: sa.RowMapping | Mapping[str, Any]) -> WorkerCredentialRecord:
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

    async def list_users_with_system_role(
        self, system_role: str, *, limit: int, offset: int
    ) -> list[UserRecord]:
        """Bounded list of accounts with one explicit organisation role."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(users_table)
                        .where(users_table.c.system_role == system_role)
                        .order_by(users_table.c.created_at.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .mappings()
                .all()
            )
        return [_user_from_row(row) for row in rows]

    async def set_user_active(
        self, user_id: UUID, *, is_active: bool, updated_at: datetime
    ) -> bool:
        """Change account lifecycle state without exposing credential fields."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(users_table)
                .where(users_table.c.user_id == user_id)
                .values(is_active=is_active, updated_at=updated_at)
            )
        return bool(result.rowcount)

    async def update_password(
        self, user_id: UUID, *, password_hash: str, must_change_password: bool, updated_at: datetime
    ) -> None:
        """Set a new password hash and forced-change flag atomically."""
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
        """Set the TOTP secret and enabled flag together."""
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(users_table)
                .where(users_table.c.user_id == user_id)
                .values(totp_secret=totp_secret, totp_enabled=totp_enabled, updated_at=updated_at)
            )

    async def count_users_with_system_role(self, system_role: str) -> int:
        """How many active accounts hold the supplied organisation role."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(sa.func.count())
                .select_from(users_table)
                .where(users_table.c.system_role == system_role, users_table.c.is_active.is_(True))
            )
            return int(result.scalar_one())

    async def has_user_with_system_role(self, system_role: str) -> bool:
        """Whether any account has ever held an organisation role.

        First-deployment setup deliberately uses this rather than the active
        count above: deactivating a Provisioner must not reopen an
        unauthenticated deployment ceremony.
        """
        async with self._engine.connect() as conn:
            result = await conn.scalar(
                sa.select(sa.exists().where(users_table.c.system_role == system_role))
            )
            return bool(result)

    async def create_first_provisioner_if_none(
        self, user: UserRecord, audit_event: SecurityAuditEventRecord
    ) -> bool:
        """Create the first active Provisioner exactly once.

        An advisory transaction lock serializes setup across API processes
        without requiring a throwaway database row or a schema migration.
        The user and audit event are committed together.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 71026001}
            )
            provisioner_exists = await conn.scalar(
                sa.select(sa.exists().where(users_table.c.system_role == "provisioner"))
            )
            if provisioner_exists:
                return False
            await conn.execute(sa.insert(users_table).values(**user.model_dump(mode="python")))
            values = _dump_for_insert(audit_event, ("outcome",))
            await conn.execute(sa.insert(security_audit_events_table).values(**values))
            return True

    # --- cases / memberships -------------------------------------------------

    async def create_case_with_owner(
        self, case: CaseRecord, membership: CaseMembershipRecord
    ) -> None:
        """Persist a new case and its first active owner in one transaction."""
        case_values = _dump_for_insert(case, ("classification", "status"))
        membership_values = _dump_for_insert(membership, ("role", "clearance"))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(cases_table).values(**case_values))
            await conn.execute(sa.insert(case_memberships_table).values(**membership_values))

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

    async def create_team_user_with_membership(
        self,
        user: UserRecord,
        membership: CaseMembershipRecord,
        audit_event: SecurityAuditEventRecord,
    ) -> None:
        """Atomically create an ordinary user and exactly one case membership."""
        membership_values = _dump_for_insert(membership, ("role", "clearance"))
        audit_values = _dump_for_insert(audit_event, ("outcome",))
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(users_table).values(**user.model_dump(mode="python")))
            await conn.execute(sa.insert(case_memberships_table).values(**membership_values))
            await conn.execute(sa.insert(security_audit_events_table).values(**audit_values))

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

    async def list_case_members(
        self, case_id: UUID
    ) -> list[tuple[CaseMembershipRecord, UserRecord]]:
        """Return only users attached to this exact case, including inactive memberships."""
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(case_memberships_table, users_table)
                        .join(
                            users_table, users_table.c.user_id == case_memberships_table.c.user_id
                        )
                        .where(case_memberships_table.c.case_id == case_id)
                        .order_by(
                            users_table.c.display_name.asc(), users_table.c.email_normalized.asc()
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [
            (
                _membership_from_row(
                    {column.name: row[column.name] for column in case_memberships_table.c}
                ),
                _user_from_row({column.name: row[column.name] for column in users_table.c}),
            )
            for row in rows
        ]

    async def list_active_team_user_candidates(self, *, limit: int) -> list[UserRecord]:
        """Safe picker source: unassigned active ordinary users only.

        This intentionally does not disclose Team Members already assigned to
        another case.  A Case Head can re-activate an inactive membership in
        their current case through the same narrow picker, but cannot use it
        as a cross-case personnel directory.
        """
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        sa.select(users_table)
                        .where(
                            users_table.c.is_active.is_(True),
                            users_table.c.system_role.is_(None),
                            ~sa.exists(
                                sa.select(case_memberships_table.c.membership_id).where(
                                    case_memberships_table.c.user_id == users_table.c.user_id,
                                    case_memberships_table.c.is_active.is_(True),
                                )
                            ),
                        )
                        .order_by(
                            users_table.c.display_name.asc(), users_table.c.email_normalized.asc()
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        return [_user_from_row(row) for row in rows]

    async def update_case_membership(
        self,
        case_id: UUID,
        user_id: UUID,
        *,
        role: str,
        clearance: str,
        is_active: bool,
        updated_at: datetime,
    ) -> CaseMembershipRecord | None:
        """Atomically edit/deactivate a membership while retaining an active owner.

        A per-case advisory lock serializes every membership mutation, then
        row locks protect the owner-count invariant within that transaction.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtext(CAST(:case_id AS text)))"),
                {"case_id": str(case_id)},
            )
            current_row = (
                (
                    await conn.execute(
                        sa.select(case_memberships_table)
                        .where(
                            case_memberships_table.c.case_id == case_id,
                            case_memberships_table.c.user_id == user_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current_row is None:
                return None
            current = _membership_from_row(current_row)
            await conn.execute(
                sa.select(case_memberships_table.c.membership_id)
                .where(
                    case_memberships_table.c.case_id == case_id,
                    case_memberships_table.c.role == "case_owner",
                    case_memberships_table.c.is_active.is_(True),
                )
                .with_for_update()
            )
            removes_owner = current.role.value == "case_owner" and (
                role != "case_owner" or not is_active
            )
            if removes_owner:
                owners = await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(case_memberships_table)
                    .where(
                        case_memberships_table.c.case_id == case_id,
                        case_memberships_table.c.role == "case_owner",
                        case_memberships_table.c.is_active.is_(True),
                    )
                )
                if owners is not None and int(owners) <= 1:
                    raise ValueError("cannot remove or demote the final active case owner")
            await conn.execute(
                sa.update(case_memberships_table)
                .where(case_memberships_table.c.membership_id == current.membership_id)
                .values(role=role, clearance=clearance, is_active=is_active, updated_at=updated_at)
            )
            return CaseMembershipRecord.model_validate(
                {
                    **current.model_dump(mode="python"),
                    "role": role,
                    "clearance": clearance,
                    "is_active": is_active,
                    "updated_at": updated_at,
                }
            )

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
        """Credential recovery: kill every live session for
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
        """Trusted worker-control-plane inspection, never a public surface.

        The Provisioner UI deliberately does not expose fleet or case data;
        this method is used only by credential-authenticated internal worker
        controls and trusted maintenance tooling.
        """
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
