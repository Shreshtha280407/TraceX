"""Offline evaluation of entity-resolution candidate quality against truth data.

Gap-Closure WP-7B (G1): replaces the previous permanent stub
(`deferred_evaluation_report`, kept below unchanged for the still-valid
"no truth data configured" case) with a real evaluator. Never touches a
live request path -- see `truth_loader.py`'s module docstring for the
import boundary this module inherits and
`tests/unit/graph/intelligence/test_evaluation_import_boundary.py` for
its static enforcement.

Deliberately narrow scope: scores only entity-resolution candidate
quality (precision/recall/false-link/false-merge/precision-recall@k)
against a human-authored truth spec of entity pairs. `temporal_boundary_
correctness` remains `None` in every report -- no event/temporal truth
schema was designed in this WP (see `docs/qa/known-limitations.md`'s
"WP-7B" section); reporting a fabricated number would be worse than
reporting none. See `truth_loader.py`'s module docstring for the import
boundary and its test.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.core.canonical import canonical_sha256
from app.modules.graph.entity_models import EntityReviewOutcome
from app.modules.graph.entity_repository import EntityRepository
from app.modules.graph.intelligence.truth_loader import (
    EntityPairLabel,
    TruthSpec,
    load_truth_spec,
    resolve_synthetic_data_root,
)
from app.modules.graph.models import GraphModel

#: Default cutoff for precision@k/recall@k over candidates ranked by
#: `vector_score` (highest first). Not configurable via truth data itself
#: -- a fixed evaluation parameter, like `rules_config_hash`.
DEFAULT_TOP_K = 10


class OfflineEvaluationReport(GraphModel):
    dataset_hash: str
    truth_hash: str | None = None
    rules_config_hash: str
    command: str
    metrics: dict[str, float | int | None]
    deferred: bool = True


def deferred_evaluation_report(*, rules_config_hash: str) -> OfflineEvaluationReport:
    return OfflineEvaluationReport(
        dataset_hash="unavailable",
        rules_config_hash=rules_config_hash,
        command="phase5-offline-evaluation",
        metrics={
            "candidate_precision": None,
            "candidate_recall": None,
            "precision_at_k": None,
            "recall_at_k": None,
            "false_link_rate": None,
            "false_merge_rate": None,
            "temporal_boundary_correctness": None,
        },
    )


def _pair_key(left: UUID, right: UUID) -> frozenset[UUID]:
    """Order-independent identity for an (unordered) entity pair."""
    return frozenset({left, right})


async def _predicted_labels(
    repository: EntityRepository, case_id: UUID
) -> dict[frozenset[UUID], EntityPairLabel]:
    """The system's own effective judgement for every candidate pair it
    ever generated for this case -- `verified_same` -> `SAME`, any other
    decision (or none at all, i.e. `needs_review`) -> `DIFFERENT`. A pair
    the system never even generated a candidate for has no entry here at
    all -- callers must treat "missing" and "predicted different"
    identically (both mean "the system did not assert these are the same
    identity"), which `_score_candidates` below does.
    """
    predictions: dict[frozenset[UUID], EntityPairLabel] = {}
    for candidate in await repository.list_candidates(case_id):
        decisions = await repository.list_decisions(
            case_id, candidate.entity_resolution_candidate_id
        )
        latest = decisions[-1] if decisions else None
        label = (
            EntityPairLabel.SAME
            if latest is not None and latest.decision is EntityReviewOutcome.VERIFIED_SAME
            else EntityPairLabel.DIFFERENT
        )
        predictions[_pair_key(candidate.left_entity_id, candidate.right_entity_id)] = label
    return predictions


def _score_candidates(
    truth: TruthSpec, predictions: dict[frozenset[UUID], EntityPairLabel]
) -> dict[str, float | int | None]:
    true_positive = false_positive = false_negative = true_negative = 0
    for pair in truth.entity_pairs:
        key = _pair_key(pair.left_entity_id, pair.right_entity_id)
        # A pair with no generated candidate is an implicit DIFFERENT
        # prediction -- the system never asserted these are the same
        # identity, which is exactly what an absent candidate means.
        predicted = predictions.get(key, EntityPairLabel.DIFFERENT)
        if pair.label is EntityPairLabel.SAME and predicted is EntityPairLabel.SAME:
            true_positive += 1
        elif pair.label is EntityPairLabel.DIFFERENT and predicted is EntityPairLabel.SAME:
            false_positive += 1
        elif pair.label is EntityPairLabel.SAME and predicted is EntityPairLabel.DIFFERENT:
            false_negative += 1
        else:
            true_negative += 1

    predicted_same = true_positive + false_positive
    actual_same = true_positive + false_negative
    actual_different = false_positive + true_negative
    return {
        "candidate_precision": (true_positive / predicted_same) if predicted_same else None,
        "candidate_recall": (true_positive / actual_same) if actual_same else None,
        "false_link_rate": (false_positive / predicted_same) if predicted_same else None,
        "false_merge_rate": (false_positive / actual_different) if actual_different else None,
        "temporal_boundary_correctness": None,
    }


def _score_at_k(
    candidates_by_score: list[tuple[frozenset[UUID], float]],
    truth_same_pairs: frozenset[frozenset[UUID]],
    *,
    k: int,
) -> tuple[float | None, float | None]:
    """Precision/recall@k over candidates ranked by `vector_score`
    (descending, `None` scores sort last -- never crash on a candidate
    with no score). `None`/`None` when there is nothing to rank or no
    truth-positive pairs to recall against, never a fabricated `0.0`."""
    if not candidates_by_score or not truth_same_pairs:
        return None, None
    top_k = [key for key, _score in candidates_by_score[:k]]
    hits = sum(1 for key in top_k if key in truth_same_pairs)
    precision_at_k = hits / len(top_k) if top_k else None
    recall_at_k = hits / len(truth_same_pairs)
    return precision_at_k, recall_at_k


async def run_offline_evaluation(
    repository: EntityRepository,
    case_id: UUID,
    *,
    rules_config_hash: str,
    synthetic_data_root: Path | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> OfflineEvaluationReport:
    """Score this case's real, already-generated entity-resolution
    candidates against a human-authored truth spec.

    Skips cleanly (returns `deferred_evaluation_report`) when no truth
    root is configured (`synthetic_data_root` explicit, else `TRACEX_
    SYNTHETIC_DATA_ROOT`) -- never an exception for the common "no truth
    data available" case. Raises `truth_loader.TruthLoadError` if a root
    *is* configured but this case's truth file is missing/malformed --
    that is a real configuration error, not something to skip past
    silently.
    """
    root = resolve_synthetic_data_root(synthetic_data_root)
    if root is None:
        return deferred_evaluation_report(rules_config_hash=rules_config_hash)

    truth = load_truth_spec(root, case_id)
    predictions = await _predicted_labels(repository, case_id)
    metrics = _score_candidates(truth, predictions)

    candidates = await repository.list_candidates(case_id)
    ranked = sorted(
        (
            (_pair_key(c.left_entity_id, c.right_entity_id), c.vector_score)
            for c in candidates
            if c.vector_score is not None
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    truth_same_pairs = frozenset(
        _pair_key(p.left_entity_id, p.right_entity_id)
        for p in truth.entity_pairs
        if p.label is EntityPairLabel.SAME
    )
    precision_at_k, recall_at_k = _score_at_k(ranked, truth_same_pairs, k=top_k)
    metrics["precision_at_k"] = precision_at_k
    metrics["recall_at_k"] = recall_at_k

    return OfflineEvaluationReport(
        dataset_hash=canonical_sha256({"case_id": str(case_id), "root": str(root)}),
        truth_hash=canonical_sha256(truth.model_dump(mode="json")),
        rules_config_hash=rules_config_hash,
        command="phase5-offline-evaluation",
        metrics=metrics,
        deferred=False,
    )
