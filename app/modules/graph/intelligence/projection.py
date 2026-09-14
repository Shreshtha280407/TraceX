"""Semantic Neo4j handler for Nipun's ``correlation.upserted.v1`` seam."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.modules.graph.integration_models import CorrelationProjectionContext
from app.modules.graph.repository import Neo4jGraphRepository

CorrelationProjectionHandler = Callable[[CorrelationProjectionContext], Awaitable[None]]


def make_correlation_projection_handler(
    repository: Neo4jGraphRepository,
) -> CorrelationProjectionHandler:
    """Adapt the semantic handler to Nipun's replay callback signature."""

    async def handler(context: CorrelationProjectionContext) -> None:
        await project_correlation_context(repository, context)

    return handler


async def project_correlation_context(
    repository: Neo4jGraphRepository, context: CorrelationProjectionContext
) -> None:
    """MERGE a review-only correlation using Nipun's replay-safe projection key.

    The query starts at existing case-scoped Observation nodes, so a damaged
    context creates no correlation node or candidate edge.
    """
    query = (
        "UNWIND $evidence_paths AS path "
        "MATCH (e:Evidence {case_id: $case_id, evidence_id: path.evidence_id}) "
        "-[:YIELDED_OBSERVATION]->(o:Observation {case_id: $case_id, "
        "observation_id: path.observation_id}) "
        "WITH collect(o) AS observations, count(o) AS found "
        "WHERE found = size($evidence_paths) "
        "MERGE (c:Correlation {case_id: $case_id, projection_key: $projection_key}) "
        "SET c += $properties "
        "WITH c, observations "
        "UNWIND observations AS o "
        "MERGE (c)-[:SUPPORTED_BY_OBSERVATION]->(o) "
        "RETURN c.projection_key AS projection_key"
    )
    correlation = context.correlation
    await repository.write(
        query,
        {
            "case_id": str(context.event.case_id),
            "projection_key": context.event.projection_key,
            "evidence_paths": [
                {
                    "evidence_id": str(path.evidence_id),
                    "observation_id": str(path.observation_id),
                }
                for path in correlation.evidence_paths
            ],
            "properties": {
                "case_id": str(context.event.case_id),
                "projection_key": context.event.projection_key,
                "correlation_id": str(correlation.correlation_id),
                "correlation_type": correlation.correlation_type,
                "status": correlation.status.value,
                "mapping_version": correlation.mapping_version,
                "config_version": correlation.config_version,
                "candidate_count": len(context.candidates),
                "candidate_only": True,
            },
        },
    )
