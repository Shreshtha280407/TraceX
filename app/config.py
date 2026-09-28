"""Runtime configuration. Secrets are supplied by the environment, never committed."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    evidence_root: Path
    max_upload_bytes: int
    lease_seconds: int
    event_heartbeat_seconds: int
    worker_health_seconds: int = 60
    ingestion_batch_records: int = 32768
    secret_key: str = "tracex-dev-only-secret"
    token_ttl_seconds: int = 86400

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_url=os.environ.get(
                "TRACEX_DATABASE_URL", "postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5432/tracex"
            ),
            evidence_root=Path(os.environ.get("TRACEX_EVIDENCE_ROOT", "var/evidence")).resolve(),
            max_upload_bytes=int(os.environ.get("TRACEX_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))),
            lease_seconds=int(os.environ.get("TRACEX_LEASE_SECONDS", "30")),
            event_heartbeat_seconds=int(os.environ.get("TRACEX_EVENT_HEARTBEAT_SECONDS", "15")),
            worker_health_seconds=int(os.environ.get("TRACEX_WORKER_HEALTH_SECONDS", "60")),
            ingestion_batch_records=int(os.environ.get("TRACEX_INGESTION_BATCH_RECORDS", "32768")),
            secret_key=os.environ.get("TRACEX_SECRET_KEY", "tracex-dev-only-secret"),
            token_ttl_seconds=int(os.environ.get("TRACEX_TOKEN_TTL_SECONDS", "86400")),
        )


settings = Settings.from_environment()
