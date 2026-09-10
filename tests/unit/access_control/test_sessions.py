"""Scenarios 7-9: refresh rotation, reuse-family revocation, and idempotent logout."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.access_control.errors import RefreshReuseDetectedError, SessionRevokedError
from app.modules.access_control.models import SessionRecord
from app.modules.access_control.sessions import (
    IssuedSession,
    create_session,
    revoke_session_by_refresh_token,
    rotate_session,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TTL = 1_209_600


async def test_create_session_persists_only_the_hash_never_the_raw_token() -> None:
    repository = FakeAccessControlRepository()
    user_id = uuid4()
    issued = await create_session(repository, user_id=user_id, now=NOW, ttl_seconds=TTL)

    stored = repository.sessions[issued.session.session_id]
    assert stored.refresh_token_hash != issued.refresh_token
    assert issued.refresh_token not in stored.refresh_token_hash


# --- Scenario 7: rotation invalidates the old token -------------------------


async def test_rotation_revokes_the_old_session_and_issues_a_new_one() -> None:
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)

    rotated = await rotate_session(
        repository,
        refresh_token=issued.refresh_token,
        now=NOW + timedelta(seconds=1),
        ttl_seconds=TTL,
    )

    old_session = repository.sessions[issued.session.session_id]
    assert old_session.revoked_at is not None
    assert old_session.replaced_by_session_id == rotated.session.session_id
    assert rotated.session.token_family_id == issued.session.token_family_id
    assert rotated.refresh_token != issued.refresh_token


async def test_old_refresh_token_no_longer_rotates_after_being_rotated_once() -> None:
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)
    await rotate_session(
        repository,
        refresh_token=issued.refresh_token,
        now=NOW + timedelta(seconds=1),
        ttl_seconds=TTL,
    )

    with pytest.raises(RefreshReuseDetectedError):
        await rotate_session(
            repository,
            refresh_token=issued.refresh_token,
            now=NOW + timedelta(seconds=2),
            ttl_seconds=TTL,
        )


async def test_unknown_refresh_token_is_a_safe_generic_denial() -> None:
    repository = FakeAccessControlRepository()
    with pytest.raises(SessionRevokedError):
        await rotate_session(repository, refresh_token="not-a-real-token", now=NOW, ttl_seconds=TTL)


async def test_expired_refresh_token_is_denied() -> None:
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=1)
    with pytest.raises(SessionRevokedError):
        await rotate_session(
            repository,
            refresh_token=issued.refresh_token,
            now=NOW + timedelta(seconds=10),
            ttl_seconds=TTL,
        )


# --- Scenario 8: reuse of a rotated token revokes the whole family ----------


async def test_reuse_of_rotated_token_revokes_the_entire_family_including_the_current_leaf() -> (
    None
):
    repository = FakeAccessControlRepository()
    first = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)
    second = await rotate_session(
        repository,
        refresh_token=first.refresh_token,
        now=NOW + timedelta(seconds=1),
        ttl_seconds=TTL,
    )

    with pytest.raises(RefreshReuseDetectedError):
        await rotate_session(
            repository,
            refresh_token=first.refresh_token,  # replay of the already-rotated token
            now=NOW + timedelta(seconds=2),
            ttl_seconds=TTL,
        )

    # The family's current, otherwise-legitimate leaf session is also
    # revoked -- reuse anywhere in the chain forces full re-authentication.
    current_leaf = repository.sessions[second.session.session_id]
    assert current_leaf.revoked_at is not None


async def test_logout_then_reuse_is_a_plain_denial_not_reuse_detection() -> None:
    # Logout (not rotation) revoking a session has `replaced_by_session_id
    # is None` -- reusing that token afterward must NOT trigger family
    # revocation (there was no rotation to "replay").
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)
    await revoke_session_by_refresh_token(
        repository, refresh_token=issued.refresh_token, now=NOW + timedelta(seconds=1)
    )
    with pytest.raises(SessionRevokedError):
        await rotate_session(
            repository,
            refresh_token=issued.refresh_token,
            now=NOW + timedelta(seconds=2),
            ttl_seconds=TTL,
        )


# --- Scenario 9: logout revokes the session and is safe to repeat ----------


async def test_logout_revokes_the_session() -> None:
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)
    await revoke_session_by_refresh_token(repository, refresh_token=issued.refresh_token, now=NOW)
    assert repository.sessions[issued.session.session_id].revoked_at is not None


async def test_logout_is_idempotent_for_an_already_revoked_session() -> None:
    repository = FakeAccessControlRepository()
    issued = await create_session(repository, user_id=uuid4(), now=NOW, ttl_seconds=TTL)
    await revoke_session_by_refresh_token(repository, refresh_token=issued.refresh_token, now=NOW)
    # Second call must not raise.
    await revoke_session_by_refresh_token(
        repository, refresh_token=issued.refresh_token, now=NOW + timedelta(seconds=1)
    )


async def test_logout_with_an_unknown_token_does_not_raise() -> None:
    repository = FakeAccessControlRepository()
    await revoke_session_by_refresh_token(repository, refresh_token="never-issued", now=NOW)


def test_issued_session_repr_never_reveals_the_raw_refresh_token() -> None:
    session = SessionRecord(
        session_id=uuid4(),
        user_id=uuid4(),
        refresh_token_hash="deadbeef",
        token_family_id=uuid4(),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=TTL),
        revoked_at=None,
        replaced_by_session_id=None,
        last_used_at=None,
    )
    issued = IssuedSession(session=session, refresh_token="super-secret-raw-token")
    assert "super-secret-raw-token" not in repr(issued)
