"""Graph HTTP layer: the case-scoped, read-only projected-graph endpoint.

Thin, like `app.modules.evidence_lifecycle.api`: parse the request, call
`queries.py`, translate typed errors into safe, generic HTTP responses. No
projection/business logic lives here.

Authorization reuses `app.modules.access_control.dependencies.require_graph_read`
(`CaseAction.GRAPH_READ`) exactly as that module's own role/action matrix
already provisions for this use case -- a caller with no active membership
on `case_id`, or a role that never had `GRAPH_READ` (e.g. none currently
lack it among roles that also have `CASE_READ`), gets the same generic
`403` every other case-scoped endpoint returns, including the
`case_access_denied` audit event `require_case_action` already records.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.modules.access_control.dependencies import require_graph_read
from app.modules.access_control.models import AuthorizedCasePrincipal
from app.modules.graph.dependencies import (
    get_graph_correlation_integration_repository,
    get_graph_repository,
)
from app.modules.graph.errors import GraphConnectionError, GraphValidationError
from app.modules.graph.integration_models import (
    CandidateListResponse,
    CorrelationIntegrationView,
    CorrelationListResponse,
    HypothesisIntegrationListResponse,
    HypothesisIntegrationView,
)
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.queries import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, list_case_observations
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.schemas import (
    CaseGraphObservationsResponse,
    case_graph_observations_response,
)

router = APIRouter(prefix="/api/v1/cases", tags=["graph"])


@router.get(
    "/{case_id}/graph/observations",
    response_model=CaseGraphObservationsResponse,
)
async def list_graph_observations(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[Neo4jGraphRepository, Depends(get_graph_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseGraphObservationsResponse:
    """The safe, case-scoped projected observation graph: observations plus their mentions.

    Never a Cypher query, an object URI, raw evidence body, worker
    credential, or Neo4j implementation detail. `limit`/`offset` are
    bounds-checked by FastAPI itself before this function ever runs;
    `queries.list_case_observations` re-validates them independently
    (defense in depth, matching every other graph read query).
    """
    try:
        page = await list_case_observations(repository, case_id, limit=limit, offset=offset)
    except GraphValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except GraphConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="graph service temporarily unavailable",
        ) from exc
    return case_graph_observations_response(page)


@router.get("/{case_id}/graph/correlations", response_model=CorrelationListResponse)
async def list_graph_correlations(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
) -> CorrelationListResponse:
    """List durable reviewable propositions, never asserted relationships."""
    correlations = await repository.list_correlations(case_id)
    items = []
    for correlation in correlations:
        event = await repository.get_event_for_correlation(case_id, correlation.correlation_id)
        if event is not None:
            items.append(CorrelationIntegrationView(correlation=correlation, projection=event))
    return CorrelationListResponse(items=tuple(items))


@router.get(
    "/{case_id}/graph/correlations/{correlation_id}", response_model=CorrelationIntegrationView
)
async def get_graph_correlation(
    case_id: UUID,
    correlation_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
) -> CorrelationIntegrationView:
    correlation = await repository.get_correlation(case_id, correlation_id)
    event = await repository.get_event_for_correlation(case_id, correlation_id)
    if correlation is None or event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="correlation not found")
    return CorrelationIntegrationView(correlation=correlation, projection=event)


@router.get("/{case_id}/graph/candidates", response_model=CandidateListResponse)
async def list_graph_candidates(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
) -> CandidateListResponse:
    """Return candidate links as candidates; no endpoint confirms or merges them."""
    return CandidateListResponse(items=tuple(await repository.list_candidates(case_id)))


@router.get("/{case_id}/graph/hypotheses", response_model=HypothesisIntegrationListResponse)
async def list_hypothesis_integration_refs(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
) -> HypothesisIntegrationListResponse:
    """Expose supplied hypothesis references only; this module generates none."""
    items = []
    for correlation in await repository.list_correlations(case_id):
        if correlation.hypothesis_reference is None:
            continue
        event = await repository.get_event_for_correlation(case_id, correlation.correlation_id)
        if event is not None:
            items.append(
                HypothesisIntegrationView(
                    correlation_id=correlation.correlation_id,
                    hypothesis_reference=correlation.hypothesis_reference,
                    status=correlation.status,
                    projection=event,
                )
            )
    return HypothesisIntegrationListResponse(items=tuple(items))
