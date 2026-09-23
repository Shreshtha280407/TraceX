"""HTTP-layer tests for the entity layer (Gap-Closure WP-2).

Mirrors `test_graph_api.py`'s pattern: `get_access_control_repository`
overridden with an in-memory fake, `get_entity_repository` overridden with
`FakeEntityRepository` (no live PostgreSQL needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import CaseRole, ClearanceLevel
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.graph.dependencies import get_entity_repository
from app.modules.graph.entity_models import EntityResolutionCandidateRecord
from tests.fixtures.access_control.factories import (
    DEFAULT_PASSWORD,
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository
from tests.fixtures.graph.fake_entity_repository import FakeEntityRepository

VALID_PASSWORD = DEFAULT_PASSWORD


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def entity_repository() -> FakeEntityRepository:
    return FakeEntityRepository()


@pytest.fixture
def _override_dependencies(
    ac_repository: FakeAccessControlRepository, entity_repository: FakeEntityRepository
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_entity_repository] = lambda: entity_repository
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _member(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    *,
    role: CaseRole = CaseRole.INVESTIGATOR,
) -> tuple[str, object]:
    email = f"agent-{uuid4().hex[:10]}@example.test"
    user = make_user_record(email_normalized=email)
    await ac_repository.create_user(user)
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await ac_repository.create_case(case)
    await ac_repository.create_membership(
        make_membership_record(
            case_id=case.case_id,
            user_id=user.user_id,
            role=role,
            clearance=ClearanceLevel.CONFIDENTIAL,
        )
    )
    return token, case.case_id


async def _seed_two_entities_and_a_candidate(
    entity_repository: FakeEntityRepository, case_id
) -> EntityResolutionCandidateRecord:
    now = datetime.now(UTC)
    left, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=uuid4(),
        entity_type="phone",
        canonical_label="9876543210",
        aliases=(),
        stable_identifiers={"phone": "9876543210"},
        created_at=now,
    )
    right, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=uuid4(),
        entity_type="phone",
        canonical_label="9876543210",
        aliases=(),
        stable_identifiers={"phone": "9876543210"},
        created_at=now,
    )
    candidate = EntityResolutionCandidateRecord(
        entity_resolution_candidate_id=uuid4(),
        case_id=case_id,
        left_entity_id=left.entity_id,
        right_entity_id=right.entity_id,
        reasons=("exact_identifier",),
        identifier_types=("phone",),
        vector_score=None,
        contradiction_reasons=(),
        supporting_observation_ids=tuple(left.created_from_observation_ids)
        + tuple(right.created_from_observation_ids),
        config_version="entity_resolution_cascade_v1",
        created_at=now,
    )
    stored, _ = await entity_repository.upsert_candidate(candidate)
    return stored


async def test_member_can_read_their_own_entity(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository)
    entity, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=uuid4(),
        entity_type="phone",
        canonical_label="9876543210",
        aliases=(),
        stable_identifiers={"phone": "9876543210"},
        created_at=datetime.now(UTC),
    )

    response = await client.get(
        f"/api/v1/entities/{entity.entity_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["entity"]["entity_id"] == str(entity.entity_id)


async def test_cross_case_entity_is_never_visible(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    _owner_token, case_a = await _member(client, ac_repository)
    outsider_token, _case_b = await _member(client, ac_repository)
    entity, _ = await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_a,
        source_observation_id=uuid4(),
        entity_type="phone",
        canonical_label="9876543210",
        aliases=(),
        stable_identifiers={"phone": "9876543210"},
        created_at=datetime.now(UTC),
    )

    response = await client.get(
        f"/api/v1/entities/{entity.entity_id}",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert response.status_code == 403


async def test_unknown_entity_is_404(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, _case_id = await _member(client, ac_repository)
    response = await client.get(
        f"/api/v1/entities/{uuid4()}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


async def test_list_entity_candidates_reports_needs_review_by_default(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository)
    await _seed_two_entities_and_a_candidate(entity_repository, case_id)

    response = await client.get(
        f"/api/v1/cases/{case_id}/entity-candidates", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["effective_status"] == "needs_review"
    assert items[0]["latest_decision"] is None


async def test_name_similarity_alone_never_verifies_a_merge(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    """A candidate existing (even an exact-identifier one) never implies
    verification -- only an explicit review decision does."""
    token, case_id = await _member(client, ac_repository)
    candidate = await _seed_two_entities_and_a_candidate(entity_repository, case_id)

    response = await client.get(
        f"/api/v1/cases/{case_id}/entity-candidates", headers={"Authorization": f"Bearer {token}"}
    )
    item = response.json()["items"][0]
    assert item["candidate"]["entity_resolution_candidate_id"] == str(
        candidate.entity_resolution_candidate_id
    )
    assert item["effective_status"] == "needs_review"


async def test_reviewer_can_verify_then_split_and_the_latest_decision_wins(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository, role=CaseRole.REVIEWER)
    candidate = await _seed_two_entities_and_a_candidate(entity_repository, case_id)
    headers = {"Authorization": f"Bearer {token}"}

    verify = await client.post(
        f"/api/v1/entities/{candidate.left_entity_id}/resolution-review"
        f"?candidate_id={candidate.entity_resolution_candidate_id}",
        headers=headers,
        json={"decision": "verified_same", "rationale": "same handset, same case timeline"},
    )
    assert verify.status_code == 201, verify.text
    assert verify.json()["decision"] == "verified_same"

    listing_after_verify = await client.get(
        f"/api/v1/cases/{case_id}/entity-candidates", headers=headers
    )
    assert listing_after_verify.json()["items"][0]["effective_status"] == "verified_same"

    split = await client.post(
        f"/api/v1/entities/{candidate.left_entity_id}/resolution-review"
        f"?candidate_id={candidate.entity_resolution_candidate_id}",
        headers=headers,
        json={"decision": "split", "rationale": "further evidence shows two distinct people"},
    )
    assert split.status_code == 201, split.text

    listing_after_split = await client.get(
        f"/api/v1/cases/{case_id}/entity-candidates", headers=headers
    )
    item = listing_after_split.json()["items"][0]
    assert item["effective_status"] == "split", "unmerge (split) must work via a new decision"
    assert len(item["decision_history"]) == 2, "the original verified_same decision is retained"


async def test_rejected_candidate_is_retained_in_history_not_deleted(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository, role=CaseRole.REVIEWER)
    candidate = await _seed_two_entities_and_a_candidate(entity_repository, case_id)
    headers = {"Authorization": f"Bearer {token}"}

    reject = await client.post(
        f"/api/v1/entities/{candidate.left_entity_id}/resolution-review"
        f"?candidate_id={candidate.entity_resolution_candidate_id}",
        headers=headers,
        json={"decision": "rejected"},
    )
    assert reject.status_code == 201

    listing = await client.get(f"/api/v1/cases/{case_id}/entity-candidates", headers=headers)
    item = listing.json()["items"][0]
    assert item["effective_status"] == "rejected"
    assert len(item["decision_history"]) == 1


async def test_investigator_cannot_submit_a_review_decision(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    """REVIEW_DECIDE is not in INVESTIGATOR's role set -- 403, same as candidate review."""
    token, case_id = await _member(client, ac_repository, role=CaseRole.INVESTIGATOR)
    candidate = await _seed_two_entities_and_a_candidate(entity_repository, case_id)

    response = await client.post(
        f"/api/v1/entities/{candidate.left_entity_id}/resolution-review"
        f"?candidate_id={candidate.entity_resolution_candidate_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "verified_same"},
    )
    assert response.status_code == 403


