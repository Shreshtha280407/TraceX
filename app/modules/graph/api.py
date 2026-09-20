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

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.modules.access_control.dependencies import (
    require_graph_read,
    require_hypothesis_propose,
    require_review_decision,
)
from app.modules.access_control.models import AuthorizedCasePrincipal
from app.modules.graph.dependencies import (
    get_candidate_review_repository,
    get_graph_correlation_integration_repository,
    get_graph_repository,
    get_hypothesis_repository,
    get_postgres_engine,
)
from app.modules.graph.errors import GraphConnectionError, GraphValidationError
from app.modules.graph.hypothesis_models import (
    HypothesisConflictError,
    HypothesisCreateSubmission,
    HypothesisListResponse,
    HypothesisRecord,
    HypothesisReviewSubmission,
    HypothesisValidationError,
)
from app.modules.graph.hypothesis_repository import HypothesisRepository
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
from app.modules.graph.review_models import (
    CandidateReviewDecisionSubmission,
    CandidateReviewListResponse,
    CandidateReviewView,
    ReviewConflictError,
    candidate_review_view,
)
from app.modules.graph.review_repository import CandidateReviewRepository
from app.modules.graph.review_service import (
    CandidateNotFoundError,
    create_hypothesis,
    submit_candidate_review_decision,
    submit_hypothesis_review_decision,
)
from app.modules.graph.schemas import (
    CaseGraphObservationsResponse,
    case_graph_observations_response,
)
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.service import IntegrityService

router = APIRouter(prefix="/api/v1/cases", tags=["graph"])
DEFAULT_INTEGRATION_LIMIT = 50
MAX_INTEGRATION_LIMIT = 200


