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
from app.modules.graph.dependencies import get_graph_repository
from app.modules.graph.errors import GraphConnectionError, GraphValidationError
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
