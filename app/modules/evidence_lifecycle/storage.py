"""Private object storage: object-key derivation, a `MinioObjectStorage` adapter, and a fake.

`ObjectStorage` is the interface `service.py` depends on so tests never
need a real MinIO. `MinioObjectStorage` mirrors
`app.dependencies.services.check_minio`'s client-construction pattern
exactly: minio-py has no async API, so every blocking call is offloaded to
a thread. The bucket it writes to is never made public and no method here
ever returns a presigned or permanent URL -- see
`docs/architecture/evidence-lifecycle.md`.

`MinioSourceResolver` is the internal storage-resolver abstraction later-
phase worker orchestration uses instead of an HTTP download endpoint (see
"no unrestricted raw-evidence-download APIs" in the same doc). It
structurally implements the `SourceResolver` protocol every processing
module already defines (`read_bytes(object_uri) -> bytes`) without
importing any of those sibling modules.
"""

from __future__ import annotations

import asyncio
from typing import BinaryIO, Protocol
from uuid import UUID

from minio import Minio

from app.core.config import Settings
from app.modules.evidence_lifecycle.errors import StorageError


def object_key_for(case_id: UUID, evidence_id: UUID) -> str:
    """The one and only private object key an evidence upload is ever stored at.

    Deterministic from `case_id`/`evidence_id` alone -- never the caller's
    filename or any other untrusted input -- so it is always safe to use
    directly as a MinIO object key.
    """
    return f"cases/{case_id}/evidence/{evidence_id}/original"


class ObjectStorage(Protocol):
    async def ensure_bucket(self) -> None:
        """Idempotent: safe to call on every startup, any number of times."""
        ...

    async def put_object(
        self, object_key: str, data: BinaryIO, size: int, content_type: str
    ) -> None: ...

    async def delete_object(self, object_key: str) -> None: ...

    def read_bytes(self, object_uri: str) -> bytes:
        """Synchronous -- satisfies every processing module's `SourceResolver` protocol."""
        ...


class MinioObjectStorage:
    """The real, private-bucket-only `ObjectStorage` implementation."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    async def ensure_bucket(self) -> None:
        try:
            exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
            if not exists:
                await asyncio.to_thread(self._client.make_bucket, self._bucket)
        except Exception as exc:
            raise StorageError("failed to initialize the evidence object storage bucket") from exc

    async def put_object(
        self, object_key: str, data: BinaryIO, size: int, content_type: str
    ) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                object_key,
                data,
                length=size,
                content_type=content_type,
            )
        except Exception as exc:
            raise StorageError("failed to write evidence to object storage") from exc

    async def delete_object(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(self._client.remove_object, self._bucket, object_key)
        except Exception as exc:
            raise StorageError("failed to delete an evidence object") from exc

    def read_bytes(self, object_uri: str) -> bytes:
        response = self._client.get_object(self._bucket, object_uri)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


class FakeObjectStorage:
    """In-memory `ObjectStorage` stand-in for unit tests.

    `fail_put`/`fail_delete` simulate a storage-layer failure so
    `service.py`'s cleanup/error-handling paths are exercisable without a
    real MinIO instance.
    """

    def __init__(self, *, fail_put: bool = False, fail_delete: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_put = fail_put
        self.fail_delete = fail_delete
        self.deleted_keys: list[str] = []

    async def ensure_bucket(self) -> None:
        return None

    async def put_object(
        self, object_key: str, data: BinaryIO, size: int, content_type: str
    ) -> None:
        if self.fail_put:
            raise StorageError("simulated storage failure")
        self.objects[object_key] = data.read()

    async def delete_object(self, object_key: str) -> None:
        self.deleted_keys.append(object_key)
        if self.fail_delete:
            raise StorageError("simulated cleanup failure")
        self.objects.pop(object_key, None)

    def read_bytes(self, object_uri: str) -> bytes:
        return self.objects[object_uri]


class MinioSourceResolver:
    """A `SourceResolver` backed by a real, credentialed `MinioObjectStorage`.

    Constructed and held only by trusted orchestration code (never handed
    to an HTTP caller); a worker given this resolver calls `read_bytes`
    without ever seeing a MinIO credential.
    """

    def __init__(self, storage: MinioObjectStorage) -> None:
        self._storage = storage

    def read_bytes(self, object_uri: str) -> bytes:
        return self._storage.read_bytes(object_uri)
