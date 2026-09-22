"""Gap-Closure WP-5 (G4): durable, write-once archival of verification bundles.

A `ManifestSink` is a place to durably archive a `VerificationBundle`
export outside PostgreSQL -- defense in depth for this module's own
tamper-evidence claim: if the database were ever compromised or rolled
back, an independently stored copy of a checkpoint's public verification
material still exists.

`FilesystemManifestSink`'s write-once guarantee is application-level only
(refuses to overwrite an existing path). `MinioManifestSink` (Gap-Closure
re-close) is **infrastructure-enforced**: `ensure_bucket` creates its
bucket with MinIO object-lock enabled and a default `COMPLIANCE`-mode
retention (`Settings.integrity_manifest_retention_years`) -- under
`COMPLIANCE` mode, no principal, not even a MinIO admin with root
credentials, can delete or overwrite an object before its retention
expires. Object-lock can only be enabled at bucket *creation* -- an
already-existing bucket cannot have it retrofitted; `ensure_bucket` only
configures it for a bucket it creates itself, and documents (via
`ManifestSinkError`) when it cannot.

Both sinks write exactly the same bytes `IntegrityService.export_
verification_bundle` already produces (public verification material only
-- no raw evidence content, no private key) -- this module never
constructs or validates that payload itself, only stores it.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Protocol
from uuid import UUID

from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.error import S3Error
from minio.objectlockconfig import YEARS, ObjectLockConfig

from app.core.config import Settings


class ManifestAlreadyExistsError(RuntimeError):
    """A manifest already exists for this (case_id, checkpoint_id) -- never overwritten."""


class ManifestSinkError(RuntimeError):
    """Any other write failure -- never leaks driver-internal text."""


def manifest_key(case_id: UUID, checkpoint_id: UUID) -> str:
    return f"cases/{case_id}/checkpoints/{checkpoint_id}/verification-bundle.json"


class ManifestSink(Protocol):
    async def write_manifest(self, *, case_id: UUID, checkpoint_id: UUID, payload: bytes) -> str:
        """Durably write one manifest. Returns an opaque, sink-specific locator.

        Raises `ManifestAlreadyExistsError` if a manifest already exists
        for this `(case_id, checkpoint_id)`.
        """
        ...


class FilesystemManifestSink:
    """Local-disk sink: an operator-mounted archive volume in production, a
    repo-local scratch directory in dev (`Settings.integrity_manifest_filesystem_root`)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def write_manifest(self, *, case_id: UUID, checkpoint_id: UUID, payload: bytes) -> str:
        path = self._root / manifest_key(case_id, checkpoint_id)
        await asyncio.to_thread(self._write, path, payload)
        return str(path)

    def _write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # "xb": raises FileExistsError if the path is already occupied --
            # the write-once guarantee, enforced by the filesystem itself.
            with open(path, "xb") as handle:
                handle.write(payload)
        except FileExistsError as exc:
            raise ManifestAlreadyExistsError(f"a manifest already exists at {path}") from exc
        except OSError as exc:
            raise ManifestSinkError("failed to write manifest to the filesystem sink") from exc


class MinioManifestSink:
    """MinIO sink, in a bucket separate from raw evidence
    (`Settings.integrity_manifest_bucket`, never `minio_bucket`)."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.integrity_manifest_bucket
        self._retention_years = settings.integrity_manifest_retention_years
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        """Idempotent, but object-lock can only be configured on a bucket
        *this call itself creates* -- MinIO (and S3) has no API to
        retroactively enable object-lock on an already-existing bucket.
        If the bucket already exists (created before this WP, or by an
        operator directly), this call leaves its object-lock/retention
        configuration exactly as it already is -- never silently claims
        write-once protection that isn't actually there.
        """
        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
            if exists:
                return
            await asyncio.to_thread(self._client.make_bucket, self._bucket, None, True)
            await asyncio.to_thread(
                self._client.set_object_lock_config,
                self._bucket,
                ObjectLockConfig(COMPLIANCE, self._retention_years, YEARS),
            )
        except Exception as exc:
            raise ManifestSinkError("failed to initialize the manifest archive bucket") from exc

    async def write_manifest(self, *, case_id: UUID, checkpoint_id: UUID, payload: bytes) -> str:
        key = manifest_key(case_id, checkpoint_id)
        if await asyncio.to_thread(self._object_exists, key):
            raise ManifestAlreadyExistsError(f"a manifest already exists at {key}")
        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                key,
                io.BytesIO(payload),
                length=len(payload),
                content_type="application/json",
            )
        except Exception as exc:
            raise ManifestSinkError("failed to write manifest to object storage") from exc
        return f"minio://{self._bucket}/{key}"

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return False
            raise


__all__ = [
    "FilesystemManifestSink",
    "ManifestAlreadyExistsError",
    "ManifestSink",
    "ManifestSinkError",
    "MinioManifestSink",
    "manifest_key",
]
