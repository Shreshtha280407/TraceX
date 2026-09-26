"""HTTP-layer tests for case management (G6): create, read, add member.

Mirrors `test_api.py`'s in-memory-fake pattern.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_case_note_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import CaseRole, ClearanceLevel, SystemRole
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from tests.fixtures.access_control.factories import DEFAULT_PASSWORD, make_user_record
from tests.fixtures.access_control.fake_case_note_repository import FakeCaseNoteRepository
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

VALID_PASSWORD = DEFAULT_PASSWORD


@pytest.fixture
def fake_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def fake_note_repository() -> FakeCaseNoteRepository:
    return FakeCaseNoteRepository()


@pytest.fixture
def _override_dependencies(
    fake_repository: FakeAccessControlRepository, fake_note_repository: FakeCaseNoteRepository
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: fake_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_case_note_repository] = lambda: fake_note_repository
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _login(
    client: AsyncClient,
    fake_repository: FakeAccessControlRepository,
    *,
    system_role: SystemRole | None = SystemRole.CASE_HEAD,
) -> str:
    email = f"agent-{uuid4().hex[:10]}@example.test"
    await fake_repository.create_user(
        make_user_record(email_normalized=email, system_role=system_role)
    )
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def test_case_head_can_create_a_case_and_becomes_owner(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    token = await _login(client, fake_repository)
    headers = {"Authorization": f"Bearer {token}"}

    create = await client.post(
        "/api/v1/cases",
        headers=headers,
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    assert create.status_code == 201, create.text
    case_id = create.json()["case_id"]
    assert create.json()["status"] == "open"

    get_case = await client.get(f"/api/v1/cases/{case_id}", headers=headers)
    assert get_case.status_code == 200
    assert get_case.json()["case_reference"] == create.json()["case_reference"]

    status_response = await client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "open"


async def test_team_member_and_provisioner_cannot_create_cases(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    for role in (None, SystemRole.PROVISIONER):
        token = await _login(client, fake_repository, system_role=role)
        response = await client.post(
            "/api/v1/cases",
            headers={"Authorization": f"Bearer {token}"},
            json={"case_reference": f"DENIED-{uuid4().hex[:8]}", "classification": "restricted"},
        )
        assert response.status_code == 403


async def test_case_creation_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/cases", json={"case_reference": "CASE-X", "classification": "confidential"}
    )
    assert response.status_code == 401


async def test_duplicate_case_reference_is_conflict(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    token = await _login(client, fake_repository)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"}

    first = await client.post("/api/v1/cases", headers=headers, json=body)
    assert first.status_code == 201
    second = await client.post("/api/v1/cases", headers=headers, json=body)
    assert second.status_code == 409


async def test_non_member_cannot_read_case(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    create = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]

    outsider_token = await _login(client, fake_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}", headers={"Authorization": f"Bearer {outsider_token}"}
    )
    assert response.status_code == 403


async def test_owner_can_add_a_member_and_member_gains_case_read(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    create = await client.post(
        "/api/v1/cases",
        headers=owner_headers,
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]

    new_member_email = f"newmember-{uuid4().hex[:10]}@example.test"
    new_member = make_user_record(email_normalized=new_member_email)
    await fake_repository.create_user(new_member)

    add = await client.post(
        f"/api/v1/cases/{case_id}/members",
        headers=owner_headers,
        json={
            "user_id": str(new_member.user_id),
            "role": "investigator",
            "clearance": "confidential",
        },
    )
    assert add.status_code == 201, add.text
    assert add.json()["role"] == "investigator"

    member_login = await client.post(
        "/api/v1/auth/login", json={"email": new_member_email, "password": VALID_PASSWORD}
    )
    member_token = member_login.json()["access_token"]
    read = await client.get(
        f"/api/v1/cases/{case_id}", headers={"Authorization": f"Bearer {member_token}"}
    )
    assert read.status_code == 200


async def test_investigator_cannot_add_members(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """Only CASE_OWNER/CASE_MANAGER manage members -- INVESTIGATOR is denied."""
    from tests.fixtures.access_control.factories import make_case_record, make_membership_record

    investigator_email = f"investigator-{uuid4().hex[:10]}@example.test"
    investigator = make_user_record(email_normalized=investigator_email)
    await fake_repository.create_user(investigator)
    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await fake_repository.create_case(case)
    await fake_repository.create_membership(
        make_membership_record(
            case_id=case.case_id,
            user_id=investigator.user_id,
            role=CaseRole.INVESTIGATOR,
            clearance=ClearanceLevel.CONFIDENTIAL,
        )
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": investigator_email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]

    other_user = make_user_record(email_normalized=f"other-{uuid4().hex[:10]}@example.test")
    await fake_repository.create_user(other_user)
    response = await client.post(
        f"/api/v1/cases/{case.case_id}/members",
        headers={"Authorization": f"Bearer {token}"},
        json={"user_id": str(other_user.user_id), "role": "viewer", "clearance": "restricted"},
    )
    assert response.status_code == 403


async def test_cross_case_member_manage_is_denied(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """An owner of case A cannot add members to case B."""
    owner_a_token = await _login(client, fake_repository)
    create_a = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {owner_a_token}"},
        json={"case_reference": f"CASE-A-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    owner_b_token = await _login(client, fake_repository)
    create_b = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {owner_b_token}"},
        json={"case_reference": f"CASE-B-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_b_id = create_b.json()["case_id"]
    assert create_a.status_code == 201

    other_user = make_user_record(email_normalized=f"other-{uuid4().hex[:10]}@example.test")
    await fake_repository.create_user(other_user)
    response = await client.post(
        f"/api/v1/cases/{case_b_id}/members",
        headers={"Authorization": f"Bearer {owner_a_token}"},
        json={"user_id": str(other_user.user_id), "role": "viewer", "clearance": "restricted"},
    )
    assert response.status_code == 403


# --- Gap-Closure WP-4/WP-5 (G3, G7): case notes + audit read -----------------


async def test_owner_can_add_and_list_case_notes(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    token = await _login(client, fake_repository)
    create = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]

    add = await client.post(
        f"/api/v1/cases/{case_id}/notes",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "Suspect seen near the depot at 22:00."},
    )
    assert add.status_code == 201, add.text
    assert add.json()["text"] == "Suspect seen near the depot at 22:00."

    listing = await client.get(
        f"/api/v1/cases/{case_id}/notes", headers={"Authorization": f"Bearer {token}"}
    )
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


async def test_case_notes_pagination_offset_pages_past_limit(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """Gap-Closure WP-6 (G7/pagination): previously this route had no
    `limit`/`offset` at all -- a caller could not page past the full set."""
    token = await _login(client, fake_repository)
    create = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(3):
        add = await client.post(
            f"/api/v1/cases/{case_id}/notes", headers=headers, json={"text": f"note-{i}"}
        )
        assert add.status_code == 201, add.text

    first_page = await client.get(
        f"/api/v1/cases/{case_id}/notes", headers=headers, params={"limit": 2, "offset": 0}
    )
    second_page = await client.get(
        f"/api/v1/cases/{case_id}/notes", headers=headers, params={"limit": 2, "offset": 2}
    )
    assert first_page.status_code == second_page.status_code == 200
    first_texts = {item["text"] for item in first_page.json()["items"]}
    second_texts = {item["text"] for item in second_page.json()["items"]}
    assert len(first_page.json()["items"]) == 2
    assert len(second_page.json()["items"]) == 1
    assert first_texts.isdisjoint(second_texts)


async def test_case_notes_cursor_pagination_round_trips_across_pages(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """Gap-Closure re-close (G17): a real keyset round trip via `next_cursor`."""
    token = await _login(client, fake_repository)
    create = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(3):
        add = await client.post(
            f"/api/v1/cases/{case_id}/notes", headers=headers, json={"text": f"note-{i}"}
        )
        assert add.status_code == 201, add.text

    first_page = await client.get(
        f"/api/v1/cases/{case_id}/notes", headers=headers, params={"limit": 2}
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = await client.get(
        f"/api/v1/cases/{case_id}/notes",
        headers=headers,
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["items"]) == 1

    first_ids = {item["note_id"] for item in first_body["items"]}
    second_ids = {item["note_id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_case_notes_cursor_from_case_a_is_rejected_for_case_b(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """The exact property the gap register requires: a cursor issued while
    paging Case A is never honored against Case B."""
    token_a = await _login(client, fake_repository)
    token_b = await _login(client, fake_repository)
    create_a = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"case_reference": f"CASE-A-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    create_b = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"case_reference": f"CASE-B-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_a, case_b = create_a.json()["case_id"], create_b.json()["case_id"]

    await client.post(
        f"/api/v1/cases/{case_a}/notes",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"text": "note in case A"},
    )
    page_a = await client.get(
        f"/api/v1/cases/{case_a}/notes", headers={"Authorization": f"Bearer {token_a}"}
    )
    cursor_from_a = page_a.json()["next_cursor"]
    assert cursor_from_a is not None

    rejected = await client.get(
        f"/api/v1/cases/{case_b}/notes",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"cursor": cursor_from_a},
    )
    assert rejected.status_code == 422


async def test_note_write_denied_for_role_without_case_note_write(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    """`VIEWER` has neither `CASE_NOTE_WRITE` nor `CASE_NOTE_READ_ALL` -- see
    `access_control.models.ROLE_ACTIONS`."""
    from tests.fixtures.access_control.factories import make_case_record, make_membership_record

    viewer_email = f"viewer-{uuid4().hex[:10]}@example.test"
    viewer = make_user_record(email_normalized=viewer_email)
    await fake_repository.create_user(viewer)
    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await fake_repository.create_case(case)
    await fake_repository.create_membership(
        make_membership_record(
            case_id=case.case_id,
            user_id=viewer.user_id,
            role=CaseRole.VIEWER,
            clearance=ClearanceLevel.CONFIDENTIAL,
        )
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": viewer_email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]

    response = await client.post(
        f"/api/v1/cases/{case.case_id}/notes",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "should be denied"},
    )
    assert response.status_code == 403


async def test_audit_route_returns_case_scoped_events(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    token = await _login(client, fake_repository)
    create = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]

    audit = await client.get(
        f"/api/v1/cases/{case_id}/audit", headers={"Authorization": f"Bearer {token}"}
    )
    assert audit.status_code == 200
    events = audit.json()["items"]
    assert any(e["case_id_nullable"] == case_id for e in events)


async def test_owner_can_list_update_and_deactivate_case_member_but_not_final_owner(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    create = await client.post(
        "/api/v1/cases",
        headers=owner_headers,
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "confidential"},
    )
    case_id = create.json()["case_id"]
    member_email = f"member-{uuid4().hex[:10]}@example.test"
    member = make_user_record(email_normalized=member_email, display_name="Case Member")
    await fake_repository.create_user(member)

    added = await client.post(
        f"/api/v1/cases/{case_id}/members",
        headers=owner_headers,
        json={"user_id": str(member.user_id), "role": "viewer", "clearance": "restricted"},
    )
    assert added.status_code == 201
    listed = await client.get(f"/api/v1/cases/{case_id}/members", headers=owner_headers)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["user_id"] == str(member.user_id))
    assert row["display_name"] == "Case Member"
    assert row["email_normalized"] == member_email

    updated = await client.patch(
        f"/api/v1/cases/{case_id}/members/{member.user_id}",
        headers=owner_headers,
        json={"role": "analyst", "clearance": "confidential"},
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "analyst"
    deactivated = await client.delete(
        f"/api/v1/cases/{case_id}/members/{member.user_id}", headers=owner_headers
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False

    owner_id = next(
        item.user_id
        for item in fake_repository.users.values()
        if item.email_normalized.startswith("agent-")
    )
    protected = await client.delete(
        f"/api/v1/cases/{case_id}/members/{owner_id}", headers=owner_headers
    )
    assert protected.status_code == 409
    assert "final active case owner" in protected.json()["error"]["message"]


async def test_non_member_cannot_list_candidates_or_manage_another_case(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    case = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"case_reference": f"CASE-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    outsider_token = await _login(client, fake_repository)
    headers = {"Authorization": f"Bearer {outsider_token}"}
    for path in ("/members", "/member-candidates"):
        response = await client.get(
            f"/api/v1/cases/{case.json()['case_id']}{path}", headers=headers
        )
        assert response.status_code == 403


async def test_me_returns_only_the_callers_assigned_cases(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    token_a = await _login(client, fake_repository)
    token_b = await _login(client, fake_repository)
    create_a = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"case_reference": f"CASE-A-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    create_b = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"case_reference": f"CASE-B-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    me_a = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token_a}"})
    assert me_a.status_code == 200
    assert [item["case_id"] for item in me_a.json()["case_memberships"]] == [
        create_a.json()["case_id"]
    ]
    assert create_b.json()["case_id"] not in {
        item["case_id"] for item in me_a.json()["case_memberships"]
    }


async def test_provisioner_has_no_implied_case_or_evidence_access(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    case_head_token = await _login(client, fake_repository)
    created = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {case_head_token}"},
        json={"case_reference": f"PRIVATE-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    assert created.status_code == 201
    provisioner_token = await _login(client, fake_repository, system_role=SystemRole.PROVISIONER)
    case_id = created.json()["case_id"]
    assert (
        await client.get(
            f"/api/v1/cases/{case_id}", headers={"Authorization": f"Bearer {provisioner_token}"}
        )
    ).status_code == 403
    assert (
        await client.get(
            f"/api/v1/cases/{case_id}/evidence",
            headers={"Authorization": f"Bearer {provisioner_token}"},
        )
    ).status_code == 403


async def test_case_owner_creates_team_member_only_for_current_case(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    case_head_token = await _login(client, fake_repository)
    headers = {"Authorization": f"Bearer {case_head_token}"}
    case_a = await client.post(
        "/api/v1/cases",
        headers=headers,
        json={"case_reference": f"TEAM-A-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    case_b = await client.post(
        "/api/v1/cases",
        headers=headers,
        json={"case_reference": f"TEAM-B-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    created = await client.post(
        f"/api/v1/cases/{case_a.json()['case_id']}/team-members",
        headers=headers,
        json={
            "display_name": "Case A Team Member",
            "email": "case-a-team@example.test",
            "password": VALID_PASSWORD,
            "role": "investigator",
            "clearance": "restricted",
        },
    )
    assert created.status_code == 201, created.text
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "case-a-team@example.test", "password": VALID_PASSWORD},
    )
    member_token = login.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {member_token}"})
    assert me.json()["user"]["must_change_password"] is True
    assert [membership["case_id"] for membership in me.json()["case_memberships"]] == [
        case_a.json()["case_id"]
    ]
    assert (
        await client.get(
            f"/api/v1/cases/{case_b.json()['case_id']}",
            headers={"Authorization": f"Bearer {member_token}"},
        )
    ).status_code == 403
    candidates = await client.get(
        f"/api/v1/cases/{case_b.json()['case_id']}/member-candidates", headers=headers
    )
    assert candidates.status_code == 200
    assert all(
        item["email_normalized"] != "case-a-team@example.test"
        for item in candidates.json()["items"]
    )


async def test_case_manager_cannot_promote_or_manage_privileged_memberships(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    case = await client.post(
        "/api/v1/cases",
        headers=owner_headers,
        json={"case_reference": f"MANAGER-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    manager = await client.post(
        f"/api/v1/cases/{case.json()['case_id']}/team-members",
        headers=owner_headers,
        json={
            "display_name": "Manager",
            "email": "manager-policy@example.test",
            "password": VALID_PASSWORD,
            "role": "case_manager",
            "clearance": "restricted",
        },
    )
    assert manager.status_code == 201
    manager_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "manager-policy@example.test", "password": VALID_PASSWORD},
    )
    candidate = make_user_record(email_normalized="candidate-policy@example.test")
    await fake_repository.create_user(candidate)
    denied = await client.post(
        f"/api/v1/cases/{case.json()['case_id']}/members",
        headers={"Authorization": f"Bearer {manager_login.json()['access_token']}"},
        json={"user_id": str(candidate.user_id), "role": "case_manager", "clearance": "restricted"},
    )
    assert denied.status_code == 409
    excessive_clearance = await client.post(
        f"/api/v1/cases/{case.json()['case_id']}/members",
        headers={"Authorization": f"Bearer {manager_login.json()['access_token']}"},
        json={"user_id": str(candidate.user_id), "role": "investigator", "clearance": "secret"},
    )
    assert excessive_clearance.status_code == 409


async def test_case_owner_cannot_reset_a_provisioner_via_case_endpoint(
    client: AsyncClient, fake_repository: FakeAccessControlRepository
) -> None:
    owner_token = await _login(client, fake_repository)
    case = await client.post(
        "/api/v1/cases",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"case_reference": f"BOUNDARY-{uuid4().hex[:8]}", "classification": "restricted"},
    )
    provisioner = make_user_record(system_role=SystemRole.PROVISIONER)
    await fake_repository.create_user(provisioner)
    response = await client.post(
        f"/api/v1/cases/{case.json()['case_id']}/members/{provisioner.user_id}/reset-credentials",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert response.status_code == 404
