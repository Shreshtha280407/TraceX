from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
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


def test_signup_login_and_token_reuse(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    try:
        signup = client.post("/v1/auth/signup", json={"display_name": "shreshtha", "password": "correct-horse"})
        assert signup.status_code == 201, signup.text
        token = signup.json()["token"]

        duplicate = client.post("/v1/auth/signup", json={"display_name": "shreshtha", "password": "another-pass"})
        assert duplicate.status_code == 409

        wrong = client.post("/v1/auth/login", json={"display_name": "shreshtha", "password": "wrong"})
        assert wrong.status_code == 401

        login = client.post("/v1/auth/login", json={"display_name": "shreshtha", "password": "correct-horse"})
        assert login.status_code == 200
        login_token = login.json()["token"]

        for use_token in (token, login_token):
            headers = {"Authorization": f"Bearer {use_token}"}
            listed = client.get("/v1/cases", headers=headers)
            assert listed.status_code == 200
            assert listed.json() == {"cases": []}

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
    try:
        signup = client.post("/v1/auth/signup", json={"display_name": "case-lead", "password": "correct-horse"})
        headers = {"Authorization": f"Bearer {signup.json()['token']}"}

        empty = client.get("/v1/cases", headers=headers)
        assert empty.json() == {"cases": []}

        created = client.post("/v1/cases", headers=headers, json={"name": "PS26146-CASE-004", "synthetic": True})
        case_id = created.json()["case_id"]

        listed = client.get("/v1/cases", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["cases"] == [{**created.json(), "role": "case_lead"}]

        detail = client.get(f"/v1/cases/{case_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json() == {**created.json(), "members": [{"actor": "case-lead", "role": "case_lead"}]}

        sources = client.get(f"/v1/cases/{case_id}/sources", headers=headers)
        assert sources.status_code == 200
        assert sources.json() == {"sources": []}

        other = client.post("/v1/auth/signup", json={"display_name": "outsider", "password": "correct-horse"})
        other_headers = {"Authorization": f"Bearer {other.json()['token']}"}
        not_member = client.get(f"/v1/cases/{case_id}", headers=other_headers)
        not_found = client.get("/v1/cases/does-not-exist", headers=other_headers)
        assert not_member.status_code == not_found.status_code == 404
        assert not_member.json() == not_found.json()
    finally:
        app.dependency_overrides.clear()
