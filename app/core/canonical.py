"""Canonical JSON serialization.

Provides a single, stable byte representation for any contract payload:
object keys are sorted recursively, UUIDs and datetimes are normalized, and
enums and nested Pydantic models serialize to their plain values. Two
logically-equal payloads built in different key order or field order always
produce identical bytes, which is the property later phases (hashing,
Merkle checkpoints, signatures) will depend on.

This module intentionally stops at "stable bytes + a SHA-256 helper over
them". Merkle trees, chained hashes, and signatures are later-phase work.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

import orjson
from pydantic import BaseModel

_ORJSON_OPTIONS = orjson.OPT_SORT_KEYS


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise TypeError(f"Object of type {type(obj).__name__} is not canonically serializable")


def canonical_bytes(payload: BaseModel | dict[str, Any]) -> bytes:
    """Serialize a contract payload to canonical, key-sorted JSON bytes."""
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return orjson.dumps(data, option=_ORJSON_OPTIONS, default=_default)


def canonical_sha256(payload: BaseModel | dict[str, Any]) -> str:
    """SHA-256 hex digest of a payload's canonical byte representation."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
