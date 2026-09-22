"""FastAPI dependency providers for the graph module's read endpoint.

Mirrors `app.modules.evidence_lifecycle.dependencies`'s pattern: the Neo4j
driver is constructed once at import time and is lazy -- no real connection
opens until first used, so this cannot fail import even if Neo4j isn't
reachable yet (`/readyz` is the designated readiness signal, unaffected by
this module).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.modules.graph.entity_repository import EntityRepository
from app.modules.graph.entity_repository import create_engine as create_entity_engine
from app.modules.graph.hypothesis_repository import HypothesisRepository
from app.modules.graph.hypothesis_repository import create_engine as create_hypothesis_engine
from app.modules.graph.integration_repository import (
    GraphCorrelationIntegrationRepository,
)
from app.modules.graph.integration_repository import (
    create_engine as create_integration_engine,
)
from app.modules.graph.repository import Neo4jGraphRepository, create_driver
from app.modules.graph.review_projection_outbox import ReviewProjectionOutboxRepository
from app.modules.graph.review_projection_outbox import (
    create_engine as create_review_projection_outbox_engine,
)
from app.modules.graph.review_repository import CandidateReviewRepository
from app.modules.graph.review_repository import create_engine as create_review_engine

_settings = get_settings()
_driver = create_driver(_settings)
_repository = Neo4jGraphRepository(_driver)
_integration_repository = GraphCorrelationIntegrationRepository(
    create_integration_engine(_settings)
)
_review_repository = CandidateReviewRepository(create_review_engine(_settings))
_hypothesis_repository = HypothesisRepository(create_hypothesis_engine(_settings))
_entity_repository = EntityRepository(create_entity_engine(_settings))
_review_projection_outbox_repository = ReviewProjectionOutboxRepository(
    create_review_projection_outbox_engine(_settings)
)
_postgres_engine = create_integration_engine(_settings)


def get_graph_repository() -> Neo4jGraphRepository:
    return _repository


def get_graph_correlation_integration_repository() -> GraphCorrelationIntegrationRepository:
    """The PostgreSQL-backed Phase 5 read/integration seam."""
    return _integration_repository


def get_candidate_review_repository() -> CandidateReviewRepository:
    return _review_repository


def get_hypothesis_repository() -> HypothesisRepository:
    return _hypothesis_repository


def get_entity_repository() -> EntityRepository:
    return _entity_repository


def get_review_projection_outbox_repository() -> ReviewProjectionOutboxRepository:
    return _review_projection_outbox_repository


def get_postgres_engine() -> AsyncEngine:
    """A plain engine against the same Postgres DSN, for canonical read-backs
    (e.g. resolving `evidence_id` per observation before a Neo4j write) that
    don't belong to any one repository's own table set."""
    return _postgres_engine
