from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.auth import dependencies
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    engine = make_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(f"sqlite:///{tmp_path / 'auth.db'}", tmp_path / "evidence", 1024 * 1024, 3, 1)

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    return TestClient(app)


def _fake_verifier(emails_by_token: dict[str, str]):
    """A real Firebase ID token is a signed JWT verified against Google's public
    keys -- exercising that here would need a live Firebase project and network
    access. `current_user` only ever calls `verify_firebase_token(token) -> dict
    | None`, so patching that one function is enough to test the identity and
    authorization logic around it without touching the real SDK."""

    def verify(token: str) -> dict | None:
        email = emails_by_token.get(token)
        return {"uid": f"uid-for-{email}", "email": email} if email else None

    return verify


def test_firebase_bearer_token_creates_and_reuses_a_user(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(dependencies, "verify_firebase_token", _fake_verifier({"good-token": "analyst@example.com"}))
    try:
        headers = {"Authorization": "Bearer good-token"}
        empty = client.get("/v1/cases", headers=headers)
        assert empty.status_code == 200
        assert empty.json() == {"cases": []}

        created = client.post("/v1/cases", headers=headers, json={"name": "case", "synthetic": True})
        assert created.status_code == 201, created.text

        # Same Firebase uid on a second request reuses the same User row rather
        # than provisioning a duplicate -- the case created above is still visible.
        listed = client.get("/v1/cases", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["cases"] == [{**created.json(), "role": "case_lead"}]

        bad_token = client.get("/v1/cases", headers={"Authorization": "Bearer not-a-real-token"})
        assert bad_token.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_legacy_header_auth_still_works_unchanged(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/v1/cases", headers={"X-TraceX-Actor": "legacy-dev"}, json={"name": "legacy case", "synthetic": True}
        )
        assert response.status_code == 201, response.text
    finally:
        app.dependency_overrides.clear()


def test_new_read_endpoints_and_404_not_403_for_non_members(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        dependencies,
        "verify_firebase_token",
        _fake_verifier({"lead-token": "lead@example.com", "outsider-token": "outsider@example.com"}),
    )
    try:
        headers = {"Authorization": "Bearer lead-token"}

        empty = client.get("/v1/cases", headers=headers)
        assert empty.json() == {"cases": []}

        created = client.post("/v1/cases", headers=headers, json={"name": "PS26146-CASE-004", "synthetic": True})
        case_id = created.json()["case_id"]

        listed = client.get("/v1/cases", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["cases"] == [{**created.json(), "role": "case_lead"}]

        detail = client.get(f"/v1/cases/{case_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json() == {**created.json(), "members": [{"actor": "lead@example.com", "role": "case_lead"}]}

        sources = client.get(f"/v1/cases/{case_id}/sources", headers=headers)
        assert sources.status_code == 200
        assert sources.json() == {"sources": []}

        other_headers = {"Authorization": "Bearer outsider-token"}
        not_member = client.get(f"/v1/cases/{case_id}", headers=other_headers)
        not_found = client.get("/v1/cases/does-not-exist", headers=other_headers)
        assert not_member.status_code == not_found.status_code == 404
        assert not_member.json() == not_found.json()
    finally:
        app.dependency_overrides.clear()
