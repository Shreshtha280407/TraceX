"""Typed application configuration loaded from environment variables.

All settings are declared with no implicit defaults for values that are
required for the service to run correctly outside of local development.
Missing or malformed values fail fast with a clear validation error at
process startup rather than surfacing as confusing runtime errors later.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# HS256 wants a key with at least 256 bits of entropy; 32 ASCII characters is
# the simplest way to guarantee that floor without parsing key encoding.
_MIN_JWT_SECRET_LENGTH = 32


class AppEnv(StrEnum):
    """Deployment environment discriminator."""

    LOCAL = "local"
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Process-wide configuration.

    Values are sourced from environment variables (or a local `.env` file
    during development). See `.env.example` for the full set of required
    variables and safe development placeholders.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = Field(default="tracex-api")
    app_env: AppEnv = Field(default=AppEnv.LOCAL)
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    # --- PostgreSQL ---
    postgres_dsn: PostgresDsn

    # --- Neo4j ---
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str

    # --- Redis ---
    redis_url: RedisDsn

    # --- MinIO / S3-compatible object storage ---
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool = Field(default=False)
    minio_bucket: str = Field(default="tracex-evidence")

    # --- Contracts ---
    contract_version: str = Field(default="v1")

    # --- Authentication / access control ---
    # No default: a misconfigured deployment must fail loudly at startup,
    # same policy as postgres_dsn/neo4j_uri/etc. above. `.env.example` ships
    # a syntactically-valid (>= 32 char) development-only placeholder.
    auth_jwt_secret: SecretStr
    auth_jwt_algorithm: str = Field(default="HS256")
    auth_jwt_issuer: str = Field(default="tracex-api")
    auth_jwt_audience: str = Field(default="tracex-clients")
    # 15 minutes: short enough that a compromised/leaked access token or a
    # revoked session stops working promptly without needing per-request
    # session validation on every future case/evidence endpoint.
    auth_access_token_ttl_seconds: int = Field(default=900, ge=60)
    # 14 days: long enough to avoid forcing a working analyst to re-login
    # daily; refresh rotation + reuse detection (see `sessions.py`) bounds
    # the blast radius of a leaked refresh token more than shortening this
    # window further would.
    auth_refresh_token_ttl_seconds: int = Field(default=1_209_600, ge=3600)
    # Attempts allowed per 60-second fixed window (see `rate_limit.py`).
    auth_login_rate_limit: int = Field(default=5, ge=1)
    auth_refresh_rate_limit: int = Field(default=20, ge=1)

    @field_validator("auth_jwt_secret")
    @classmethod
    def _validate_jwt_secret_strength(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < _MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                f"auth_jwt_secret must be at least {_MIN_JWT_SECRET_LENGTH} characters"
            )
        return value

    # --- Evidence lifecycle ---
    # 200 MiB: a safe default ceiling for a single evidence upload (document/
    # audio/image-sized files) in local development. Deployments handling
    # large video evidence should raise this explicitly via `.env`.
    max_evidence_bytes: int = Field(default=209_715_200, ge=1)

    # --- Worker job claim/result integration (Phase 2.1) ---
    # How long a claimed job stays exclusively owned by the claiming worker
    # before its lease is considered expired and the job becomes eligible
    # for another worker to reclaim (see "Lease and retry policy" in
    # docs/architecture/worker-job-lifecycle.md).
    worker_lease_seconds: int = Field(default=300, ge=1)
    # The base URL a *worker process* (e.g. the structured-processing
    # worker's `--once` CLI runner) uses to reach `/api/v1/internal/
    # worker-jobs/*` over HTTP. Deliberately separate from `app_host`/
    # `app_port` (the server's own bind address, `0.0.0.0` by default and
    # not itself a dialable client URL) -- a worker may run in a different
    # container/host than the API, so this is independently configurable.
    worker_api_base_url: str = Field(default="http://localhost:8000")

    # --- Worker identity (Phase 2 security hardening) ---
    # The plaintext credential *this specific worker process* presents as
    # `Authorization: Bearer <WORKER_TOKEN>` -- issued once, out of band, by
    # `uv run python -m app.modules.access_control.worker_credentials create`
    # (see docs/architecture/worker-identity-and-security.md). Client-side
    # only: the API server never reads this setting itself, only a worker
    # process does (e.g. `structured_processing.worker`'s CLI runner).
    # Deliberately no shared-secret fallback: a worker with no token
    # configured simply cannot authenticate, the same fail-closed posture
    # `auth_jwt_secret` and every other credential in this file already take.
    worker_token: SecretStr | None = Field(default=None)
    # A server-side secret mixed into every worker-credential digest
    # (`HMAC-SHA256(pepper, token)` instead of a plain unkeyed hash) so a
    # leaked `worker_credentials.credential_digest` column alone is not
    # enough to impersonate a worker -- the attacker would also need this
    # pepper. Optional in local/development/test environments (falls back to
    # an unkeyed SHA-256 digest, the same "high-entropy secret, fast hash is
    # fine" reasoning `access_control.tokens.hash_refresh_token` already
    # documents) but required in production -- see
    # `worker_credentials.resolve_worker_pepper`, which raises a clear,
    # non-secret configuration error rather than silently proceeding
    # unkeyed in that environment.
    worker_credential_pepper: SecretStr | None = Field(default=None)

    @field_validator("worker_token", "worker_credential_pepper")
    @classmethod
    def _normalize_blank_worker_secret_to_none(cls, value: SecretStr | None) -> SecretStr | None:
        """An empty/whitespace-only value is treated as "not configured", not as a real secret.

        The exact bug this guards against was caught live in Phase 2.1: a
        `docker compose` `${VAR}` substitution with no default resolves an
        *unset* variable to an *empty string* inside the container, not an
        absent one -- which pydantic-settings would otherwise treat as
        "provided" (`SecretStr('')`, not `None`), defeating an `is None`
        fail-closed check entirely and, worse, letting an empty submitted
        value pass an equality/`hmac.compare_digest` check against it.
        Normalizing here closes that at the source, for every caller of
        `Settings`, not just one compose passthrough.
        """
        if value is not None and not value.get_secret_value().strip():
            return None
        return value


def get_settings() -> Settings:
    """Build a fresh `Settings` instance from the current environment.

    Not cached at module scope so that tests can freely construct settings
    against different environments without cross-test leakage.
    """
    return Settings()
