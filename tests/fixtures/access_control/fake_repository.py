"""In-memory duck-typed stand-in for `AccessControlRepository`.

Implements the same async method signatures so `service.py`/`sessions.py`/
`dependencies.py` can be exercised in unit tests without a real PostgreSQL
instance. See `tests/integration/access_control/` for tests against a live
database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    SecurityAuditEventRecord,
    SessionRecord,
    UserRecord,
    WorkerCredentialRecord,
    WorkerCredentialStatus,
)


class FakeAccessControlRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, UserRecord] = {}
        self.cases: dict[UUID, CaseRecord] = {}
        self.memberships: dict[UUID, CaseMembershipRecord] = {}
        self.sessions: dict[UUID, SessionRecord] = {}
        self.audit_events: list[SecurityAuditEventRecord] = []
        self.worker_credentials: dict[UUID, WorkerCredentialRecord] = {}
        self._first_admin_lock = asyncio.Lock()

    async def close(self) -> None:
        pass

    # --- users ---------------------------------------------------------

    async def create_user(self, user: UserRecord) -> None:
        self.users[user.user_id] = user

    async def get_user_by_email(self, email_normalized: str) -> UserRecord | None:
        return next(
            (u for u in self.users.values() if u.email_normalized == email_normalized), None
        )

    async def get_user_by_id(self, user_id: UUID) -> UserRecord | None:
        return self.users.get(user_id)

    async def list_users(self, *, limit: int, offset: int) -> list[UserRecord]:
        ordered = sorted(self.users.values(), key=lambda u: u.created_at)
        return ordered[offset : offset + limit]

    async def update_password(
        self, user_id: UUID, *, password_hash: str, must_change_password: bool, updated_at: datetime
    ) -> None:
        user = self.users.get(user_id)
        if user is not None:
            self.users[user_id] = user.model_copy(
                update={
                    "password_hash": password_hash,
                    "must_change_password": must_change_password,
                    "updated_at": updated_at,
                }
            )

    async def update_totp(
        self, user_id: UUID, *, totp_secret: str | None, totp_enabled: bool, updated_at: datetime
    ) -> None:
        user = self.users.get(user_id)
        if user is not None:
            self.users[user_id] = user.model_copy(
                update={
                    "totp_secret": totp_secret,
                    "totp_enabled": totp_enabled,
                    "updated_at": updated_at,
                }
            )

    async def count_users_with_system_role(self, system_role: str) -> int:
        return sum(
            1
            for u in self.users.values()
            if u.is_active and u.system_role is not None and u.system_role.value == system_role
        )

    async def create_first_admin_if_none(
        self, user: UserRecord, audit_event: SecurityAuditEventRecord
    ) -> bool:
        async with self._first_admin_lock:
            if await self.count_users_with_system_role("admin"):
                return False
            self.users[user.user_id] = user
            self.audit_events.append(audit_event)
            return True

    # --- cases / memberships ---------------------------------------------

    async def create_case(self, case: CaseRecord) -> None:
        """Mirrors the real `cases.uq_cases_case_reference` UNIQUE constraint."""
        if any(c.case_reference == case.case_reference for c in self.cases.values()):
            raise sa.exc.IntegrityError(
                "INSERT INTO cases (...)", {}, Exception("duplicate case_reference")
            )
        self.cases[case.case_id] = case

    async def get_case(self, case_id: UUID) -> CaseRecord | None:
        return self.cases.get(case_id)

    async def create_membership(self, membership: CaseMembershipRecord) -> None:
        self.memberships[membership.membership_id] = membership

    async def get_active_membership(
        self, case_id: UUID, user_id: UUID
    ) -> CaseMembershipRecord | None:
        return next(
            (
                m
                for m in self.memberships.values()
                if m.case_id == case_id and m.user_id == user_id and m.is_active
            ),
            None,
        )

    async def list_active_memberships_for_user(self, user_id: UUID) -> list[CaseMembershipRecord]:
        return [m for m in self.memberships.values() if m.user_id == user_id and m.is_active]

    async def list_case_members(
        self, case_id: UUID
    ) -> list[tuple[CaseMembershipRecord, UserRecord]]:
        rows = [
            (m, self.users[m.user_id])
            for m in self.memberships.values()
            if m.case_id == case_id and m.user_id in self.users
        ]
        return sorted(rows, key=lambda row: (row[1].display_name, row[1].email_normalized))

    async def list_active_user_candidates(self, *, limit: int) -> list[UserRecord]:
        return sorted(
            (user for user in self.users.values() if user.is_active),
            key=lambda user: (user.display_name, user.email_normalized),
        )[:limit]

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
        membership = next(
            (
                item
                for item in self.memberships.values()
                if item.case_id == case_id and item.user_id == user_id
            ),
            None,
        )
        if membership is None:
            return None
        if membership.role.value == "case_owner" and (role != "case_owner" or not is_active):
            owners = sum(
                1
                for item in self.memberships.values()
                if item.case_id == case_id and item.is_active and item.role.value == "case_owner"
            )
            if owners <= 1:
                raise ValueError("cannot remove or demote the final active case owner")
        updated = CaseMembershipRecord.model_validate(
            {
                **membership.model_dump(mode="python"),
                "role": role,
                "clearance": clearance,
                "is_active": is_active,
                "updated_at": updated_at,
            }
        )
        self.memberships[updated.membership_id] = updated
        return updated

    # --- sessions --------------------------------------------------------

    async def create_session(self, session: SessionRecord) -> None:
        self.sessions[session.session_id] = session

    async def get_session_by_id(self, session_id: UUID) -> SessionRecord | None:
        return self.sessions.get(session_id)

    async def get_session_by_refresh_token_hash(
        self, refresh_token_hash: str
    ) -> SessionRecord | None:
        return next(
            (s for s in self.sessions.values() if s.refresh_token_hash == refresh_token_hash),
            None,
        )

    async def rotate_session(
        self,
        *,
        old_session_id: UUID,
        new_session: SessionRecord,
        revoked_at: datetime,
    ) -> None:
        old = self.sessions[old_session_id]
        self.sessions[old_session_id] = old.model_copy(
            update={"revoked_at": revoked_at, "replaced_by_session_id": new_session.session_id}
        )
        self.sessions[new_session.session_id] = new_session

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> None:
        session = self.sessions.get(session_id)
        if session is not None and session.revoked_at is None:
            self.sessions[session_id] = session.model_copy(update={"revoked_at": revoked_at})

    async def revoke_all_sessions_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        for session_id, session in list(self.sessions.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.sessions[session_id] = session.model_copy(update={"revoked_at": revoked_at})

    async def revoke_family(self, token_family_id: UUID, revoked_at: datetime) -> None:
        for session_id, session in list(self.sessions.items()):
            if session.token_family_id == token_family_id and session.revoked_at is None:
                self.sessions[session_id] = session.model_copy(update={"revoked_at": revoked_at})

    async def touch_session_last_used(self, session_id: UUID, last_used_at: datetime) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            self.sessions[session_id] = session.model_copy(update={"last_used_at": last_used_at})

    # --- audit -------------------------------------------------------------

    async def record_audit_event(self, event: SecurityAuditEventRecord) -> None:
        self.audit_events.append(event)

    async def get_audit_event_by_id(self, event_id: UUID) -> SecurityAuditEventRecord | None:
        return next((e for e in self.audit_events if e.event_id == event_id), None)

    async def list_audit_events_for_case(
        self, case_id: UUID, *, limit: int | None = None
    ) -> list[SecurityAuditEventRecord]:
        events = sorted(
            (e for e in self.audit_events if e.case_id_nullable == case_id),
            key=lambda e: e.occurred_at,
            reverse=True,
        )
        return events[:limit] if limit is not None else events

    # --- worker credentials --------------------------------------------------

    async def create_worker_credential(self, credential: WorkerCredentialRecord) -> None:
        self.worker_credentials[credential.worker_id] = credential

    async def get_worker_credential_by_id(self, worker_id: UUID) -> WorkerCredentialRecord | None:
        return self.worker_credentials.get(worker_id)

    async def get_worker_credential_by_digest(
        self, credential_digest: str
    ) -> WorkerCredentialRecord | None:
        return next(
            (
                c
                for c in self.worker_credentials.values()
                if c.credential_digest == credential_digest
            ),
            None,
        )

    async def touch_worker_last_seen(self, worker_id: UUID, seen_at: datetime) -> None:
        credential = self.worker_credentials.get(worker_id)
        if credential is not None:
            self.worker_credentials[worker_id] = credential.model_copy(
                update={"last_seen_at": seen_at}
            )

    async def list_worker_credentials(self) -> list[WorkerCredentialRecord]:
        return sorted(self.worker_credentials.values(), key=lambda c: c.created_at)

    async def rotate_worker_credential(
        self, worker_id: UUID, *, credential_digest: str, rotated_at: datetime
    ) -> None:
        credential = self.worker_credentials.get(worker_id)
        if credential is not None:
            self.worker_credentials[worker_id] = credential.model_copy(
                update={"credential_digest": credential_digest, "rotated_at": rotated_at}
            )

    async def revoke_worker_credential(self, worker_id: UUID, revoked_at: datetime) -> None:
        credential = self.worker_credentials.get(worker_id)
        if credential is not None and credential.status is not WorkerCredentialStatus.REVOKED:
            self.worker_credentials[worker_id] = credential.model_copy(
                update={"status": WorkerCredentialStatus.REVOKED, "revoked_at": revoked_at}
            )
