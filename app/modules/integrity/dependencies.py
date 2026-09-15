"""FastAPI dependency providers for the integrity module.

Mirrors `app.modules.evidence_lifecycle.dependencies`'s pattern: the
PostgreSQL engine is constructed once at import time and is lazy, so this
cannot fail import even if postgres isn't reachable yet.

No router is registered here in Phase 6 Part 1 -- see
`docs/architecture/phase-6-integrity.md`'s "Verification and safe export"
section for why protected HTTP exposure is left to Aditya. These providers
exist so other in-process modules (evidence lifecycle, graph) and the
verification CLI can obtain a ready `IntegrityService` the same way every
other module obtains its dependencies.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.modules.integrity.repository import IntegrityRepository, create_engine
from app.modules.integrity.service import IntegrityService

_settings = get_settings()
_engine = create_engine(_settings)
_repository = IntegrityRepository(_engine)


def get_integrity_repository() -> IntegrityRepository:
    return _repository


def get_integrity_service() -> IntegrityService:
    return IntegrityService(_repository, _settings)
