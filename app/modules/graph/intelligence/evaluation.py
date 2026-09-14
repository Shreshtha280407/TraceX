"""Offline-only evaluation report shape; no production truth data is loaded."""

from __future__ import annotations

from app.modules.graph.models import GraphModel


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
