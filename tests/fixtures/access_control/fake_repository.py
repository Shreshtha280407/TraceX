"""In-memory duck-typed stand-in for `AccessControlRepository`.

Implements the same async method signatures so `service.py`/`sessions.py`/
`dependencies.py` can be exercised in unit tests without a real PostgreSQL
instance. See `tests/integration/access_control/` for tests against a live
database.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    SecurityAuditEventRecord,
    SessionRecord,
    UserRecord,
)


class FakeAccessControlRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, UserRecord] = {}
        self.cases: dict[UUID, CaseRecord] = {}
        self.memberships: dict[UUID, CaseMembershipRecord] = {}
        self.sessions: dict[UUID, SessionRecord] = {}
        self.audit_events: list[SecurityAuditEventRecord] = []

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

    # --- cases / memberships ---------------------------------------------

    async def create_case(self, case: CaseRecord) -> None:
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
