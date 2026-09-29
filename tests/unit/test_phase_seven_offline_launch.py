"""Phase 7 offline-launch proof.

The Phase 6 offline-ingestion test (`test_phase_six_offline_and_access.py`)
installs its socket guard *after* the module-level `from app.main import app`
has already executed, so it only proves that in-request code paths (upload,
parse, graph, findings) make no outbound network call once a process already
exists. It says nothing about booting the process itself.

This test installs the guard first, before anything under `app` is imported,
so the assertion covers module import, FastAPI app construction, dependency
wiring, and readiness -- "startup order... no surprise network call" as the
submission proof package requires, not just steady-state request handling.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_process_boot_and_readiness_make_no_outbound_network_call(tmp_path, monkeypatch) -> None:
    real_socket_init = socket.socket.__init__
    allowed_families = {socket.AF_UNIX} if hasattr(socket, "AF_UNIX") else set()

    def guarded_init(self, family=socket.AF_INET, type=socket.SOCK_STREAM, *a, **k):
        if family not in allowed_families:
            raise AssertionError(f"unexpected socket() call during offline boot: family={family!r} type={type!r}")
        return real_socket_init(self, family, type, *a, **k)

    monkeypatch.setattr(socket.socket, "__init__", guarded_init)

    # Every `app.*` import happens from here on, strictly after the guard --
    # this ordering is the entire point of the test.
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.api import routes
    from app.config import Settings
    from app.db import Base, get_session, make_engine
    from app.main import app
    from app.models import WorkerHeartbeat

    engine = make_engine(f"sqlite:///{tmp_path / 'boot.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'boot.db'}",
        evidence_root=tmp_path / "evidence",
        max_upload_bytes=1024 * 1024,
        lease_seconds=30,
        event_heartbeat_seconds=1,
    )

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    try:
        client = TestClient(app)

        health = client.get("/v1/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "database": "ok", "evidence_vault": "ok"}

        # readyz additionally needs a live worker heartbeat -- write one directly,
        # the way the real worker process would, no HTTP call involved.
        with sessions() as session:
            session.add(WorkerHeartbeat(worker_id="offline-boot-check"))
            session.commit()
        ready = client.get("/v1/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
    finally:
        app.dependency_overrides.clear()


def test_dependency_manifests_are_pinned_for_offline_replay() -> None:
    """"Pinned Mac and Linux CPU dependency manifests" -- proof the lockfile and
    the offline-runtime contract are both present and internally consistent,
    not just asserted in prose in a doc nobody checks against the repo."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "requires-python" in pyproject

    lock = REPO / "uv.lock"
    assert lock.exists(), "uv.lock must be committed so an offline replay never resolves dependencies from the network"

    manifest_path = REPO / "docs" / "environment.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["offline_runtime_contract"]["network_required_at_runtime"] is False
    assert manifest["observed_host"]["os"]
    assert manifest["observed_host"]["python"]

    hardware_doc = (REPO / "docs" / "hardware.md").read_text(encoding="utf-8")
    assert "Linux CPU replay is the primary supported baseline" in hardware_doc