# --- GET /cases/{case_id}/entities (case-scoped listing) ---------------------


async def _seed_entity(
    entity_repository: FakeEntityRepository,
    case_id,
    *,
    created_at: datetime,
) -> None:
    await entity_repository.get_or_create_entity_for_observation(
        entity_id=uuid4(),
        case_id=case_id,
        source_observation_id=uuid4(),
        entity_type="phone",
        canonical_label=f"label-{uuid4().hex[:6]}",
        aliases=(),
        stable_identifiers={},
        created_at=created_at,
    )


async def test_member_can_list_their_own_case_entities(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository)
    await _seed_entity(entity_repository, case_id, created_at=datetime.now(UTC))

    response = await client.get(
        f"/api/v1/cases/{case_id}/entities", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["case_id"] == str(case_id)


async def test_non_member_cannot_list_case_entities(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    """Same default-deny 403 every other case-scoped GET returns -- `require_graph_read`
    (`CaseAction.GRAPH_READ`), not a new authorization path."""
    _owner_token, case_a = await _member(client, ac_repository)
    outsider_token, _case_b = await _member(client, ac_repository)
    await _seed_entity(entity_repository, case_a, created_at=datetime.now(UTC))

    response = await client.get(
        f"/api/v1/cases/{case_a}/entities",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert response.status_code == 403


async def test_case_b_listing_never_includes_case_a_entities(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    """Cross-case leak check distinct from the 403 test above: even when a
    caller is authorized on their own case, that case's listing must never
    surface another case's rows -- same predicate discipline as every other
    route in this repo (`entities_table.c.case_id == case_id`)."""
    _token_a, case_a = await _member(client, ac_repository)
    token_b, case_b = await _member(client, ac_repository)
    await _seed_entity(entity_repository, case_a, created_at=datetime.now(UTC))
    await _seed_entity(entity_repository, case_b, created_at=datetime.now(UTC))

    response = await client.get(
        f"/api/v1/cases/{case_b}/entities", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["case_id"] == str(case_b)
    assert all(item["case_id"] != str(case_a) for item in items)


async def test_empty_case_returns_empty_list_not_an_error(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _member(client, ac_repository)

    response = await client.get(
        f"/api/v1/cases/{case_id}/entities", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_entity_listing_cursor_pagination_round_trips_across_pages(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository)
    base = datetime(2026, 9, 15, tzinfo=UTC)
    for i in range(3):
        await _seed_entity(entity_repository, case_id, created_at=base + timedelta(seconds=i))

    first_page = await client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 2},
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = await client.get(
        f"/api/v1/cases/{case_id}/entities",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["items"]) == 1

    first_ids = {item["entity_id"] for item in first_body["items"]}
    second_ids = {item["entity_id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)


async def test_entity_listing_cursor_from_case_a_is_rejected_for_case_b(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    entity_repository: FakeEntityRepository,
) -> None:
    """A cursor issued while paging Case A is never honored against Case B,
    even for the same authenticated caller -- matches `pagination.py`'s own
    case-binding contract and the equivalent hypotheses-listing test."""
    token_a, case_a = await _member(client, ac_repository)
    token_b, case_b = await _member(client, ac_repository)
    await _seed_entity(entity_repository, case_a, created_at=datetime.now(UTC))
    await _seed_entity(entity_repository, case_b, created_at=datetime.now(UTC))

    page_a = await client.get(
        f"/api/v1/cases/{case_a}/entities", headers={"Authorization": f"Bearer {token_a}"}
    )
    cursor_from_a = page_a.json()["next_cursor"]
    assert cursor_from_a is not None

    rejected = await client.get(
        f"/api/v1/cases/{case_b}/entities",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"cursor": cursor_from_a},
    )
    assert rejected.status_code == 422
