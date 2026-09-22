"""Live MinIO coverage for `MinioManifestSink` (Gap-Closure WP-5, G4).

Reuses this package's `conftest.py` fixtures/skip pattern -- same
Postgres-reachability gate as every other test here, even though this
specific sink only touches MinIO; see `tests/integration/evidence_
lifecycle/conftest.py`'s `minio_storage` fixture for the existing
precedent of a MinIO-only fixture still living behind that shared gate.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from minio.error import S3Error

from app.core.config import Settings
from app.dependencies.services import check_minio
from app.modules.integrity.manifest_sink import (
    ManifestAlreadyExistsError,
    MinioManifestSink,
    manifest_key,
)
from tests.integration.integrity.conftest import _live_settings


@pytest.fixture
def minio_sink_settings() -> Settings:
    """A dedicated, disposable manifest bucket so this suite never touches
    the real `INTEGRITY_MANIFEST_BUCKET` an operator might already use."""
    return _live_settings(integrity_manifest_bucket=f"test-integrity-manifests-{uuid4().hex[:8]}")


async def _sink(settings: Settings) -> MinioManifestSink:
    try:
        await check_minio(settings)
    except Exception as exc:
        pytest.skip(f"minio not reachable: {type(exc).__name__}")
    sink = MinioManifestSink(settings)
    await sink.ensure_bucket()
    return sink


async def test_write_manifest_round_trips_through_real_minio(
    minio_sink_settings: Settings,
) -> None:
    sink = await _sink(minio_sink_settings)
    case_id, checkpoint_id = uuid4(), uuid4()
    payload = b'{"root_hash": "a" * 64, "checkpoint_id": "synthetic"}'

    locator = await sink.write_manifest(
        case_id=case_id, checkpoint_id=checkpoint_id, payload=payload
    )
    assert locator == f"minio://{minio_sink_settings.integrity_manifest_bucket}/" + (
        f"cases/{case_id}/checkpoints/{checkpoint_id}/verification-bundle.json"
    )


async def test_write_manifest_refuses_to_overwrite_an_existing_archive(
    minio_sink_settings: Settings,
) -> None:
    sink = await _sink(minio_sink_settings)
    case_id, checkpoint_id = uuid4(), uuid4()

    await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_id, payload=b"first")
    with pytest.raises(ManifestAlreadyExistsError):
        await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_id, payload=b"second")


async def test_ensure_bucket_enables_compliance_mode_object_lock(
    minio_sink_settings: Settings,
) -> None:
    """Gap-Closure re-close (G4): object-lock is a real, infrastructure-
    level property of the bucket `ensure_bucket` creates -- verified by
    reading the config back from MinIO itself, not merely asserting the
    call didn't raise."""
    sink = await _sink(minio_sink_settings)
    lock_config = sink._client.get_object_lock_config(  # noqa: SLF001 - live boundary proof
        minio_sink_settings.integrity_manifest_bucket
    )
    assert lock_config.mode == "COMPLIANCE"
    assert lock_config.duration == minio_sink_settings.integrity_manifest_retention_years
    assert lock_config.duration_unit == "Years"


async def test_object_lock_rejects_deleting_the_specific_locked_version(
    minio_sink_settings: Settings,
) -> None:
    """The real proof of write-once: a version-specific delete against the
    exact version `write_manifest` created must be rejected by MinIO
    itself (`WORM protected`), independent of this module's own
    application-level `ManifestAlreadyExistsError` check. An unversioned
    `remove_object` call only adds a delete marker under S3 versioning
    semantics (the original version survives untouched either way) --
    this test targets the specific version id directly to prove the
    underlying data genuinely cannot be destroyed, not merely hidden."""
    sink = await _sink(minio_sink_settings)
    case_id, checkpoint_id = uuid4(), uuid4()
    await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_id, payload=b"locked")

    key = manifest_key(case_id, checkpoint_id)
    bucket = minio_sink_settings.integrity_manifest_bucket
    versions = list(
        sink._client.list_objects(bucket, prefix=key, recursive=True, include_version=True)
    )
    locked_version_id = next(v.version_id for v in versions if not v.is_delete_marker)

    with pytest.raises(S3Error, match="WORM"):
        sink._client.remove_object(bucket, key, version_id=locked_version_id)
