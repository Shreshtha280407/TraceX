"""FastAPI dependency providers for the graph module's read endpoint.

Mirrors `app.modules.evidence_lifecycle.dependencies`'s pattern: the Neo4j
driver is constructed once at import time and is lazy -- no real connection
opens until first used, so this cannot fail import even if Neo4j isn't
reachable yet (`/readyz` is the designated readiness signal, unaffected by
this module).
"""

from __future__ import annotations

from app.core.config import get_settings
from app.modules.graph.repository import Neo4jGraphRepository, create_driver

_settings = get_settings()
_driver = create_driver(_settings)
_repository = Neo4jGraphRepository(_driver)


def get_graph_repository() -> Neo4jGraphRepository:
    return _repository
