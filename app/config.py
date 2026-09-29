"""Runtime configuration. Secrets are supplied by the environment, never committed."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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
    # The anomaly stack runs after the deterministic findings on every completed
    # snapshot. It needs the optional `ml` extra; without it the run is skipped
    # and recorded, never fatal, so a deployment without scikit-learn still
    # ingests and still produces the Phase 4 findings.
    ml_findings_enabled: bool = True
    ml_review_budget: float = 0.01

    @staticmethod
    def _parse_ml_review_budget(raw: str) -> float:
        """Never let a malformed env var crash application startup.

        A range check (is this budget *safe*, e.g. not negative or larger than
        half the snapshot) belongs to the caller that knows what "safe" means for
        a review queue -- `app.engine.ingestion.pipeline._materialize_ml_findings`
        does that and turns an unsafe-but-numeric value into a visible
        `invalid_budget` status without failing the import. This parser only
        guards the narrower failure: a value that is not a number at all, which
        would otherwise raise out of `Settings.from_environment()` at process
        start and take the whole application down over one bad ML setting.
        """
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "TRACEX_ML_REVIEW_BUDGET=%r is not a number; using the default 0.01. "
                "ML findings will run at the default budget until this is fixed.",
                raw,
            )
            return 0.01

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
            ml_findings_enabled=os.environ.get("TRACEX_ML_FINDINGS", "1").lower() not in {"0", "false", "no"},
            ml_review_budget=cls._parse_ml_review_budget(os.environ.get("TRACEX_ML_REVIEW_BUDGET", "0.01")),
        )


settings = Settings.from_environment()
