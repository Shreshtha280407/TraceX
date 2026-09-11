"""Scenario 4 (unit level): `object_key_for` is deterministic and input-safe."""

from __future__ import annotations

from uuid import uuid4

from app.modules.evidence_lifecycle.storage import FakeObjectStorage, object_key_for


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
