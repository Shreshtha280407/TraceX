"""Streaming, quota-bound storage of immutable uploaded source bytes."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile


class UploadRejected(ValueError):
    pass


SUPPORTED_SUFFIXES = {".csv": "csv", ".json": "json", ".ndjson": "ndjson", ".xml": "xml"}
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class StoredSource:
    sha256: str
    byte_size: int
    filename: str
    source_format: str
    relative_path: str


def normalized_filename(filename: str | None) -> str:
    candidate = SAFE_FILENAME.sub("_", Path(filename or "upload.bin").name).strip("._")
    if not candidate:
        raise UploadRejected("A usable filename is required")
    return candidate[:255]


def source_format(filename: str) -> str:
    fmt = SUPPORTED_SUFFIXES.get(Path(filename).suffix.lower())
    if not fmt:
        raise UploadRejected("Only .csv, .json, .ndjson, and .xml uploads are supported")
    return fmt


async def store_upload(upload: UploadFile, *, case_id: str, evidence_root: Path, max_bytes: int) -> StoredSource:
    """Hash while writing staging bytes, then atomically publish an immutable file."""
    filename = normalized_filename(upload.filename)
    fmt = source_format(filename)
    staging = evidence_root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"{uuid.uuid4()}.part"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(f"Upload exceeds configured {max_bytes}-byte limit")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        source_hash = digest.hexdigest()
        relative = Path(case_id) / source_hash / "original"
        target = evidence_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with target.open("rb") as handle:
                existing_digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if existing_digest != source_hash:
                raise RuntimeError("Immutable evidence target digest mismatch")
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, target)
        return StoredSource(source_hash, size, filename, fmt, relative.as_posix())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def resolve_source(evidence_root: Path, relative_path: str) -> Path:
    candidate = (evidence_root / relative_path).resolve()
    root = evidence_root.resolve()
    if root not in candidate.parents or candidate.name != "original":
        raise RuntimeError("Invalid evidence path stored in control plane")
    return candidate
