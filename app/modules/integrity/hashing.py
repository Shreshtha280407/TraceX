"""Deterministic, domain-separated SHA-256 Merkle tree over integrity-event leaves.

Frozen rules (see `docs/architecture/phase-6-integrity.md` and
`docs/decisions/ADR-013-phase-6-integrity-checkpoints.md`):

- Hash algorithm: SHA-256.
- Leaf ordering: `(case_id, sequence_number)` ascending -- never database
  return order or wall-clock order alone.
- Leaf hash input: domain-separated canonical bytes of the event's safe,
  already-persisted columns (never raw source content -- it was never
  stored in the first place, see `models.py`).
- Parent hash input: domain-separated `left_hash || right_hash` (raw bytes,
  not hex text).
- Odd node at a level: duplicate the final hash. Never zero-pad.

Changing any of these rules changes every previously computed root and must
be a new `MERKLE_TREE_FORMAT_VERSION`, not a silent edit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.core.canonical import canonical_bytes
from app.modules.integrity.models import IntegrityEventRecord

LEAF_DOMAIN_PREFIX = b"tracex.integrity.leaf.v1\x1f"
NODE_DOMAIN_PREFIX = b"tracex.integrity.node.v1\x1f"


def leaf_hash(event: IntegrityEventRecord) -> str:
    """The Merkle leaf hash for one integrity event, as lowercase hex."""
    payload = {
        "integrity_event_id": str(event.integrity_event_id),
        "case_id": str(event.case_id),
        "event_kind": event.event_kind.value,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "canonical_payload_sha256": event.canonical_payload_sha256,
        "payload_schema_version": event.payload_schema_version,
        "sequence_number": event.sequence_number,
    }
    return hashlib.sha256(LEAF_DOMAIN_PREFIX + canonical_bytes(payload)).hexdigest()


def parent_hash(left_hex: str, right_hex: str) -> str:
    digest = hashlib.sha256(NODE_DOMAIN_PREFIX + bytes.fromhex(left_hex) + bytes.fromhex(right_hex))
    return digest.hexdigest()


def build_merkle_root(leaf_hashes: Sequence[str]) -> str:
    """Fold an ordered sequence of leaf hashes up to a single root hash.

    `leaf_hashes` must already be ordered by `(case_id, sequence_number)`;
    this function does not sort -- reordering the input changes the root,
    which is the point (see proof point 12, swapped leaf order detected).
    """
    if not leaf_hashes:
        raise ValueError("cannot build a checkpoint over zero leaves")
    level = list(leaf_hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [parent_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]
