"""Deterministic ID generation.

TraceX needs IDs that are stable across re-ingestion of the same source
material (so re-processing evidence does not create duplicate graph nodes)
while still being different whenever the meaningful input actually changes.
`uuid5` over a fixed namespace gives us exactly that: same namespace + same
normalized parts -> same UUID, always.
"""

from __future__ import annotations

import uuid

# Fixed, never-changing namespace for all TraceX deterministic IDs.
# Generated once via uuid.uuid4() and frozen; do not regenerate.
TRACEX_NAMESPACE = uuid.UUID("f47a1e2c-6b3d-4a8e-9c1f-2d5b8e0a7c34")


def deterministic_uuid(*parts: str, namespace: uuid.UUID = TRACEX_NAMESPACE) -> uuid.UUID:
    """Derive a stable UUID from normalized string parts.

    Parts are joined with a unit separator that is exceedingly unlikely to
    appear in real input, so that `("ab", "c")` and `("a", "bc")` never
    collide. Callers are responsible for normalizing each part (case,
    whitespace, encoding) before calling this function, since normalization
    is domain-specific.
    """
    if not parts:
        raise ValueError("deterministic_uuid requires at least one input part")
    joined = "\x1f".join(parts)
    return uuid.uuid5(namespace, joined)
