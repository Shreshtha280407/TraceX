"""HTTP-layer tests: `GET /api/v1/cases/{case_id}/graph/observations`.

Mirrors `tests/unit/evidence_lifecycle/test_evidence_api.py`'s pattern:
`get_access_control_repository` overridden with an in-memory fake so a real
user/case/membership can be registered and logged in through the actual
`/api/v1/auth` flow; `get_graph_repository` overridden with a minimal
duck-typed fake Neo4j repository (no live Neo4j needed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
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
from app.modules.graph.dependencies import (
    get_graph_correlation_integration_repository,
    get_graph_repository,
    get_hypothesis_repository,
)
from app.modules.graph.hypothesis_models import HypothesisRecord, HypothesisStatus
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
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


class _FakeIntegrationRepository:
    """The route guard must run before this inert PostgreSQL boundary."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[tuple[str, Any]] = []

    def _raise_if_needed(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_correlations(self, case_id: Any, *, limit: int | None = None) -> list[Any]:
        self.calls.append(("list_correlations", (case_id, limit)))
        self._raise_if_needed()
        return []

    async def get_correlation(self, case_id: Any, correlation_id: Any) -> None:
        self.calls.append(("get_correlation", (case_id, correlation_id)))
        self._raise_if_needed()
        return None

    async def get_event_for_correlation(self, case_id: Any, correlation_id: Any) -> None:
        self.calls.append(("get_event_for_correlation", (case_id, correlation_id)))
        self._raise_if_needed()
        return None

    async def list_candidates(self, case_id: Any, *, limit: int | None = None) -> list[Any]:
        self.calls.append(("list_candidates", (case_id, limit)))
        self._raise_if_needed()
        return []


class _FakeHypothesisRepository:
    """Minimal duck-typed stand-in: only `list_hypotheses` is exercised by
    `GET /cases/{id}/hypotheses` (Gap-Closure re-close, G17 cursor tests)."""

    def __init__(self) -> None:
        self.hypotheses: list[Any] = []

    async def list_hypotheses(
        self, case_id: Any, *, limit: int | None = None, offset: int = 0, after: Any = None
    ) -> list[Any]:
        rows = [h for h in self.hypotheses if h.case_id == case_id]
        rows.sort(key=lambda h: (h.created_at, h.hypothesis_id), reverse=True)
        if after is not None:
            rows = [
                h
                for h in rows
                if (h.created_at, h.hypothesis_id) < (after.created_at, after.row_id)
            ]
        else:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def graph_repository() -> _FakeGraphRepository:
    return _FakeGraphRepository()


@pytest.fixture
def integration_repository() -> _FakeIntegrationRepository:
    return _FakeIntegrationRepository()


@pytest.fixture
def hypothesis_repository() -> _FakeHypothesisRepository:
    return _FakeHypothesisRepository()


@pytest.fixture
def _override_dependencies(
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
    integration_repository: _FakeIntegrationRepository,
    hypothesis_repository: _FakeHypothesisRepository,
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_graph_repository] = lambda: graph_repository
    app.dependency_overrides[get_graph_correlation_integration_repository] = lambda: (
        integration_repository
    )
    app.dependency_overrides[get_hypothesis_repository] = lambda: hypothesis_repository
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
    user = make_user_record(email_normalized=email)
    await ac_repository.create_user(user)
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    user_id = user.user_id

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
    await ac_repository.create_user(make_user_record(email_normalized=email))
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


async def test_all_phase5_integration_reads_require_authentication(client: AsyncClient) -> None:
    case_id, correlation_id = uuid4(), uuid4()
    paths = (
        f"/api/v1/cases/{case_id}/graph/correlations",
        f"/api/v1/cases/{case_id}/graph/correlations/{correlation_id}",
        f"/api/v1/cases/{case_id}/graph/candidates",
        f"/api/v1/cases/{case_id}/graph/hypotheses",
    )
    for path in paths:
        response = await client.get(path)
        assert response.status_code == 401


async def test_cross_case_integration_read_is_denied_before_object_lookup(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    integration_repository: _FakeIntegrationRepository,
) -> None:
    token, _case_a_id = await _authenticated_member(client, ac_repository)
    _other_token, case_b_id = await _authenticated_member(client, ac_repository)
    paths = (
        f"/api/v1/cases/{case_b_id}/graph/correlations",
        f"/api/v1/cases/{case_b_id}/graph/correlations/{uuid4()}",
        f"/api/v1/cases/{case_b_id}/graph/candidates",
        f"/api/v1/cases/{case_b_id}/graph/hypotheses",
        f"/api/v1/cases/{case_b_id}/candidates",
        f"/api/v1/cases/{case_b_id}/candidates/{uuid4()}",
        f"/api/v1/cases/{case_b_id}/hypotheses",
        f"/api/v1/cases/{case_b_id}/hypotheses/{uuid4()}",
    )
    for path in paths:
        response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    denials = [
        event for event in ac_repository.audit_events if event.event_type == "case_access_denied"
    ]
    assert len(denials) == len(paths)
    assert all(event.metadata_safe_json == {"action": "graph_read"} for event in denials)
    assert integration_repository.calls == [], "authorization must run before graph/SQL lookup"


async def test_postgres_outage_on_correlation_read_returns_a_safe_service_error(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    integration_repository: _FakeIntegrationRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    integration_repository.error = sa.exc.OperationalError(
        "SELECT correlations", {}, RuntimeError("postgresql://user:secret@unavailable")
    )

    response = await client.get(
        f"/api/v1/cases/{case_id}/graph/correlations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "graph data temporarily unavailable"
    lowered = response.text.lower()
    assert "secret" not in lowered
    assert "select" not in lowered
    assert "postgresql" not in lowered


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


@pytest.mark.parametrize(
    "suffix",
    ("graph/correlations", "graph/candidates", "graph/hypotheses", "candidates", "hypotheses"),
)
async def test_all_graph_collection_limits_are_validated_before_repository_work(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    integration_repository: _FakeIntegrationRepository,
    suffix: str,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/{suffix}",
        params={"limit": 201},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert integration_repository.calls == []


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


# --- Gap-Closure WP-6 (G7 rest): graph snapshot/path/analytics/motifs -------


async def test_graph_snapshot_returns_entities_events_and_relationships(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    entity_a, entity_b, event_a = uuid4(), uuid4(), uuid4()
    graph_repository._read_results = [
        [
            {
                "entity_id": str(entity_a),
                "entity_type": "phone_number",
                "canonical_label": "+91-synthetic",
                "aliases": [],
                "review_status": "pending",
            }
        ],
        [
            {
                "event_id": str(event_a),
                "event_type": "cdr_call",
                "review_status": "pending",
                "confidence": 0.8,
                "event_time": None,
            }
        ],
        [{"from_id": str(event_a), "to_id": str(entity_a)}],  # HAS_PARTICIPANT
        [{"from_id": str(entity_a), "to_id": str(entity_b)}],  # POSSIBLY_SAME_AS
        [],  # CONTRADICTED_BY
    ]

    response = await client.get(
        f"/api/v1/cases/{case_id}/graph", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["case_id"] == str(case_id)
    assert len(body["entities"]) == 1
    assert len(body["events"]) == 1
    kinds = {r["kind"] for r in body["relationships"]}
    assert kinds == {"HAS_PARTICIPANT", "POSSIBLY_SAME_AS"}


async def test_graph_snapshot_empty_case_returns_empty_snapshot(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.get(
        f"/api/v1/cases/{case_id}/graph", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entities"] == []
    assert body["events"] == []
    assert body["relationships"] == []


async def test_graph_path_returns_found_false_when_no_path_exists(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    graph_repository._read_results = [[]]

    response = await client.post(
        f"/api/v1/cases/{case_id}/graph/path",
        headers={"Authorization": f"Bearer {token}"},
        json={"from_entity_id": str(uuid4()), "to_entity_id": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    assert response.json()["found"] is False


async def test_graph_path_returns_the_found_path(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    entity_a, event_a, entity_b = uuid4(), uuid4(), uuid4()
    graph_repository._read_results = [
        [
            {
                "path_nodes": [
                    {"labels": ["Entity"], "id": str(entity_a)},
                    {"labels": ["Event"], "id": str(event_a)},
                    {"labels": ["Entity"], "id": str(entity_b)},
                ],
                "path_relationships": [
                    {"kind": "HAS_PARTICIPANT", "from_id": str(event_a), "to_id": str(entity_a)},
                    {"kind": "HAS_PARTICIPANT", "from_id": str(event_a), "to_id": str(entity_b)},
                ],
                "path_length": 2,
            }
        ]
    ]

    response = await client.post(
        f"/api/v1/cases/{case_id}/graph/path",
        headers={"Authorization": f"Bearer {token}"},
        json={"from_entity_id": str(entity_a), "to_entity_id": str(entity_b), "max_hops": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] is True
    assert len(body["nodes"]) == 3
    assert len(body["relationships"]) == 2


async def test_graph_path_beyond_requested_max_hops_is_reported_not_found(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    """The shortest path Neo4j found is real (length 4) but exceeds the
    caller's own requested `max_hops=2` -- never silently returned anyway."""
    token, case_id = await _authenticated_member(client, ac_repository)
    graph_repository._read_results = [
        [{"path_nodes": [], "path_relationships": [], "path_length": 4}]
    ]

    response = await client.post(
        f"/api/v1/cases/{case_id}/graph/path",
        headers={"Authorization": f"Bearer {token}"},
        json={"from_entity_id": str(uuid4()), "to_entity_id": str(uuid4()), "max_hops": 2},
    )
    assert response.status_code == 200, response.text
    assert response.json()["found"] is False


async def test_graph_path_rejects_an_ambiguous_endpoint(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    response = await client.post(
        f"/api/v1/cases/{case_id}/graph/path",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "from_entity_id": str(uuid4()),
            "from_event_id": str(uuid4()),
            "to_entity_id": str(uuid4()),
        },
    )
    assert response.status_code == 422


async def test_graph_analytics_returns_node_and_relationship_counts(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    graph_repository._read_results = [
        [{"label": "Entity", "node_count": 3}, {"label": "Event", "node_count": 2}],
        [{"kind": "HAS_PARTICIPANT", "relationship_count": 5}],
    ]

    response = await client.get(
        f"/api/v1/cases/{case_id}/analytics", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["node_counts"] == {"Entity": 3, "Event": 2}
    assert body["relationship_counts"] == {"HAS_PARTICIPANT": 5}


async def test_graph_motifs_returns_co_participation_pairs(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    graph_repository: _FakeGraphRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    entity_id, event_a, event_b = uuid4(), uuid4(), uuid4()
    graph_repository._read_results = [
        [
            {
                "shared_entity_id": str(entity_id),
                "event_a_id": str(event_a),
                "event_b_id": str(event_b),
            }
        ]
    ]

    response = await client.get(
        f"/api/v1/cases/{case_id}/motifs", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["co_participation"]) == 1
    assert body["co_participation"][0]["shared_entity_id"] == str(entity_id)


async def test_new_read_routes_deny_a_non_member(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    other_email = f"outsider-{uuid4().hex[:10]}@example.test"
    await ac_repository.create_user(make_user_record(email_normalized=other_email))
    login = await client.post(
        "/api/v1/auth/login", json={"email": other_email, "password": VALID_PASSWORD}
    )
    token = login.json()["access_token"]
    case_id = uuid4()
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get(f"/api/v1/cases/{case_id}/graph", headers=headers)).status_code == 403
    assert (
        await client.post(
            f"/api/v1/cases/{case_id}/graph/path",
            headers=headers,
            json={"from_entity_id": str(uuid4()), "to_entity_id": str(uuid4())},
        )
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/cases/{case_id}/analytics", headers=headers)
    ).status_code == 403
    assert (await client.get(f"/api/v1/cases/{case_id}/motifs", headers=headers)).status_code == 403


# --- Gap-Closure re-close (G17): opaque, case-bound cursor pagination -------


def _hypothesis(case_id: Any, *, created_at: datetime) -> HypothesisRecord:
    return HypothesisRecord(
        hypothesis_id=uuid4(),
        case_id=case_id,
        status=HypothesisStatus.NEEDS_REVIEW,
        created_by=uuid4(),
        created_at=created_at,
        updated_at=created_at,
        decided_at=None,
        decided_by=None,
        supporting_observation_ids=(uuid4(),),
        supporting_candidate_ids=(),
        supporting_entity_resolution_candidate_ids=(),
        statement="synthetic statement",
        statement_commitment_sha256="a" * 64,
        rationale=None,
        rationale_commitment_sha256=None,
    )


async def test_hypotheses_cursor_pagination_round_trips_across_pages(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    hypothesis_repository: _FakeHypothesisRepository,
) -> None:
    """Gap-Closure re-close (G17): a real keyset round trip -- page 1's
    `next_cursor` fetches page 2 with no overlap and no gap."""
    token, case_id = await _authenticated_member(client, ac_repository)
    base = datetime(2026, 9, 15, tzinfo=UTC)
    seeded = [_hypothesis(case_id, created_at=base + timedelta(seconds=i)) for i in range(3)]
    hypothesis_repository.hypotheses = seeded

    first_page = await client.get(
        f"/api/v1/cases/{case_id}/hypotheses",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 2},
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = await client.get(
        f"/api/v1/cases/{case_id}/hypotheses",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["items"]) == 1

    first_ids = {item["hypothesis_id"] for item in first_body["items"]}
    second_ids = {item["hypothesis_id"] for item in second_body["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {str(h.hypothesis_id) for h in seeded}


async def test_hypotheses_cursor_from_case_a_is_rejected_for_case_b(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    hypothesis_repository: _FakeHypothesisRepository,
) -> None:
    """The exact property the gap register requires: a cursor issued while
    paging Case A is never honored against Case B, even for the same
    authenticated caller."""
    token_a, case_a = await _authenticated_member(client, ac_repository)
    token_b, case_b = await _authenticated_member(client, ac_repository)
    hypothesis_repository.hypotheses = [
        _hypothesis(case_a, created_at=datetime(2026, 9, 15, tzinfo=UTC)),
        _hypothesis(case_b, created_at=datetime(2026, 9, 15, tzinfo=UTC)),
    ]

    page_a = await client.get(
        f"/api/v1/cases/{case_a}/hypotheses", headers={"Authorization": f"Bearer {token_a}"}
    )
    cursor_from_a = page_a.json()["next_cursor"]
    assert cursor_from_a is not None

    rejected = await client.get(
        f"/api/v1/cases/{case_b}/hypotheses",
        headers={"Authorization": f"Bearer {token_b}"},
        params={"cursor": cursor_from_a},
    )
    assert rejected.status_code == 422


async def test_hypotheses_tampered_cursor_is_rejected(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    hypothesis_repository: _FakeHypothesisRepository,
) -> None:
    token, case_id = await _authenticated_member(client, ac_repository)
    hypothesis_repository.hypotheses = [
        _hypothesis(case_id, created_at=datetime(2026, 9, 15, tzinfo=UTC))
    ]
    first = await client.get(
        f"/api/v1/cases/{case_id}/hypotheses", headers={"Authorization": f"Bearer {token}"}
    )
    real_cursor = first.json()["next_cursor"]
    tampered = real_cursor[:-1] + ("A" if real_cursor[-1] != "A" else "B")

    response = await client.get(
        f"/api/v1/cases/{case_id}/hypotheses",
        headers={"Authorization": f"Bearer {token}"},
        params={"cursor": tampered},
    )
    assert response.status_code == 422
