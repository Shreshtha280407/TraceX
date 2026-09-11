"""HTTP-layer tests: `GET /api/v1/cases/{case_id}/graph/observations`.

Mirrors `tests/unit/evidence_lifecycle/test_evidence_api.py`'s pattern:
`get_access_control_repository` overridden with an in-memory fake so a real
user/case/membership can be registered and logged in through the actual
`/api/v1/auth` flow; `get_graph_repository` overridden with a minimal
duck-typed fake Neo4j repository (no live Neo4j needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
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
from app.modules.access_control.password import MIN_PASSWORD_LENGTH
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.graph.dependencies import get_graph_repository
from tests.fixtures.access_control.factories import make_case_record, make_membership_record
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

VALID_PASSWORD = "correct-horse-battery-staple"
assert len(VALID_PASSWORD) >= MIN_PASSWORD_LENGTH


class _FakeGraphRepository:
    """Duck-typed stand-in for `Neo4jGraphRepository`: canned `.read()` results in call order."""

    def __init__(self, read_results: list[list[dict[str, Any]]] | None = None) -> None:
        self._read_results = list(read_results or [])
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    async def write(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        raise AssertionError("the read-only graph endpoint must never issue a write")

    async def read(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        self.read_calls.append((query, parameters))
        return self._read_results.pop(0) if self._read_results else []


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def graph_repository() -> _FakeGraphRepository:
    return _FakeGraphRepository()


@pytest.fixture
def _override_dependencies(
    ac_repository: FakeAccessControlRepository, graph_repository: _FakeGraphRepository
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_graph_repository] = lambda: graph_repository
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_override_dependencies: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _authenticated_member(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    *,
    role: CaseRole = CaseRole.INVESTIGATOR,
) -> tuple[str, Any]:
    email = f"agent-{uuid4().hex[:10]}@example.test"
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Agent"},
    )
    assert register.status_code == 201, register.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    user_id = register.json()["user_id"]

    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await ac_repository.create_case(case)
    membership = make_membership_record(
        case_id=case.case_id, user_id=user_id, role=role, clearance=ClearanceLevel.CONFIDENTIAL
    )
    await ac_repository.create_membership(membership)
    return token, case.case_id


def _observation_props(case_id: Any, evidence_id: Any, observation_id: Any) -> dict[str, Any]:
    return {
        "observation_id": str(observation_id),
        "case_id": str(case_id),
        "evidence_id": str(evidence_id),
        "observation_type": "fir_report_mention",
        "extraction_confidence": 0.9,
        "extractor_name": "fir_report_text_v1",
        "extractor_version": "1.0.0",
        "extractor_config_hash": "deadbeef",
        "extractor_model_version": "n/a",
    }


def _mention_props(case_id: Any, observation_id: Any, mention_id: Any) -> dict[str, Any]:
    return {
        "mention_id": str(mention_id),
        "case_id": str(case_id),
        "observation_id": str(observation_id),
        "display_label": "Jane Roe",
        "mention_type": "person",
    }


async def test_authorized_member_gets_empty_page_for_a_case_with_no_graph_data(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["case_id"] == str(case_id)
    assert body["items"] == []
    assert body["has_more"] is False


async def test_authorized_member_sees_observation_and_its_mentions(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    evidence_id, observation_id, mention_id = uuid4(), uuid4(), uuid4()
    graph_repository._read_results = [
        [{"observation": _observation_props(case_id, evidence_id, observation_id)}],
        [
            {
                "observation_id": str(observation_id),
                "mention": _mention_props(case_id, observation_id, mention_id),
                "ordinal": 0,
            }
        ],
    ]

    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["observation_id"] == str(observation_id)
    assert item["evidence_id"] == str(evidence_id)
    assert len(item["mentions"]) == 1
    assert item["mentions"][0]["mention_id"] == str(mention_id)
    assert item["mentions"][0]["display_label"] == "Jane Roe"
    assert item["mentions"][0]["ordinal"] == 0


async def test_caller_without_case_membership_is_denied(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    email = f"outsider-{uuid4().hex[:10]}@example.test"
    register = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": VALID_PASSWORD, "display_name": "Outsider"},
    )
    assert register.status_code == 201
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]

    response = await client.get(
        f"/api/v1/cases/{uuid4()}/graph/observations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_case_a_member_cannot_read_case_b_graph(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token_a, _case_a_id = await _authenticated_member(client, ac_repository)
    _token_b, case_b_id = await _authenticated_member(client, ac_repository)

    response = await client.get(
        f"/api/v1/cases/{case_b_id}/graph/observations",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert response.status_code == 403


async def test_unauthenticated_request_is_denied(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/cases/{uuid4()}/graph/observations")
    assert response.status_code == 401


async def test_limit_above_maximum_is_rejected(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        params={"limit": 201},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_limit_zero_is_rejected(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        params={"limit": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_negative_offset_is_rejected(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        params={"offset": -1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


async def test_response_never_exposes_object_uri_credential_or_cypher(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    evidence_id, observation_id, mention_id = uuid4(), uuid4(), uuid4()
    graph_repository._read_results = [
        [{"observation": _observation_props(case_id, evidence_id, observation_id)}],
        [
            {
                "observation_id": str(observation_id),
                "mention": _mention_props(case_id, observation_id, mention_id),
                "ordinal": 0,
            }
        ],
    ]

    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    text = response.text.lower()
    for forbidden in ("object_uri", "minio", "cypher", "match (", "merge (", "password", "secret"):
        assert forbidden not in text


async def test_a_write_is_never_issued_by_this_read_only_endpoint(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    """`_FakeGraphRepository.write` raises if ever called -- this endpoint must never call it."""
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/observations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
