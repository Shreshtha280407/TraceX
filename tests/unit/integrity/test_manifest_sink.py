"""Gap-Closure WP-5 (G4): `FilesystemManifestSink` coverage -- pure, no live infra needed.

`MinioManifestSink`'s equivalent behavior is exercised live against a real
bucket in `tests/integration/integrity/test_manifest_sink_live.py`.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.integrity.manifest_sink import (
    FilesystemManifestSink,
    ManifestAlreadyExistsError,
    manifest_key,
)

_PAYLOAD = b'{"checkpoint_id": "synthetic", "root_hash": "a" * 64}'


async def test_write_manifest_creates_the_file_at_the_expected_key(tmp_path: Path) -> None:
    sink = FilesystemManifestSink(tmp_path)
    case_id, checkpoint_id = uuid4(), uuid4()

    locator = await sink.write_manifest(
        case_id=case_id, checkpoint_id=checkpoint_id, payload=_PAYLOAD
    )

    expected_path = tmp_path / manifest_key(case_id, checkpoint_id)
    assert locator == str(expected_path)
    assert expected_path.read_bytes() == _PAYLOAD


async def test_write_manifest_refuses_to_overwrite_an_existing_archive(tmp_path: Path) -> None:
    sink = FilesystemManifestSink(tmp_path)
    case_id, checkpoint_id = uuid4(), uuid4()

    await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_id, payload=_PAYLOAD)
    with pytest.raises(ManifestAlreadyExistsError):
        await sink.write_manifest(
            case_id=case_id, checkpoint_id=checkpoint_id, payload=b"different-payload"
        )
    # The original archive is untouched by the rejected second write.
    expected_path = tmp_path / manifest_key(case_id, checkpoint_id)
    assert expected_path.read_bytes() == _PAYLOAD


async def test_write_manifest_is_case_and_checkpoint_scoped(tmp_path: Path) -> None:
    sink = FilesystemManifestSink(tmp_path)
    case_id = uuid4()
    checkpoint_a, checkpoint_b = uuid4(), uuid4()

    await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_a, payload=b"a")
    await sink.write_manifest(case_id=case_id, checkpoint_id=checkpoint_b, payload=b"b")

    assert (tmp_path / manifest_key(case_id, checkpoint_a)).read_bytes() == b"a"
    assert (tmp_path / manifest_key(case_id, checkpoint_b)).read_bytes() == b"b"
