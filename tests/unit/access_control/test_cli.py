"""First-admin bootstrap CLI (G5): idempotency, safe refusals, never leaks the password."""

from __future__ import annotations

import pytest

from app.modules.access_control.cli import provision_admin
from app.modules.access_control.models import SystemRole
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from tests.fixtures.access_control.factories import DEFAULT_PASSWORD
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository


@pytest.fixture
def repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


async def test_first_admin_is_created_successfully(
    repository: FakeAccessControlRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await provision_admin(
        repository, "admin@example.test", DEFAULT_PASSWORD, "Admin", force=False
    )
    assert code == 0
    (created,) = repository.users.values()
    assert created.system_role is SystemRole.ADMIN
    printed = capsys.readouterr().out.strip()
    assert printed == str(created.user_id)
    assert DEFAULT_PASSWORD not in printed


async def test_second_call_without_force_is_refused(
    repository: FakeAccessControlRepository,
) -> None:
    first = await provision_admin(
        repository, "admin@example.test", DEFAULT_PASSWORD, "Admin", force=False
    )
    assert first == 0
    second = await provision_admin(
        repository, "another-admin@example.test", DEFAULT_PASSWORD, "Admin 2", force=False
    )
    assert second == 1
    assert len(repository.users) == 1


async def test_force_creates_another_admin(repository: FakeAccessControlRepository) -> None:
    await provision_admin(repository, "admin@example.test", DEFAULT_PASSWORD, "Admin", force=False)
    second = await provision_admin(
        repository, "another-admin@example.test", DEFAULT_PASSWORD, "Admin 2", force=True
    )
    assert second == 0
    assert len(repository.users) == 2
    assert all(u.system_role is SystemRole.ADMIN for u in repository.users.values())


async def test_invalid_email_is_refused(repository: FakeAccessControlRepository) -> None:
    code = await provision_admin(repository, "not-an-email", DEFAULT_PASSWORD, "Admin", force=False)
    assert code == 1
    assert repository.users == {}


async def test_short_password_is_refused(repository: FakeAccessControlRepository) -> None:
    code = await provision_admin(
        repository, "admin@example.test", "x" * (MIN_PASSWORD_LENGTH - 1), "Admin", force=False
    )
    assert code == 1
    assert repository.users == {}


async def test_duplicate_email_is_refused(repository: FakeAccessControlRepository) -> None:
    await provision_admin(repository, "admin@example.test", DEFAULT_PASSWORD, "Admin", force=False)
    code = await provision_admin(
        repository, "admin@example.test", DEFAULT_PASSWORD, "Admin Again", force=True
    )
    assert code == 1
    assert len(repository.users) == 1
