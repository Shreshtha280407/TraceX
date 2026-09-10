"""Safe security-audit-event recording.

Every field this module writes is safe security telemetry only -- never a
password, token, secret, evidence/CDR/financial value, full IP address, or
stack trace. See `docs/architecture/security-boundaries-v1.md`. This is
security telemetry, not a substitute for Nipun's later tamper-evident
evidence audit chain.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import JsonValue

from app.modules.access_control.models import AuditOutcome, SecurityAuditEventRecord
from app.modules.access_control.repository import AccessControlRepository

#: Truncated so this is unambiguously a marker, not a reconstructable full hash.
_IP_MARKER_LENGTH = 16


def hash_ip(raw_ip: str) -> str:
    """A short, non-reversible marker for a client IP. Never the raw address."""
    return hashlib.sha256(raw_ip.encode()).hexdigest()[:_IP_MARKER_LENGTH]


async def record_audit_event(
    repository: AccessControlRepository,
    *,
    event_type: str,
    outcome: AuditOutcome,
    now: datetime,
    request_id: str | None = None,
    user_id: UUID | None = None,
    case_id: UUID | None = None,
    ip_marker: str | None = None,
    metadata: dict[str, JsonValue] | None = None,
) -> None:
    """Persist one safe security-audit row.

    `metadata` must only ever carry safe, structured facts (e.g. `{"reason":
    "invalid_password"}`) -- never a secret, token, password hash, or raw
    evidence/CDR/financial value. Callers are responsible for that; this
    function does not (and cannot generically) scan for one.
    """
    event = SecurityAuditEventRecord(
        event_id=uuid4(),
        occurred_at=now,
        event_type=event_type,
        outcome=outcome,
        request_id=request_id,
        user_id_nullable=user_id,
        case_id_nullable=case_id,
        ip_hash_or_safe_network_marker=ip_marker,
        metadata_safe_json=metadata or {},
    )
    await repository.record_audit_event(event)
