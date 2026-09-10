"""Typed application configuration loaded from environment variables.

All settings are declared with no implicit defaults for values that are
required for the service to run correctly outside of local development.
Missing or malformed values fail fast with a clear validation error at
process startup rather than surfacing as confusing runtime errors later.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


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


def get_settings() -> Settings:
    """Build a fresh `Settings` instance from the current environment.

    Not cached at module scope so that tests can freely construct settings
    against different environments without cross-test leakage.
    """
    return Settings()
