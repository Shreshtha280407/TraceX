"""Gap-Closure WP-6 (G8): `AuditEventType` catalog drift protection.

Statically greps every `event_type="..."` literal actually used in
`app/` and asserts it is a member of `AuditEventType` -- and that every
catalog member is actually used somewhere. This is the catalog's real
enforcement: it fails immediately if a new call site introduces an
uncatalogued (or typo'd) event type, or if a catalog entry goes dead.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.modules.access_control.audit_catalog import AuditEventType

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "app"

#: `EventV1.event_type` domain values (see `app/modules/graph/mapping.py`)
#: are a different concept entirely -- a real-world event kind projected
#: into the graph, not a security-audit event. Excluded here, and
#: explicitly documented as excluded in `audit_catalog.py`'s own docstring.
_DOMAIN_EVENT_TYPES = frozenset(
    {
        "cdr_call",
        "financial_transaction",
        "meeting_candidate",
        "message",
        "sighting",
        "speech_segment",
    }
)

_EVENT_TYPE_LITERAL = re.compile(r'event_type="([a-zA-Z0-9_.]+)"')


def _event_type_literals_used_in_app() -> set[str]:
    found: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found.update(_EVENT_TYPE_LITERAL.findall(text))
    return found - _DOMAIN_EVENT_TYPES


def test_every_event_type_literal_used_in_app_is_catalogued() -> None:
    used = _event_type_literals_used_in_app()
    catalogued = {member.value for member in AuditEventType}
    uncatalogued = used - catalogued
    assert not uncatalogued, (
        f"event_type literal(s) used in app/ but missing from AuditEventType: {uncatalogued}"
    )


def test_every_catalogued_event_type_is_actually_used() -> None:
    used = _event_type_literals_used_in_app()
    catalogued = {member.value for member in AuditEventType}
    dead = catalogued - used
    assert not dead, f"AuditEventType member(s) with no real call site: {dead}"


def test_catalog_has_no_duplicate_values() -> None:
    values = [member.value for member in AuditEventType]
    assert len(values) == len(set(values))
