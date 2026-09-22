"""Phase 6 Part 5: live end-to-end candidate-review and hypothesis workflow.

Covers, against the real running API server, live PostgreSQL, and live
Neo4j: authorization ordering (401/403/404 before any candidate/hypothesis
lookup, no cross-case existence leakage), idempotent retry, conflicting
retry rejection, append-only PostgreSQL enforcement for the two new
immutable tables, provenance-gated Neo4j projection, and reconciliation
replay for `review_decision`/`hypothesis_action` integrity events.

Self-skips (never fabricates a pass) whenever there's no `.env`, the live
API server isn't reachable, or PostgreSQL/Neo4j specifically aren't
reachable through it -- same pattern as
`test_phase5_final_acceptance_live.py`. Only synthetic, non-sensitive
fixture data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from app.contracts.observation import ExtractedEntityMention
from app.core.config import Settings
from app.dependencies.services import check_neo4j, check_postgres
from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    CaseRole,
    CaseStatus,
    ClearanceLevel,
)
from app.modules.access_control.repository import AccessControlRepository
from app.modules.graph.hypothesis_repository import hypothesis_actions_table
from app.modules.graph.integration_models import CandidateLinkSubmission, CorrelationSubmission
from app.modules.graph.integration_projector import replay_graph_updates
from app.modules.graph.integration_repository import GraphCorrelationIntegrationRepository
from app.modules.graph.intelligence.projection import make_correlation_projection_handler
from app.modules.graph.outbox_repository import GraphProjectionOutboxRepository
from app.modules.graph.outbox_repository import create_engine as create_pg_engine
from app.modules.graph.repository import Neo4jGraphRepository, create_driver
from app.modules.graph.review_repository import (
    CandidateReviewRepository,
    candidate_review_decisions_table,
)
from app.modules.integrity.reconciliation import IntegrityReconciliationService
from app.modules.integrity.repository import IntegrityRepository
from app.modules.integrity.service import IntegrityService
from app.modules.integrity.signing import generate_signing_key_b64
from tests.fixtures.access_control.factories import make_user_record
from tests.integration.graph.test_phase5_correlation_integration_live import (
    _cleanup,
    _insert_observation,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"
_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_SHARED_PHONE = "9876543210"

pytestmark = pytest.mark.skipif(
    not ENV_FILE.exists(),
    reason="no .env at repo root; copy .env.example and start infra to run this suite",
)


def _live_settings(**overrides: Any) -> Settings:
    values = dotenv_values(ENV_FILE)
    kwargs: dict[str, Any] = {k.lower(): v for k, v in values.items() if v is not None}
    kwargs.update(overrides)
    kwargs.setdefault("integrity_signing_key", generate_signing_key_b64())
    return Settings(_env_file=None, **kwargs)


def _skip_unless_api_reachable(settings: Settings) -> None:
    try:
        httpx.get(f"{settings.worker_api_base_url}/healthz", timeout=2.0).raise_for_status()
    except Exception as exc:
        pytest.skip(
            f"live API server not reachable at {settings.worker_api_base_url}: {type(exc).__name__}"
        )


async def _project_evidence_and_observation(
    repository: Neo4jGraphRepository, *, case_id: UUID, evidence_id: UUID, observation_id: UUID
) -> None:
    await repository.write(
        "MERGE (e:Evidence {case_id: $case_id, evidence_id: $evidence_id}) "
        "MERGE (o:Observation {case_id: $case_id, observation_id: $observation_id}) "
        "MERGE (e)-[:YIELDED_OBSERVATION]->(o)",
        {
            "case_id": str(case_id),
            "evidence_id": str(evidence_id),
            "observation_id": str(observation_id),
        },
    )


async def test_review_and_hypothesis_workflow_against_live_stack() -> None:  # noqa: PLR0915
    settings = _live_settings()
    _skip_unless_api_reachable(settings)
    try:
        await check_postgres(settings)
        await check_neo4j(settings)
    except Exception as exc:
        pytest.skip(f"live PostgreSQL/Neo4j not reachable: {type(exc).__name__}")

    outbox = GraphProjectionOutboxRepository(create_pg_engine(settings))
    engine = outbox._engine  # noqa: SLF001 - shared DB seam, same pattern as sibling live tests
    ac_repository = AccessControlRepository(engine)
    correlation_repository = GraphCorrelationIntegrationRepository(engine)
    review_repository = CandidateReviewRepository(engine)
    integrity_repository = IntegrityRepository(engine)
    integrity_service = IntegrityService(integrity_repository, settings)
    reconciliation = IntegrityReconciliationService(integrity_service, integrity_repository)
    graph_repository = Neo4jGraphRepository(create_driver(settings))

    case_id = uuid4()
    other_case_id = uuid4()
    investigator_email = f"phase6-investigator-{uuid4().hex[:8]}@example.test"
    reviewer_email = f"phase6-reviewer-{uuid4().hex[:8]}@example.test"
    outsider_email = f"phase6-outsider-{uuid4().hex[:8]}@example.test"
    password = "correct-horse-battery-staple"  # noqa: S105

    try:
        # No public self-registration exists (G5) -- seed users directly
        # against the same live Postgres `ac_repository` already writes to.
        for email in (investigator_email, reviewer_email, outsider_email):
            await ac_repository.create_user(make_user_record(email_normalized=email))

        async with httpx.AsyncClient(base_url=settings.worker_api_base_url, timeout=30.0) as ac:

            async def _login(email: str) -> dict[str, str]:
                response = await ac.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert response.status_code == 200, response.text
                return {"Authorization": f"Bearer {response.json()['access_token']}"}

            investigator_headers = await _login(investigator_email)
            reviewer_headers = await _login(reviewer_email)
            outsider_headers = await _login(outsider_email)

            async def _user_id(headers: dict[str, str]) -> UUID:
                me = await ac.get("/api/v1/auth/me", headers=headers)
                return UUID(me.json()["user"]["user_id"])

            investigator_id = await _user_id(investigator_headers)
            reviewer_id = await _user_id(reviewer_headers)

            await ac_repository.create_case(
                CaseRecord(
                    case_id=case_id,
                    case_reference=f"LIVE-PHASE6-REVIEW-{uuid4().hex[:8]}",
                    classification=ClearanceLevel.RESTRICTED,
                    status=CaseStatus.OPEN,
                    created_at=_NOW,
                )
            )
            for user_id, role in (
                (investigator_id, CaseRole.INVESTIGATOR),
                (reviewer_id, CaseRole.REVIEWER),
            ):
                await ac_repository.create_membership(
                    CaseMembershipRecord(
                        membership_id=uuid4(),
                        case_id=case_id,
                        user_id=user_id,
                        role=role,
                        clearance=ClearanceLevel.RESTRICTED,
                        is_active=True,
                        created_at=_NOW,
                        updated_at=_NOW,
                    )
                )
            # `outsider` deliberately never gets a membership row.

            # --- unauthenticated access is rejected before any lookup ---
            unauth = await ac.get(f"/api/v1/cases/{case_id}/candidates")
            assert unauth.status_code == 401

            # --- build two real, matching observations and one real candidate ---
            entity = [ExtractedEntityMention(text=_SHARED_PHONE, entity_type_hint="phone_number")]
            first = await _insert_observation(
                outbox,
                case_id=case_id,
                evidence_id=uuid4(),
                observation_type="phone_number_mention",
                extracted_entities=entity,
                attributes={},
            )
            second = await _insert_observation(
                outbox,
                case_id=case_id,
                evidence_id=uuid4(),
                observation_type="phone_number_mention",
                extracted_entities=entity,
                attributes={},
            )
            submission = CorrelationSubmission(
                idempotency_key=f"phase6-review-live.{case_id}",
                correlation_type="rules_candidate_correlation",
                supporting_observation_ids=(first.observation_id, second.observation_id),
                candidate_links=(
                    CandidateLinkSubmission(
                        idempotency_key=f"phase6-review-live.link.{case_id}",
                        left_observation_id=first.observation_id,
                        right_observation_id=second.observation_id,
                        reason_reference="exact_identifier",
                    ),
                ),
                mapping_version="phase5.v1",
                config_version="phase5.config.v1",
            )
            receipt = await correlation_repository.submit(case_id=case_id, submission=submission)
            candidates = await correlation_repository.list_candidates(case_id)
            assert len(candidates) == 1
            candidate_link_id = candidates[0].candidate_link_id

            # Project the real Evidence->Observation chain, then the Correlation
            # node itself, through Nipun's real replay seam -- exactly the
            # provenance a review decision's Neo4j write is gated on.
            for observation in (first, second):
                await _project_evidence_and_observation(
                    graph_repository,
                    case_id=case_id,
                    evidence_id=observation.evidence_id,
                    observation_id=observation.observation_id,
                )
            handler = make_correlation_projection_handler(graph_repository)
            await replay_graph_updates(correlation_repository, handler)

            # --- cross-case / non-member access is denied before any lookup ---
            forbidden = await ac.get(
                f"/api/v1/cases/{other_case_id}/candidates", headers=investigator_headers
            )
            assert forbidden.status_code == 403
            outsider_forbidden = await ac.get(
                f"/api/v1/cases/{case_id}/candidates", headers=outsider_headers
            )
            assert outsider_forbidden.status_code == 403

            # --- a nonexistent candidate is a safe 404, not a leak ---
            missing = await ac.get(
                f"/api/v1/cases/{case_id}/candidates/{uuid4()}", headers=reviewer_headers
            )
            assert missing.status_code == 404

            # --- the candidate starts needs_review ---
            listing = await ac.get(f"/api/v1/cases/{case_id}/candidates", headers=reviewer_headers)
            assert listing.status_code == 200
            assert listing.json()["items"][0]["review_status"] == "needs_review"

            # --- an investigator (no REVIEW_DECIDE) cannot review it ---
            investigator_denied = await ac.post(
                f"/api/v1/cases/{case_id}/candidates/{candidate_link_id}/review",
                headers=investigator_headers,
                json={"decision": "accepted_by_reviewer", "rationale": "matches on shared phone"},
            )
            assert investigator_denied.status_code == 403

            # --- the reviewer accepts it ---
            review = await ac.post(
                f"/api/v1/cases/{case_id}/candidates/{candidate_link_id}/review",
                headers=reviewer_headers,
                json={"decision": "accepted_by_reviewer", "rationale": "matches on shared phone"},
            )
            assert review.status_code == 200, review.text
            assert review.json()["review_status"] == "accepted_by_reviewer"
            # The raw rationale IS returned on this protected, case-scoped read
            # surface (see `review_models.py`'s module docstring) -- only the
            # integrity event/Neo4j projection/logs are commitment-only.
            assert review.json()["decision"]["rationale"] == "matches on shared phone"

            # --- exact retry is idempotent ---
            replay = await ac.post(
                f"/api/v1/cases/{case_id}/candidates/{candidate_link_id}/review",
                headers=reviewer_headers,
                json={"decision": "accepted_by_reviewer", "rationale": "matches on shared phone"},
            )
            assert replay.status_code == 200
            assert (
                replay.json()["decision"]["candidate_review_decision_id"]
                == review.json()["decision"]["candidate_review_decision_id"]
            )

            # --- a conflicting retry never overwrites the existing decision ---
            conflicting = await ac.post(
                f"/api/v1/cases/{case_id}/candidates/{candidate_link_id}/review",
                headers=reviewer_headers,
                json={"decision": "rejected_by_reviewer"},
            )
            assert conflicting.status_code == 409
            async with engine.connect() as conn:
                decision_count = (
                    await conn.execute(
                        sa.text(
                            "SELECT count(*) FROM candidate_review_decisions WHERE case_id = :c"
                        ),
                        {"c": case_id},
                    )
                ).scalar_one()
            assert decision_count == 1

            # --- the review_decision integrity event and Neo4j projection landed ---
            decision_record = await review_repository.get_decision(case_id, candidate_link_id)
            assert decision_record is not None
            integrity_event = await integrity_repository.get_event_by_idempotency_key(
                case_id, decision_record.idempotency_key
            )
            assert integrity_event is not None
            assert integrity_event.event_kind.value == "review_decision"
            correlation_node = await graph_repository.read(
                "MATCH (c:Correlation {case_id: $case_id, correlation_id: $correlation_id}) "
                "RETURN c.review_status AS review_status",
                {
                    "case_id": str(case_id),
                    "correlation_id": str(receipt.correlation.correlation_id),
                },
            )
            assert correlation_node[0]["review_status"] == "accepted_by_reviewer"

            # --- append-only: a direct UPDATE/DELETE is rejected by PostgreSQL ---
            async with engine.connect() as conn:
                with pytest.raises(sa.exc.DBAPIError):
                    async with conn.begin():
                        await conn.execute(
                            candidate_review_decisions_table.update()
                            .where(
                                candidate_review_decisions_table.c.candidate_review_decision_id
                                == decision_record.candidate_review_decision_id
                            )
                            .values(decision="rejected_by_reviewer")
                        )
            async with engine.connect() as conn:
                with pytest.raises(sa.exc.DBAPIError):
                    async with conn.begin():
                        await conn.execute(
                            candidate_review_decisions_table.delete().where(
                                candidate_review_decisions_table.c.candidate_review_decision_id
                                == decision_record.candidate_review_decision_id
                            )
                        )

            # --- hypothesis workflow: create, review, idempotency, conflict ---
            hypothesis_create = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses",
                headers=investigator_headers,
                json={
                    "statement": "the two accounts may belong to a shared operator",
                    "rationale": "both observations share a normalized phone identifier",
                    "supporting_observation_ids": [str(first.observation_id)],
                    "supporting_candidate_ids": [str(candidate_link_id)],
                },
            )
            assert hypothesis_create.status_code == 200, hypothesis_create.text
            hypothesis_id = hypothesis_create.json()["hypothesis_id"]
            assert hypothesis_create.json()["status"] == "needs_review"

            invalid_reference = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses",
                headers=investigator_headers,
                json={
                    "statement": "cites a fabricated observation",
                    "supporting_observation_ids": [str(uuid4())],
                },
            )
            assert invalid_reference.status_code == 422

            missing_hypothesis = await ac.get(
                f"/api/v1/cases/{case_id}/hypotheses/{uuid4()}", headers=reviewer_headers
            )
            assert missing_hypothesis.status_code == 404

            reviewer_cannot_propose = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses",
                headers=reviewer_headers,
                json={
                    "statement": "a reviewer should not be able to propose one",
                    "supporting_observation_ids": [str(first.observation_id)],
                },
            )
            assert reviewer_cannot_propose.status_code == 403

            hypothesis_review_body = {
                "decision": "accepted_by_reviewer",
                "rationale": "consistent with the candidate",
            }
            hypothesis_review = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses/{hypothesis_id}/review",
                headers=reviewer_headers,
                json=hypothesis_review_body,
            )
            assert hypothesis_review.status_code == 200, hypothesis_review.text
            assert hypothesis_review.json()["status"] == "accepted_by_reviewer"

            hypothesis_replay = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses/{hypothesis_id}/review",
                headers=reviewer_headers,
                json=hypothesis_review_body,
            )
            assert hypothesis_replay.status_code == 200

            hypothesis_conflict = await ac.post(
                f"/api/v1/cases/{case_id}/hypotheses/{hypothesis_id}/review",
                headers=reviewer_headers,
                json={"decision": "rejected_by_reviewer"},
            )
            assert hypothesis_conflict.status_code == 409

            hypothesis_node = await graph_repository.read(
                "MATCH (h:Hypothesis {case_id: $case_id, hypothesis_id: $hypothesis_id})"
                "-[:SUPPORTED_BY_OBSERVATION]->(o:Observation) "
                "RETURN h.status AS status, h.statement_commitment_sha256 AS commitment, "
                "count(o) AS observation_count",
                {"case_id": str(case_id), "hypothesis_id": hypothesis_id},
            )
            assert hypothesis_node[0]["status"] == "accepted_by_reviewer"
            assert hypothesis_node[0]["observation_count"] == 1
            assert hypothesis_node[0]["commitment"] is not None

            reference_edge = await graph_repository.read(
                "MATCH (h:Hypothesis {case_id: $case_id, hypothesis_id: $hypothesis_id})"
                "-[:REFERENCES_CANDIDATE]->(c:Correlation) RETURN count(c) AS n",
                {"case_id": str(case_id), "hypothesis_id": hypothesis_id},
            )
            assert reference_edge[0]["n"] == 1

            # --- append-only: hypothesis_actions rejects direct mutation too ---
            async with engine.connect() as conn:
                action_row = (
                    await conn.execute(
                        sa.select(hypothesis_actions_table.c.hypothesis_action_id).where(
                            hypothesis_actions_table.c.hypothesis_id == UUID(hypothesis_id)
                        )
                    )
                ).first()
            assert action_row is not None
            async with engine.connect() as conn:
                with pytest.raises(sa.exc.DBAPIError):
                    async with conn.begin():
                        await conn.execute(
                            hypothesis_actions_table.delete().where(
                                hypothesis_actions_table.c.hypothesis_action_id
                                == action_row.hypothesis_action_id
                            )
                        )

            # --- reconciliation: idempotent, replays only when genuinely missing ---
            # This fixture builds its two evidence rows and its correlation
            # directly (never through `upload_evidence`/`run_case_correlation_pass`,
            # the seams that record their integrity leaves), so those three
            # are genuinely missing until reconciliation backfills them here --
            # that is reconciliation doing its job, not a Part 5 defect. The
            # review-decision and both hypothesis-action leaves were already
            # recorded live by the HTTP flow above, so only those three are
            # already-present replays on this same first pass.
            first_pass = await reconciliation.reconcile_case(case_id)
            assert first_pass.scanned == 6
            assert first_pass.missing == 3
            assert first_pass.recorded == 3
            assert first_pass.replayed == 3
            second_pass = await reconciliation.reconcile_case(case_id)
            assert second_pass.missing == 0
            assert second_pass.replayed == second_pass.scanned == 6
    finally:
        # `candidate_review_decisions` and `hypothesis_actions` are genuinely
        # append-only (the same PostgreSQL trigger integrity_events uses) --
        # by design, not even this cleanup can delete them. `hypotheses` FKs
        # from `hypothesis_actions`, so it is equally undeletable once a row
        # exists. This mirrors `tests/integration/integrity/conftest.py`'s
        # own documented precedent: leave them, scoped to this unguessable,
        # never-reused `case_id`, on a disposable integration database.
        await graph_repository.write(
            "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(case_id)}
        )
        await graph_repository.write(
            "MATCH (n {case_id: $case_id}) DETACH DELETE n", {"case_id": str(other_case_id)}
        )
        await graph_repository.close()
        async with engine.begin() as conn:
            # `case_memberships` FKs to `cases` -- must go before `_cleanup`
            # below deletes the `cases` row itself.
            await conn.execute(
                sa.text("DELETE FROM case_memberships WHERE case_id = :c"), {"c": case_id}
            )
        await _cleanup(outbox, case_id)
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("DELETE FROM users WHERE email_normalized = ANY(:emails)"),
                {"emails": [investigator_email, reviewer_email, outsider_email]},
            )
        await engine.dispose()
