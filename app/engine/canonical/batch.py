"""Normalize a batch of parsed source rows: the CPU-heavy half of parsing.

Used in-process for small imports and by worker processes
(app.engine.process_pool) for large ones; either way each row yields exactly
what the ingestion loop needs to accept, deduplicate or quarantine it, with
every fact's canonical JSON (the fragment column) already serialized.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, NamedTuple

from app.engine.adapters import ParsedRow
from app.engine.canonical.normalize import NormalizationError, normalize_row


class RowOutcome(NamedTuple):
    facts: dict[str, list[dict[str, Any]]] | None
    #: Per record type, `canonical_json` of each fact in `facts`, in order.
    canonical: dict[str, list[str]] | None
    error: str | None
    txid: bytes | None
    #: Digest of the source row: tells a duplicate row from a conflicting variant.
    variant_hash: bytes | None
    field_mapping: dict[str, str] | None


def canonical_json(fact: dict[str, Any]) -> str:
    return json.dumps(fact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_one(row: ParsedRow, *, case_id: str, source_id: str, source_sha256: str) -> RowOutcome:
    try:
        normalized = normalize_row(case_id=case_id, source_id=source_id, source_sha256=source_sha256, row=row)
    except NormalizationError as exc:
        return RowOutcome(None, None, str(exc), None, None, None)
    txid = bytes.fromhex(normalized.facts["transactions"][0]["txid"])
    variant_hash = hashlib.blake2b(
        json.dumps(row.value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(),
        digest_size=16,
    ).digest()
    canonical = {record_type: [canonical_json(fact) for fact in facts] for record_type, facts in normalized.facts.items()}
    return RowOutcome(normalized.facts, canonical, None, txid, variant_hash, normalized.field_mapping or None)


def normalize_batch(job: dict[str, Any]) -> list[RowOutcome]:
    """Worker entry point: {"rows", "case_id", "source_id", "source_sha256"}."""
    ids = {key: job[key] for key in ("case_id", "source_id", "source_sha256")}
    return [normalize_one(row, **ids) for row in job["rows"]]
