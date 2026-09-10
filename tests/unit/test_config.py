"""CORE-CONFIG-001: missing/invalid configuration fails clearly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_REQUIRED_KEYS = [
    "POSTGRES_DSN",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "REDIS_URL",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
]


def test_missing_required_config_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _REQUIRED_KEYS:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)  # type: ignore[call-arg]

    missing = {str(err["loc"][0]) for err in exc_info.value.errors()}
    assert missing == {key.lower() for key in _REQUIRED_KEYS}


def test_invalid_postgres_dsn_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", "not-a-valid-dsn")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_valid_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "key")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.contract_version == "v1"
