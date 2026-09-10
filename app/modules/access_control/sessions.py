"""Refresh-token session lifecycle: creation, rotation, and reuse detection.

Orchestrates `tokens.py` (opaque secret generation/hashing) and
`repository.py` (persistence). No HTTP/FastAPI concerns and no password
logic live here -- see `service.py` for the endpoint-facing orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.modules.access_control.errors import RefreshReuseDetectedError, SessionRevokedError
from app.modules.access_control.models import SessionRecord
from app.modules.access_control.repository import AccessControlRepository
from app.modules.access_control.tokens import generate_refresh_token, hash_refresh_token


@dataclass(frozen=True)
class IssuedSession:
    """A newly created/rotated session and its one-time raw refresh token.

    `__repr__` is overridden so an accidental `logger.info(..., x=issued)`
    or bare `print(issued)` cannot leak the raw secret -- see
    "Session values must never appear in logs or responses".
    """

    session: SessionRecord
    refresh_token: str

    def __repr__(self) -> str:
        return f"IssuedSession(session_id={self.session.session_id}, refresh_token=***redacted***)"


def _new_session_record(
    *, user_id: UUID, token_family_id: UUID, now: datetime, ttl_seconds: int
) -> tuple[SessionRecord, str]:
    refresh_token = generate_refresh_token()
    session = SessionRecord(
        session_id=uuid4(),
        user_id=user_id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        token_family_id=token_family_id,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        revoked_at=None,
        replaced_by_session_id=None,
        last_used_at=None,
    )
    return session, refresh_token


async def create_session(
    repository: AccessControlRepository,
    *,
    user_id: UUID,
    now: datetime,
    ttl_seconds: int,
) -> IssuedSession:
    """Start a brand-new token family (first login)."""
    session, refresh_token = _new_session_record(
        user_id=user_id, token_family_id=uuid4(), now=now, ttl_seconds=ttl_seconds
    )
    await repository.create_session(session)
    return IssuedSession(session=session, refresh_token=refresh_token)


async def rotate_session(
    repository: AccessControlRepository,
    *,
    refresh_token: str,
    now: datetime,
    ttl_seconds: int,
) -> IssuedSession:
    """Validate and rotate a refresh token. Every rotation creates a replacement session.

    - Unknown hash -> `SessionRevokedError` (a safe, generic denial; never
      reveals whether *some* token exists versus this exact one being
      wrong -- see `docs/decisions/ADR-003-...md`).
    - Expired -> `SessionRevokedError`.
    - Already revoked *and already rotated away*
      (`replaced_by_session_id is not None`) -> this is a replay of a
      stale token: revoke the *entire* token family (including whatever
      session it was rotated into, which may still be legitimately in
      use) and raise `RefreshReuseDetectedError`. Reuse anywhere in a
      chain is treated as a signal the whole family may be compromised.
    - Already revoked for any other reason (e.g. logout) ->
      `SessionRevokedError`.
    - Otherwise: revoke this session and create its replacement in the
      *same* token family, atomically.
    """
    token_hash = hash_refresh_token(refresh_token)
    session = await repository.get_session_by_refresh_token_hash(token_hash)
    if session is None:
        raise SessionRevokedError("refresh token not recognized")
    if session.expires_at <= now:
        raise SessionRevokedError("refresh token expired")
    if session.revoked_at is not None:
        if session.replaced_by_session_id is not None:
            await repository.revoke_family(session.token_family_id, now)
            raise RefreshReuseDetectedError("refresh token reuse detected; family revoked")
        raise SessionRevokedError("refresh token session already revoked")

    new_session, new_refresh_token = _new_session_record(
        user_id=session.user_id,
        token_family_id=session.token_family_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    await repository.rotate_session(
        old_session_id=session.session_id, new_session=new_session, revoked_at=now
    )
    return IssuedSession(session=new_session, refresh_token=new_refresh_token)


async def revoke_session_by_refresh_token(
    repository: AccessControlRepository,
    *,
    refresh_token: str,
    now: datetime,
) -> None:
    """Logout: revoke the session this refresh token maps to.

    Idempotent from the caller's perspective: an unknown or
    already-revoked token is treated as "already logged out", not an
    error -- repeated logout never raises.
    """
    token_hash = hash_refresh_token(refresh_token)
    session = await repository.get_session_by_refresh_token_hash(token_hash)
    if session is None or session.revoked_at is not None:
        return
    await repository.revoke_session(session.session_id, now)
