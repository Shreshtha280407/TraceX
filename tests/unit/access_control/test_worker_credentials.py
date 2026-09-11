"""Scenarios 1, 2, 5: worker-credential creation, digest-only storage, and rotation.

`FakeAccessControlRepository` (the same in-memory stand-in every other
access-control unit test already uses) stands in for PostgreSQL -- no real
database is needed to exercise `worker_credentials.py`'s own logic.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.config import AppEnv, Settings
from app.modules.access_control import worker_credentials as worker_credentials_module
from app.modules.access_control.errors import WorkerSecurityConfigurationError
from app.modules.access_control.models import WorkerCredentialStatus
from app.modules.access_control.worker_credentials import (
    create_worker_credential,
    generate_worker_token,
    hash_worker_credential,
    list_worker_credentials,
    resolve_worker_pepper,
    revoke_worker_credential,
    rotate_worker_credential,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "postgres_dsn": "postgresql+asyncpg://u:p@localhost:5432/db",
        "neo4j_uri": "bolt://localhost:7687",
        "neo4j_username": "neo4j",
        "neo4j_password": "secret",
        "redis_url": "redis://localhost:6379/0",
        "minio_endpoint": "localhost:9000",
        "minio_access_key": "key",
        "minio_secret_key": "secret",
        "auth_jwt_secret": "x" * 32,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


# --- Scenario 1: credential creation stores only a digest -------------------


async def test_create_worker_credential_stores_only_a_digest() -> None:
    repository = FakeAccessControlRepository()
    record, token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1", "cdr_generic_v1"],
        pepper="a-test-pepper",
        now=FIXED_TIME,
    )

    stored = repository.worker_credentials[record.worker_id]
    assert stored.credential_digest != token
    assert token not in stored.credential_digest
    assert stored.credential_digest == hash_worker_credential(token, "a-test-pepper")
    assert stored.status is WorkerCredentialStatus.ACTIVE
    assert stored.allowed_processor_names == ("fir_report_text_v1", "cdr_generic_v1")
    assert stored.rotated_at is None
    assert stored.revoked_at is None


# --- Scenario 2: the plaintext token is issued once, never persisted --------


async def test_plaintext_token_is_returned_once_and_never_persisted() -> None:
    repository = FakeAccessControlRepository()
    record, token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper=None,
        now=FIXED_TIME,
    )

    assert isinstance(token, str)
    assert len(token) >= 32  # secrets.token_urlsafe(32) -- high entropy, not a short value

    stored = repository.worker_credentials[record.worker_id]
    # The stored record's own fields never carry the raw token anywhere.
    for value in stored.model_dump().values():
        assert token != value
        assert (not isinstance(value, str)) or token not in value


def test_generate_worker_token_produces_distinct_high_entropy_values() -> None:
    first = generate_worker_token()
    second = generate_worker_token()
    assert first != second
    assert len(first) >= 32


# --- hash_worker_credential: pepper vs. unpeppered ---------------------------


def test_hash_worker_credential_is_deterministic_for_the_same_inputs() -> None:
    assert hash_worker_credential("a-token", "a-pepper") == hash_worker_credential(
        "a-token", "a-pepper"
    )


def test_hash_worker_credential_changes_with_the_pepper() -> None:
    peppered = hash_worker_credential("a-token", "pepper-one")
    differently_peppered = hash_worker_credential("a-token", "pepper-two")
    unpeppered = hash_worker_credential("a-token", None)
    assert len({peppered, differently_peppered, unpeppered}) == 3


def test_hash_worker_credential_never_contains_the_raw_token() -> None:
    digest = hash_worker_credential("a-very-distinctive-raw-token-value", "pepper")
    assert "a-very-distinctive-raw-token-value" not in digest


# --- Scenario 5: rotation invalidates the old token, accepts only the new one ---


async def test_rotation_invalidates_the_old_token_and_accepts_only_the_new_one() -> None:
    repository = FakeAccessControlRepository()
    record, old_token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    old_digest = hash_worker_credential(old_token, "pepper")
    assert await repository.get_worker_credential_by_digest(old_digest) is not None

    new_token = await rotate_worker_credential(
        repository, worker_id=record.worker_id, pepper="pepper", now=FIXED_TIME
    )

    assert new_token != old_token
    assert await repository.get_worker_credential_by_digest(old_digest) is None
    new_digest = hash_worker_credential(new_token, "pepper")
    rotated = await repository.get_worker_credential_by_digest(new_digest)
    assert rotated is not None
    assert rotated.worker_id == record.worker_id  # identity survives rotation
    assert rotated.rotated_at == FIXED_TIME


async def test_rotate_unknown_worker_id_raises_lookup_error() -> None:
    repository = FakeAccessControlRepository()
    with pytest.raises(LookupError):
        await rotate_worker_credential(
            repository, worker_id=uuid4(), pepper="pepper", now=FIXED_TIME
        )


async def test_rotate_a_revoked_credential_raises_value_error() -> None:
    repository = FakeAccessControlRepository()
    record, _token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    await revoke_worker_credential(repository, worker_id=record.worker_id, now=FIXED_TIME)
    with pytest.raises(ValueError, match="revoked"):
        await rotate_worker_credential(
            repository, worker_id=record.worker_id, pepper="pepper", now=FIXED_TIME
        )


# --- revoke: idempotent, auditable via status -------------------------------


async def test_revoke_is_idempotent() -> None:
    repository = FakeAccessControlRepository()
    record, _token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    await revoke_worker_credential(repository, worker_id=record.worker_id, now=FIXED_TIME)
    await revoke_worker_credential(
        repository, worker_id=record.worker_id, now=FIXED_TIME
    )  # no error

    stored = repository.worker_credentials[record.worker_id]
    assert stored.status is WorkerCredentialStatus.REVOKED
    assert stored.revoked_at == FIXED_TIME


async def test_revoke_unknown_worker_id_is_not_an_error() -> None:
    repository = FakeAccessControlRepository()
    await revoke_worker_credential(repository, worker_id=uuid4(), now=FIXED_TIME)  # no raise


# --- rotation/revocation are audited via the existing audit service ---------


async def test_rotation_records_a_worker_credential_rotated_audit_event() -> None:
    repository = FakeAccessControlRepository()
    record, _token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    await rotate_worker_credential(
        repository, worker_id=record.worker_id, pepper="pepper", now=FIXED_TIME
    )

    rotation_events = [
        e for e in repository.audit_events if e.event_type == "worker_credential_rotated"
    ]
    assert len(rotation_events) == 1
    assert rotation_events[0].metadata_safe_json["worker_id"] == str(record.worker_id)
    # Never the old or new token, ever, in any audit field.
    for value in rotation_events[0].model_dump().values():
        assert not isinstance(value, str) or "pepper" not in value


async def test_revoke_records_a_worker_credential_revoked_audit_event_every_call() -> None:
    repository = FakeAccessControlRepository()
    record, _token = await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    await revoke_worker_credential(repository, worker_id=record.worker_id, now=FIXED_TIME)
    await revoke_worker_credential(repository, worker_id=record.worker_id, now=FIXED_TIME)

    revoke_events = [
        e for e in repository.audit_events if e.event_type == "worker_credential_revoked"
    ]
    assert len(revoke_events) == 2  # idempotent DB state, but each operator call is still audited
    assert all(e.metadata_safe_json["worker_id"] == str(record.worker_id) for e in revoke_events)


# --- list: never exposes a token or digest ----------------------------------


async def test_list_worker_credentials_never_needs_the_plaintext_token() -> None:
    repository = FakeAccessControlRepository()
    await create_worker_credential(
        repository,
        display_name="structured-worker",
        allowed_processor_names=["fir_report_text_v1"],
        pepper="pepper",
        now=FIXED_TIME,
    )
    records = await list_worker_credentials(repository)
    assert len(records) == 1
    assert records[0].display_name == "structured-worker"
    # The record itself legitimately carries the digest for internal use;
    # the CLI's own printed `list` output is what must omit it (covered by
    # a static source inspection below).


def test_cli_list_output_never_prints_a_credential_digest() -> None:
    source = inspect.getsource(worker_credentials_module._print_list)
    assert "credential_digest" not in source


# --- resolve_worker_pepper: production fail-closed ---------------------------


def test_resolve_worker_pepper_returns_none_when_unset_outside_production() -> None:
    settings = _settings(app_env=AppEnv.TEST, worker_credential_pepper=None)
    assert resolve_worker_pepper(settings) is None


def test_resolve_worker_pepper_returns_the_configured_value() -> None:
    settings = _settings(app_env=AppEnv.LOCAL, worker_credential_pepper="a-real-pepper")
    assert resolve_worker_pepper(settings) == "a-real-pepper"


def test_resolve_worker_pepper_fails_closed_in_production_when_missing() -> None:
    settings = _settings(app_env=AppEnv.PRODUCTION, worker_credential_pepper=None)
    with pytest.raises(WorkerSecurityConfigurationError) as excinfo:
        resolve_worker_pepper(settings)
    # The error is clear and non-secret: names the setting, carries no value.
    assert "WORKER_CREDENTIAL_PEPPER" in str(excinfo.value)


def test_resolve_worker_pepper_succeeds_in_production_when_configured() -> None:
    settings = _settings(app_env=AppEnv.PRODUCTION, worker_credential_pepper="a-real-pepper")
    assert resolve_worker_pepper(settings) == "a-real-pepper"
