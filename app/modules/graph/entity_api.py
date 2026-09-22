"""Gap-Closure WP-2 HTTP layer: entity reads and resolution-review decisions.

`GET /api/v1/entities/{id}` and `POST /api/v1/entities/{id}/resolution-review`
are not case-scoped in their URL (no `case_id` path parameter) -- the case
is looked up from the entity itself, then authorized exactly like every
other case-scoped route (`policy.authorize_case_action`, same default-deny
semantics `require_case_action` uses). `GET /api/v1/cases/{id}/entity-
candidates` is ordinarily case-scoped and reuses `require_graph_read`
directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.contracts.entity import EntityV1
from app.core.errors import get_request_id
from app.modules.access_control.audit import record_audit_event_safely
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    require_authenticated_user,
    require_graph_read,
)
from app.modules.access_control.models import (
    AuditOutcome,
    AuthenticatedPrincipal,
    AuthorizedCasePrincipal,
    CaseAction,
)
from app.modules.access_control.policy import authorize_case_action
from app.modules.access_control.repository import AccessControlRepository
from app.modules.graph.dependencies import get_entity_repository
from app.modules.graph.entity_models import (
    EntityResolutionCandidateListResponse,
    EntityReviewDecisionRecord,
    EntityReviewDecisionSubmission,
    EntityView,
    entity_resolution_review_view,
)
from app.modules.graph.entity_repository import EntityRepository
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.service import IntegrityService

router = APIRouter(prefix="/api/v1", tags=["entities"])
logger = structlog.get_logger(__name__)


async def _record_integrity_event_safely(
    integrity_service: IntegrityService, decision: EntityReviewDecisionRecord
) -> None:
    """Best-effort side effect, mirrors `review_service._record_integrity_event_safely`
    exactly: the decision has already durably committed by the time this
    runs, so a failure here is logged and left for
    `IntegrityReconciliationService` to repair -- never re-raised."""
    try:
        await integrity_service.record_integrity_event(decision.to_integrity_submission())
    except Exception as exc:  # noqa: BLE001 - a side-effect failure must never propagate
        logger.warning(
            "integrity.event_record_failed",
            case_id=str(decision.case_id),
            event_kind="entity_resolution_decision",
            subject_id=str(decision.entity_review_decision_id),
            retry_state="reconciliation_pending",
            failure_category=type(exc).__name__,
        )


async def _authorize_entity_action(
    entity_case_id: UUID,
    action: CaseAction,
    principal: AuthenticatedPrincipal,
    ac_repository: AccessControlRepository,
) -> None:
    """Same default-deny decision `require_case_action` makes, for an ID whose
    case isn't in the URL. Never reveals whether the denial was "entity
    doesn't exist", "wrong case", or "insufficient role/clearance"."""
    membership = await ac_repository.get_active_membership(entity_case_id, principal.user_id)
    case = await ac_repository.get_case(entity_case_id)
    allowed = authorize_case_action(
        case_id=entity_case_id,
        action=action,
        user_is_active=True,
        membership=membership,
        case=case,
    )
    if not allowed:
        await record_audit_event_safely(
            ac_repository,
            event_type="case_access_denied",
            outcome=AuditOutcome.DENIED,
            now=datetime.now(UTC),
            request_id=get_request_id() or None,
            user_id=principal.user_id,
            case_id=entity_case_id,
            metadata={"action": action.value},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access denied")


@router.get("/entities/{entity_id}", response_model=EntityView)
async def get_entity(
    entity_id: UUID,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    ac_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    entity_repository: Annotated[EntityRepository, Depends(get_entity_repository)],
) -> EntityView:
    entity = await _find_entity_by_id(entity_repository, entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
    await _authorize_entity_action(entity.case_id, CaseAction.GRAPH_READ, principal, ac_repository)
    return EntityView(entity=entity)


async def _find_entity_by_id(
    entity_repository: EntityRepository, entity_id: UUID
) -> EntityV1 | None:
    """Entity IDs are globally unique (deterministic from case_id+observation_id);
    a direct cross-case lookup exists only to answer "does this ID exist at
    all", never to bypass authorization -- the caller must still pass
    `_authorize_entity_action` before any entity content is returned."""
    return await entity_repository.get_entity_by_id_any_case(entity_id)


@router.get(
    "/cases/{case_id}/entity-candidates", response_model=EntityResolutionCandidateListResponse
)
async def list_entity_candidates(
    case_id: UUID,
    principal: Annotated[AuthorizedCasePrincipal, Depends(require_graph_read)],
    entity_repository: Annotated[EntityRepository, Depends(get_entity_repository)],
) -> EntityResolutionCandidateListResponse:
    candidates = await entity_repository.list_candidates(case_id)
    views = []
    for candidate in candidates:
        decisions = await entity_repository.list_decisions(
            case_id, candidate.entity_resolution_candidate_id
        )
        views.append(entity_resolution_review_view(candidate, tuple(decisions)))
    return EntityResolutionCandidateListResponse(items=tuple(views))


@router.post(
    "/entities/{entity_id}/resolution-review",
    response_model=EntityReviewDecisionRecord,
    status_code=status.HTTP_201_CREATED,
)
async def submit_entity_resolution_review(
    entity_id: UUID,
    candidate_id: UUID,
    body: EntityReviewDecisionSubmission,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_user)],
    ac_repository: Annotated[AccessControlRepository, Depends(get_access_control_repository)],
    entity_repository: Annotated[EntityRepository, Depends(get_entity_repository)],
    integrity_service: Annotated[IntegrityService, Depends(get_integrity_service)],
) -> EntityReviewDecisionRecord:
    """`candidate_id` (query param) names which resolution candidate this
    decision is about; `entity_id` in the path is used only to resolve the
    case for authorization -- it must be one of the candidate's own two
    entities, or the request is rejected before any decision is recorded.
    """
    entity = await _find_entity_by_id(entity_repository, entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entity not found")
    await _authorize_entity_action(
        entity.case_id, CaseAction.REVIEW_DECIDE, principal, ac_repository
    )

    candidate = await entity_repository.get_candidate(entity.case_id, candidate_id)
    if candidate is None or entity_id not in (candidate.left_entity_id, candidate.right_entity_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="resolution candidate not found"
        )

    decision = await entity_repository.record_decision(
        case_id=entity.case_id,
        entity_resolution_candidate_id=candidate.entity_resolution_candidate_id,
        decision=body.decision,
        reviewer_user_id=principal.user_id,
        rationale=body.rationale,
        decision_id=uuid4(),
    )
    await _record_integrity_event_safely(integrity_service, decision)
    return decision


__all__ = ["router"]