def _integration_unavailable() -> HTTPException:
    """A fixed public failure for PostgreSQL-backed graph reads.

    Driver messages can reveal DSNs, table names, or the existence of a
    resource.  The error handler logs only the exception type for unexpected
    failures; routes intentionally return this fixed envelope for known
    database outages.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="graph data temporarily unavailable",
    )


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
    limit: Annotated[int, Query(ge=1, le=MAX_INTEGRATION_LIMIT)] = DEFAULT_INTEGRATION_LIMIT,
) -> CorrelationListResponse:
    """List durable reviewable propositions, never asserted relationships."""
    try:
        correlations = await repository.list_correlations(case_id, limit=limit)
        items = []
        for correlation in correlations:
            event = await repository.get_event_for_correlation(case_id, correlation.correlation_id)
            if event is not None:
                items.append(CorrelationIntegrationView(correlation=correlation, projection=event))
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
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
    try:
        correlation = await repository.get_correlation(case_id, correlation_id)
        event = await repository.get_event_for_correlation(case_id, correlation_id)
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
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
    limit: Annotated[int, Query(ge=1, le=MAX_INTEGRATION_LIMIT)] = DEFAULT_INTEGRATION_LIMIT,
) -> CandidateListResponse:
    """Return candidate links as candidates; no endpoint confirms or merges them."""
    try:
        return CandidateListResponse(
            items=tuple(await repository.list_candidates(case_id, limit=limit))
        )
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc


@router.get("/{case_id}/graph/hypotheses", response_model=HypothesisIntegrationListResponse)
async def list_hypothesis_integration_refs(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_INTEGRATION_LIMIT)] = DEFAULT_INTEGRATION_LIMIT,
) -> HypothesisIntegrationListResponse:
    """Expose supplied hypothesis references only; this module generates none."""
    try:
        items = []
        for correlation in await repository.list_correlations(case_id, limit=limit):
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
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
    return HypothesisIntegrationListResponse(items=tuple(items))


# --- Phase 6 Part 5: candidate review decisions ------------------------------
#
# Deliberately at bare `/candidates`, not `/graph/candidates` -- a distinct
# read/write surface from Nipun's read-only `/graph/candidates` above (which
# returns raw `CandidateLinkRecord`s and stays untouched). These routes
# additionally decorate each candidate with its effective review status and
# expose the one write action this phase adds: a human review decision.
# `review_service.py` holds all business logic; this stays a thin adapter.


async def _candidate_review_view_or_404(
    repository: GraphCorrelationIntegrationRepository,
    review_repository: CandidateReviewRepository,
    case_id: UUID,
    candidate_link_id: UUID,
) -> CandidateReviewView:
    candidate = await repository.get_candidate_link(case_id, candidate_link_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    decision = await review_repository.get_decision(case_id, candidate_link_id)
    return candidate_review_view(candidate, decision)


@router.get("/{case_id}/candidates", response_model=CandidateReviewListResponse)
async def list_candidates_for_review(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
    review_repository: Annotated[
        CandidateReviewRepository, Depends(get_candidate_review_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_INTEGRATION_LIMIT)] = DEFAULT_INTEGRATION_LIMIT,
) -> CandidateReviewListResponse:
    try:
        candidates = await repository.list_candidates(case_id, limit=limit)
        candidate_ids = tuple(candidate.candidate_link_id for candidate in candidates)
        decisions = {
            decision.candidate_link_id: decision
            for decision in await review_repository.list_decisions(
                case_id, candidate_link_ids=candidate_ids, limit=limit
            )
        }
        items = tuple(
            candidate_review_view(candidate, decisions.get(candidate.candidate_link_id))
            for candidate in candidates
        )
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
    return CandidateReviewListResponse(items=items)


@router.get("/{case_id}/candidates/{candidate_id}", response_model=CandidateReviewView)
async def get_candidate_for_review(
    case_id: UUID,
    candidate_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
    review_repository: Annotated[
        CandidateReviewRepository, Depends(get_candidate_review_repository)
    ],
) -> CandidateReviewView:
    try:
        return await _candidate_review_view_or_404(
            repository, review_repository, case_id, candidate_id
        )
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc


@router.post(
    "/{case_id}/candidates/{candidate_id}/review",
    response_model=CandidateReviewView,
)
async def review_candidate(
    case_id: UUID,
    candidate_id: UUID,
    submission: CandidateReviewDecisionSubmission,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_review_decision)],
    repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
    review_repository: Annotated[
        CandidateReviewRepository, Depends(get_candidate_review_repository)
    ],
    graph_repository: Annotated[Neo4jGraphRepository, Depends(get_graph_repository)],
    integrity_service: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> CandidateReviewView:
    """Record one authorized human review decision. Idempotent on exact retry;
    a conflicting retry never overwrites an existing decision (safe `409`)."""
    try:
        return await submit_candidate_review_decision(
            case_id=case_id,
            candidate_link_id=candidate_id,
            submission=submission,
            reviewer_user_id=principal.principal.user_id,
            integration_repository=repository,
            review_repository=review_repository,
            graph_repository=graph_repository,
            integrity_service=integrity_service,
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found"
        ) from exc
    except ReviewConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc


# --- Phase 6 Part 5: evidence-backed hypotheses ------------------------------
#
# Deliberately at bare `/hypotheses`, distinct from Nipun's read-only
# `/graph/hypotheses` above (an upstream `correlation.hypothesis_reference`
# passthrough this module never writes to). A hypothesis here is always
# human-created and explicitly reviewable -- see `hypothesis_models.py`'s
# module docstring.


@router.get("/{case_id}/hypotheses", response_model=HypothesisListResponse)
async def list_hypotheses(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[HypothesisRepository, Depends(get_hypothesis_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_INTEGRATION_LIMIT)] = DEFAULT_INTEGRATION_LIMIT,
) -> HypothesisListResponse:
    try:
        return HypothesisListResponse(
            items=tuple(await repository.list_hypotheses(case_id, limit=limit))
        )
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc


@router.get("/{case_id}/hypotheses/{hypothesis_id}", response_model=HypothesisRecord)
async def get_hypothesis(
    case_id: UUID,
    hypothesis_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    repository: Annotated[HypothesisRepository, Depends(get_hypothesis_repository)],
) -> HypothesisRecord:
    try:
        hypothesis = await repository.get_hypothesis(case_id, hypothesis_id)
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
    if hypothesis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="hypothesis not found")
    return hypothesis


@router.post("/{case_id}/hypotheses", response_model=HypothesisRecord)
async def propose_hypothesis(
    case_id: UUID,
    submission: HypothesisCreateSubmission,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_hypothesis_propose)],
    repository: Annotated[HypothesisRepository, Depends(get_hypothesis_repository)],
    integration_repository: Annotated[
        GraphCorrelationIntegrationRepository,
        Depends(get_graph_correlation_integration_repository),
    ],
    postgres_engine: Annotated[AsyncEngine, Depends(get_postgres_engine)],
    graph_repository: Annotated[Neo4jGraphRepository, Depends(get_graph_repository)],
    integrity_service: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> HypothesisRecord:
    """Create one human-authored, evidence-backed hypothesis.

    Never a fact or guilt conclusion: it starts (and can only ever be
    created at) `needs_review`. Every cited observation/candidate is
    re-verified against this case before the hypothesis is durably written.
    """
    try:
        return await create_hypothesis(
            case_id=case_id,
            submission=submission,
            created_by=principal.principal.user_id,
            hypothesis_repository=repository,
            integration_repository=integration_repository,
            postgres_engine=postgres_engine,
            graph_repository=graph_repository,
            integrity_service=integrity_service,
        )
    except HypothesisValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc


@router.post("/{case_id}/hypotheses/{hypothesis_id}/review", response_model=HypothesisRecord)
async def review_hypothesis(
    case_id: UUID,
    hypothesis_id: UUID,
    submission: HypothesisReviewSubmission,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_review_decision)],
    repository: Annotated[HypothesisRepository, Depends(get_hypothesis_repository)],
    graph_repository: Annotated[Neo4jGraphRepository, Depends(get_graph_repository)],
    integrity_service: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> HypothesisRecord:
    """Record one authorized human review decision on a hypothesis. Idempotent
    on exact retry; a conflicting retry never overwrites an existing decision."""
    try:
        result = await submit_hypothesis_review_decision(
            case_id=case_id,
            hypothesis_id=hypothesis_id,
            submission=submission,
            reviewer_user_id=principal.principal.user_id,
            hypothesis_repository=repository,
            graph_repository=graph_repository,
            integrity_service=integrity_service,
        )
    except HypothesisConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except sa.exc.SQLAlchemyError as exc:
        raise _integration_unavailable() from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="hypothesis not found")
    return result
