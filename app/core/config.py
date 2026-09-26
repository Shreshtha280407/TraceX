"""Typed application configuration loaded from environment variables.

All settings are declared with no implicit defaults for values that are
required for the service to run correctly outside of local development.
Missing or malformed values fail fast with a clear validation error at
process startup rather than surfacing as confusing runtime errors later.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import (
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# HS256 wants a key with at least 256 bits of entropy; 32 ASCII characters is
# the simplest way to guarantee that floor without parsing key encoding.
_MIN_JWT_SECRET_LENGTH = 32
_MIN_FIRST_ADMIN_SETUP_TOKEN_LENGTH = 32


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
        populate_by_name=True,
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
    # 5 minutes: long enough for an investigator to open their authenticator
    # app and type a code, short enough that a leaked `mfa_token` (it grants
    # no session by itself, only a shot at `mfa/login-verify`) is a narrow
    # window of exposure.
    auth_mfa_challenge_ttl_seconds: int = Field(default=300, ge=60)
    # A TOTP code is only 6 digits (1e6 space) -- kept tight per 60-second
    # window, same reasoning as `auth_login_rate_limit`.
    auth_mfa_rate_limit: int = Field(default=8, ge=1)

    # First-admin setup is deliberately opt-in.  The CLI remains the
    # operator fallback; deployments that want the browser ceremony enable
    # it temporarily and provide a high-entropy, one-time deployment secret.
    first_admin_setup_enabled: bool = Field(
        default=False,
        validation_alias="TRACEX_FIRST_ADMIN_SETUP_ENABLED",
    )
    first_admin_setup_token: SecretStr | None = Field(
        default=None,
        validation_alias="TRACEX_FIRST_ADMIN_SETUP_TOKEN",
    )
    first_admin_setup_rate_limit: int = Field(
        default=5,
        ge=1,
        validation_alias="TRACEX_FIRST_ADMIN_SETUP_RATE_LIMIT",
    )

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
    # Absolute ceiling on how far `POST /{job_id}/renew` may ever push a
    # single attempt's `lease_expires_at`, measured from that attempt's own
    # `claimed_at` -- regardless of how many times, or how frequently, the
    # worker heartbeats. Once real time passes `claimed_at +
    # worker_lease_max_seconds`, renewal stops extending the lease (it is
    # capped, via `LEAST(...)`, at the ceiling itself) and the lease expires
    # on schedule, exactly as if the worker had stopped renewing -- a
    # runaway or stuck worker can never hold a job forever just by calling
    # `/renew` frequently enough. See "Lease and retry policy" in
    # docs/architecture/worker-job-lifecycle.md. Must be `>=
    # worker_lease_seconds` (enforced below) -- otherwise a lease could
    # never be renewed even once.
    worker_lease_max_seconds: int = Field(default=3600, ge=1)
    # A `running` job whose lease keeps expiring (a worker that keeps
    # crashing or timing out) is reclaimed and retried up to this many times
    # before `EvidenceLifecycleRepository.sweep_retry_exhausted_jobs`
    # transitions it durably to `failed` (`error.code="retry_exhausted"`)
    # rather than leaving it reclaimable forever -- mirrors
    # `graph_projection_max_attempts`'s identical policy for the graph-
    # projection outbox. See "Lease and retry policy" in
    # docs/architecture/worker-job-lifecycle.md.
    worker_job_max_attempts: int = Field(default=5, ge=1)
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

    # --- Phase 4 LAN worker control plane ---
    # HTTP is deliberately allowed only when this switch is false (the
    # documented local-development exception).  A production/LAN deployment
    # terminates TLS at an explicitly trusted proxy or serves HTTPS directly.
    worker_secure_transport_required: bool = Field(default=False)
    # Comma-separated literal proxy IPs.  Forwarded protocol headers are
    # ignored unless the immediate peer is on this allow-list.
    worker_trusted_proxy_ips: str = Field(default="")
    worker_internal_request_max_bytes: int = Field(default=2_097_152, ge=1024)
    worker_internal_request_timeout_seconds: float = Field(default=30.0, gt=0)
    worker_batch_max_observations: int = Field(default=500, ge=1)
    worker_batch_max_transformations: int = Field(default=500, ge=1)
    worker_media_chunk_max_artifacts: int = Field(default=100, ge=0)
    worker_heartbeat_stale_seconds: int = Field(default=120, ge=1)

    # --- Graph projection (Phase 2 -- Shreshtha) ---
    # How many durable `graph_projection_jobs` rows one `graph.worker --once`
    # invocation claims and processes before exiting. Bounded so a single
    # run has a predictable, finite amount of work -- never "claim
    # everything queued."
    graph_projection_batch_size: int = Field(default=25, ge=1)
    # How long a claimed projection job stays exclusively owned by the
    # claiming projector run before its lease is considered expired and the
    # job becomes eligible for reclaim -- same "lease, not a lock held
    # forever" policy as `worker_lease_seconds` above.
    graph_projection_lease_seconds: int = Field(default=120, ge=1)
    # A projection job whose lease keeps expiring (Neo4j down, a crashing
    # projector run) is reclaimed and retried up to this many times before
    # being left `failed` for operator inspection rather than retried
    # forever -- see "Bounded retries" in docs/architecture/graph-projection.md.
    graph_projection_max_attempts: int = Field(default=5, ge=1)
    # How often (seconds) `graph.worker --loop` renews a claimed batch's
    # projection-job leases while a long batch is still being worked, and
    # how long a graph-projector loop sleeps between empty poll attempts.
    # Independent settings from `graph_projection_lease_seconds` (the lease
    # *duration* itself) -- see docs/architecture/graph-projection.md.
    graph_projection_renew_interval_seconds: int = Field(default=40, ge=1)
    graph_projector_poll_interval_seconds: float = Field(default=5.0, gt=0)
    graph_projector_max_backoff_seconds: float = Field(default=60.0, gt=0)
    graph_projector_max_consecutive_failures: int = Field(default=5, ge=1)

    # --- Phase 7 Gate C release configuration ---
    # This ID must resolve inside the committed, hash-bound Gate C freeze.
    # Arbitrary model names or local paths are never accepted here.  The
    # independent disable switch is the safe operational rollback: when set,
    # correlation generation fails closed before retrieving case data.
    release_configuration_id: str = Field(
        default="tracex-release-v1-baseline", pattern=r"^[a-z0-9_.\-]{1,120}$"
    )
    release_configuration_disabled: bool = Field(default=False)

    # --- Media detection/OCR (Phase 2 closeout -- Nipun) ---
    # Typed, explicit local-model configuration -- see
    # docs/architecture/media-processing-worker.md's "Model asset bootstrap".
    # No model weights are bundled in this repository or downloaded at
    # import time; this path must point at a real local file an operator
    # placed there via `uv run python -m
    # app.modules.media_processing.bootstrap_models`. The default path is
    # exactly where that command writes it, so the common case ("I ran the
    # documented bootstrap command") needs no override.
    media_detector_model_path: Path = Field(
        default=Path("models/media/object_detection_yolox_2022nov.onnx")
    )
    # Pinned to the exact asset `bootstrap_models.py` downloads and verifies
    # -- see that module and `analysis/onnx_detector.py`'s "Model
    # provenance" for the source URL, license, and commit this was built
    # from. Overridable only for a deliberately different, equally-pinned
    # model asset; never intended to disable verification.
    media_detector_model_sha256: str = Field(
        default="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
    )
    media_detector_device: Literal["auto", "cpu", "cuda"] = Field(default="auto")
    media_detector_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    media_detector_nms_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Explicit Phase 4 analysis profile selected by the existing worker CLI.
    media_processing_profile: Literal["rapid", "deep"] = Field(default="rapid")
    # `eng` (English) is the only tesseract language pack this repository's
    # Dockerfile installs by default -- see docs/architecture/media-
    # processing-worker.md's "OCR runtime setup" for adding others.
    media_ocr_language: str = Field(default="eng")
    media_ocr_min_confidence: float = Field(default=0.3, ge=0.0, le=1.0)
    # How often (seconds) `media.worker --loop` renews a claimed job's lease
    # while a long-running video analysis is still in progress -- see
    # docs/architecture/media-processing-worker.md's "Continuous operation".
    media_worker_renew_interval_seconds: int = Field(default=60, ge=1)
    media_worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    media_worker_max_backoff_seconds: float = Field(default=60.0, gt=0)
    media_worker_max_consecutive_failures: int = Field(default=5, ge=1)

    # --- Document/FIR page-OCR and local NER (Phase 3 -- Jasraj) ---
    # See docs/architecture/document-structured-processing.md's "OCR runtime
    # setup"/"Local NER bootstrap". OCR reuses the same `tesseract` system
    # binary media_processing already requires -- no separate install step.
    document_ocr_language: str = Field(default="eng")
    document_ocr_dpi: int = Field(default=300, ge=72, le=600)
    document_ocr_min_confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    # Typed, explicit local-model configuration for the real spaCy NER
    # adapter -- mirrors `media_detector_model_path`'s reasoning exactly.
    # No model weights are bundled in this repository or downloaded at
    # import time; this path must point at a real local directory an
    # operator placed there via `uv run python -m
    # app.modules.structured_processing.bootstrap_ner_model`. The default
    # is exactly where that command writes it.
    ner_model_path: Path = Field(default=Path("models/nlp/en_core_web_sm"))
    # Pinned to the exact asset `bootstrap_ner_model.py` downloads and
    # verifies -- see that module's module docstring for the source URL,
    # license, and version this was built from.
    ner_model_sha256: str = Field(
        default="1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
    )

    # --- CDR/finance chunked batch processing (Phase 3 -- Jasraj) ---
    # How many normalized records one micro-batch submits to Nipun's
    # observation-batch endpoint at a time -- see
    # docs/architecture/document-structured-processing.md.
    structured_batch_size: int = Field(default=500, ge=1)
    # Naive (no explicit source timezone) CDR/finance timestamps are
    # interpreted in this fixed, documented timezone before being converted
    # to the canonical UTC value that is actually stored -- never guessed
    # per-file. Must be a valid IANA timezone name.
    structured_default_timezone: str = Field(default="Asia/Kolkata")

    # --- Audio/social-chat chunked batch processing (Phase 3 -- Sarthak) ---
    # How many transcript/diarization segments or chat messages one
    # micro-batch submits to Nipun's observation-batch endpoint at a time --
    # see docs/architecture/communication-processing.md.
    communication_batch_size: int = Field(default=200, ge=1)
    # Naive (no explicit offset/epoch signal) chat timestamps are
    # interpreted in this fixed, documented timezone before being converted
    # to the canonical UTC value that is actually stored -- never silently
    # interpreted in the host machine's own local time. Must be a valid
    # IANA timezone name. Deliberately the same default as
    # `structured_default_timezone` -- both modules process the same class
    # of India-context evidence.
    communication_default_timezone: str = Field(default="Asia/Kolkata")
    # Phase 4 local-only audio backends.  Each command is an operator-owned
    # executable path (not a shell fragment); models stay outside Git and
    # are never downloaded by the worker.  Missing paths yield a truthful
    # deferred audio outcome.
    communication_audio_profile: Literal["rapid", "deep"] = Field(default="rapid")
    communication_asr_command: Path | None = Field(default=None)
    communication_asr_model_path: Path | None = Field(default=None)
    communication_asr_language: str = Field(default="auto")
    communication_asr_timeout_seconds: float = Field(default=120.0, gt=0)
    communication_diarization_command: Path | None = Field(default=None)
    communication_diarization_model_path: Path | None = Field(default=None)
    communication_diarization_timeout_seconds: float = Field(default=120.0, gt=0)

    # --- Tamper-evident integrity checkpoints (Phase 6 -- Nipun) ---
    # Local Ed25519 signing key for Merkle checkpoint roots: base64-encoded
    # 32-byte raw private key. No external KMS/HSM -- see
    # docs/runbooks/local-development.md for the dev-only generation
    # workflow (`uv run python -m app.modules.integrity.cli generate-key`).
    # Never logged, returned in an API/CLI response, included in an error,
    # or committed -- only a blank placeholder ships in `.env.example`.
    integrity_signing_key: SecretStr | None = Field(default=None)
    # Free-text label stored alongside every signature so a verifier can
    # tell which configured key produced it; not itself secret.
    integrity_signing_key_id: str = Field(default="dev-local-ed25519-1")
    # Gap-Closure WP-5 (G4): a separate MinIO bucket (never `minio_bucket`,
    # which holds raw evidence) for durable, write-once verification-bundle
    # archives. Reuses the same MinIO credentials/endpoint above.
    integrity_manifest_bucket: str = Field(default="tracex-integrity-manifests")
    # Local filesystem root for `FilesystemManifestSink` -- an operator-
    # mounted archive volume in production, a repo-local scratch dir in dev.
    integrity_manifest_filesystem_root: Path = Field(default=Path("./data/integrity-manifests"))
    # Gap-Closure WP-5 (G4) re-close: `MinioManifestSink.ensure_bucket`
    # creates `integrity_manifest_bucket` with MinIO object-lock enabled
    # (only possible at bucket creation -- cannot be retrofitted) and a
    # default COMPLIANCE-mode retention of this many years -- COMPLIANCE,
    # not GOVERNANCE, because no principal (not even a MinIO admin) may
    # delete or overwrite an archived checkpoint manifest before its
    # retention expires. This is a real, hard-to-reverse operational
    # choice -- see docs/runbooks/integrity-verification.md and
    # ADR-023 before changing it in a live deployment.
    integrity_manifest_retention_years: int = Field(default=10, ge=1, le=100)

    @field_validator(
        "worker_token",
        "worker_credential_pepper",
        "integrity_signing_key",
        "first_admin_setup_token",
    )
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

    @model_validator(mode="after")
    def _validate_worker_lease_max_covers_base_lease(self) -> Settings:
        """`worker_lease_max_seconds` must be able to grant at least one real renewal.

        If it were smaller than `worker_lease_seconds`, the very first
        `/renew` call would already be capped below the lease a job starts
        with -- a configuration that can never actually renew anything,
        almost certainly a typo rather than an intentional policy.
        """
        if self.worker_lease_max_seconds < self.worker_lease_seconds:
            raise ValueError(
                "worker_lease_max_seconds must be >= worker_lease_seconds "
                f"(got {self.worker_lease_max_seconds} < {self.worker_lease_seconds})"
            )
        if self.worker_heartbeat_stale_seconds > self.worker_lease_seconds:
            raise ValueError("worker_heartbeat_stale_seconds must be <= worker_lease_seconds")
        if self.first_admin_setup_enabled and self.app_env is AppEnv.PRODUCTION:
            token = (
                self.first_admin_setup_token.get_secret_value()
                if self.first_admin_setup_token
                else ""
            )
            if len(token) < _MIN_FIRST_ADMIN_SETUP_TOKEN_LENGTH:
                raise ValueError(
                    "first_admin_setup_token must be at least "
                    f"{_MIN_FIRST_ADMIN_SETUP_TOKEN_LENGTH} characters when first-admin "
                    "setup is enabled in production"
                )
        return self


def get_settings() -> Settings:
    """Build a fresh `Settings` instance from the current environment.

    Not cached at module scope so that tests can freely construct settings
    against different environments without cross-test leakage.
    """
    return Settings()
