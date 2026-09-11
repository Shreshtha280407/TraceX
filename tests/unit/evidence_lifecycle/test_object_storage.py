"""Scenario 4 (unit level): `object_key_for` is deterministic and input-safe.

Also covers Phase 2.2's `open_stream`/`_stream_response`: the worker
input-delivery endpoint's core "never fully buffer the object in API
memory" property, proven directly against `_stream_response` (not through
an HTTP client, whose transport-level chunk buffering is not something
this code controls -- see `tests/unit/evidence_lifecycle/
test_worker_input_api.py`'s multi-chunk test for the end-to-end wiring
proof instead).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest

from app.modules.evidence_lifecycle.errors import StorageError
from app.modules.evidence_lifecycle.storage import (
    FakeObjectStorage,
    _stream_response,
    object_key_for,
)


def test_object_key_is_deterministic_from_ids_alone() -> None:
    case_id = uuid4()
    evidence_id = uuid4()
    assert object_key_for(case_id, evidence_id) == object_key_for(case_id, evidence_id)
    assert (
        object_key_for(case_id, evidence_id) == f"cases/{case_id}/evidence/{evidence_id}/original"
    )


def test_object_key_never_takes_a_filename_argument() -> None:
    # object_key_for's signature structurally cannot accept a filename --
    # this is a documentation-as-code check that the function stays two-arity.
    import inspect

    params = list(inspect.signature(object_key_for).parameters)
    assert params == ["case_id", "evidence_id"]


async def test_fake_storage_round_trips_bytes() -> None:
    import io

    storage = FakeObjectStorage()
    key = object_key_for(uuid4(), uuid4())
    await storage.put_object(key, io.BytesIO(b"hello"), 5, "text/plain")
    assert storage.read_bytes(key) == b"hello"


class _FakeMinioResponse:
    """Mimics the slice of minio-py's `get_object` response `_stream_response` uses."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
        self.pulled = 0
        self.closed = False
        self.released = False

    def stream(self, chunk_size: int) -> Iterator[bytes]:  # noqa: ARG002
        for chunk in self._chunks:
            self.pulled += 1
            yield chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


async def test_stream_response_pulls_chunks_lazily_and_closes_the_response() -> None:
    """Proves `open_stream` never reads the whole object before the caller asks for it.

    This is the property the worker input-delivery endpoint's "stream, don't
    buffer" requirement actually rests on -- checked here directly against
    `_stream_response`, since an HTTP client's own transport-level chunk
    buffering would make this unreliable to assert end to end.
    """
    fake_response = _FakeMinioResponse([b"a" * 10, b"b" * 10, b"c" * 10])
    stream = _stream_response(fake_response)
    assert fake_response.pulled == 0  # constructing the generator pulls nothing

    first = await stream.__anext__()
    assert first == b"a" * 10
    assert fake_response.pulled == 1  # exactly one chunk pulled, not all three

    remaining = [chunk async for chunk in stream]
    assert remaining == [b"b" * 10, b"c" * 10]
    assert fake_response.pulled == 3
    assert fake_response.closed is True
    assert fake_response.released is True


async def test_fake_storage_open_stream_round_trips_bytes() -> None:
    storage = FakeObjectStorage()
    key = object_key_for(uuid4(), uuid4())
    storage.objects[key] = b"hello stream"

    stream = await storage.open_stream(key)

    assert stream.content_length == len(b"hello stream")
    assert b"".join([chunk async for chunk in stream.chunks]) == b"hello stream"


async def test_fake_storage_open_stream_raises_safely_when_object_missing() -> None:
    storage = FakeObjectStorage()
    with pytest.raises(StorageError):
        await storage.open_stream("never-written")
