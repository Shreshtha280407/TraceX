"""Phase 6 Part 5: safe Neo4j projection for candidate review decisions and hypotheses.

Mirrors `intelligence/projection.py::project_correlation_context` exactly:
every write is provenance-gated (it can only ever touch nodes that already
exist because their own Evidence -> Observation chain was already
projected), idempotent via `MERGE` keyed on `(case_id, <domain>_id)` or
`(case_id, projection_key)`, and carries only safe, non-sensitive
properties -- IDs, enum values, timestamps, and commitments, never raw
reviewer rationale or hypothesis statement text.

A `False` return means "no matching node yet" (the Correlation this review
targets, or the Evidence/Observation chain a hypothesis cites, hasn't been
projected into Neo4j yet) -- a safe, explicit no-op, never a fabricated
node. A `GraphConnectionError` from `Neo4jGraphRepository.write` is left to
the caller: this module never itself decides whether a Neo4j outage should
block an otherwise-successful durable PostgreSQL write (see
`docs/architecture/phase-6-review-and-hypothesis.md`'s "best-effort
projection" section for why callers treat this the same as an integrity
recording failure -- log and continue, never roll back an already-committed
decision).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.modules.graph.hypothesis_models import HypothesisRecord
from app.modules.graph.repository import Neo4jGraphRepository
from app.modules.graph.review_models import CandidateReviewDecisionRecord


async def project_candidate_review_decision(
    repository: Neo4jGraphRepository,
    *,
    case_id: UUID,
    projection_key: str,
    decision: CandidateReviewDecisionRecord,
) -> bool:
    """`MATCH` (never `MERGE`) the existing Correlation node for this candidate's
    correlation and set safe review properties on it.

    Uses `MATCH`, not `MERGE`: a review decision must never fabricate a bare
    Correlation node lacking the Evidence -> Observation provenance
    `project_correlation_context` already required before creating one --
    if that projection hasn't happened yet, this is a safe no-op (`False`),
    reviewable again once the correlation itself has been projected.
    """
    query = (
        "MATCH (c:Correlation {case_id: $case_id, projection_key: $projection_key}) "
        "SET c += $properties "
        "RETURN c.projection_key AS projection_key"
    )
    records = await repository.write(
        query,
        {
            "case_id": str(case_id),
            "projection_key": projection_key,
            "properties": {
                "review_status": decision.decision.value,
                "reviewed_at": decision.created_at.isoformat(),
                "reviewer_rationale_commitment_sha256": decision.rationale_commitment_sha256,
                "candidate_review_decision_id": str(decision.candidate_review_decision_id),
            },
        },
    )
    return bool(records)


async def project_hypothesis(
    repository: Neo4jGraphRepository,
    *,
    hypothesis: HypothesisRecord,
    evidence_paths: tuple[tuple[UUID, UUID], ...],
) -> bool:
    """Provenance-gated `MERGE` of one Hypothesis node plus its observation edges.

    `evidence_paths` is `(evidence_id, observation_id)` for every one of
    `hypothesis.supporting_observation_ids` -- the caller resolves these
    from canonical PostgreSQL rows (never trusts a client-supplied evidence
    ID). All must already exist as a projected `Evidence
    -[:YIELDED_OBSERVATION]-> Observation` chain in this case, or nothing is
    written at all (same all-or-nothing gate `project_correlation_context`
    uses). A hypothesis citing only candidates (no direct observations) has
    an empty `evidence_paths` and is never itself the direct target of this
    function -- see `project_hypothesis_candidate_reference` for that edge.
    """
    if not evidence_paths:
        return False
    query = (
        "UNWIND $evidence_paths AS path "
        "MATCH (e:Evidence {case_id: $case_id, evidence_id: path.evidence_id})"
        "-[:YIELDED_OBSERVATION]->(o:Observation {case_id: $case_id, "
        "observation_id: path.observation_id}) "
        "WITH collect(o) AS observations, count(o) AS found "
        "WHERE found = size($evidence_paths) "
        "MERGE (h:Hypothesis {case_id: $case_id, hypothesis_id: $hypothesis_id}) "
        "SET h += $properties "
        "WITH h, observations "
        "UNWIND observations AS o "
        "MERGE (h)-[:SUPPORTED_BY_OBSERVATION]->(o) "
        "RETURN h.hypothesis_id AS hypothesis_id"
    )
    records = await repository.write(
        query,
        {
            "case_id": str(hypothesis.case_id),
            "hypothesis_id": str(hypothesis.hypothesis_id),
            "evidence_paths": [
                {"evidence_id": str(evidence_id), "observation_id": str(observation_id)}
                for evidence_id, observation_id in evidence_paths
            ],
            "properties": _hypothesis_properties(hypothesis),
        },
    )
    return bool(records)


async def project_hypothesis_candidate_reference(
    repository: Neo4jGraphRepository, *, case_id: UUID, hypothesis_id: UUID, projection_key: str
) -> bool:
    """Link an already-projected Hypothesis node to an already-projected Correlation node.

    Both sides use `MATCH`: neither a Hypothesis lacking its own observation
    provenance nor a Correlation lacking its own evidence provenance is ever
    fabricated by this edge.
    """
    query = (
        "MATCH (h:Hypothesis {case_id: $case_id, hypothesis_id: $hypothesis_id}) "
        "MATCH (c:Correlation {case_id: $case_id, projection_key: $projection_key}) "
        "MERGE (h)-[:REFERENCES_CANDIDATE]->(c) "
        "RETURN h.hypothesis_id AS hypothesis_id"
    )
    records = await repository.write(
        query,
        {
            "case_id": str(case_id),
            "hypothesis_id": str(hypothesis_id),
            "projection_key": projection_key,
        },
    )
    return bool(records)


async def project_hypothesis_review_decision(
    repository: Neo4jGraphRepository,
    *,
    case_id: UUID,
    hypothesis_id: UUID,
    status: str,
    decided_at: datetime,
    decided_by: UUID,
    rationale_commitment_sha256: str | None,
) -> bool:
    """`MATCH` (never `MERGE`) the existing Hypothesis node and set review state.

    Same no-fabrication rule as `project_candidate_review_decision`: if the
    hypothesis was never successfully projected (its own evidence chain was
    incomplete at creation time), this is a safe no-op.
    """
    query = (
        "MATCH (h:Hypothesis {case_id: $case_id, hypothesis_id: $hypothesis_id}) "
        "SET h += $properties "
        "RETURN h.hypothesis_id AS hypothesis_id"
    )
    records = await repository.write(
        query,
        {
            "case_id": str(case_id),
            "hypothesis_id": str(hypothesis_id),
            "properties": {
                "status": status,
                "decided_at": decided_at.isoformat(),
                "decided_by": str(decided_by),
                "reviewer_rationale_commitment_sha256": rationale_commitment_sha256,
            },
        },
    )
    return bool(records)


def _hypothesis_properties(hypothesis: HypothesisRecord) -> dict[str, object]:
    """The exact safe property set written to a `Hypothesis` node -- never raw text.

    `kind: "hypothesis"` makes the fact/candidate/hypothesis distinction
    queryable directly on the node, mirroring `Correlation`'s own
    `candidate_only: True` marker.
    """
    return {
        "case_id": str(hypothesis.case_id),
        "hypothesis_id": str(hypothesis.hypothesis_id),
        "status": hypothesis.status.value,
        "created_by": str(hypothesis.created_by),
        "created_at": hypothesis.created_at.isoformat(),
        "statement_commitment_sha256": hypothesis.statement_commitment_sha256,
        "rationale_commitment_sha256": hypothesis.rationale_commitment_sha256,
        "kind": "hypothesis",
    }


__all__ = [
    "project_candidate_review_decision",
    "project_hypothesis",
    "project_hypothesis_candidate_reference",
    "project_hypothesis_review_decision",
]
