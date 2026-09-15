"""Loads and validates the frozen Model Candidate Catalog V1.

Candidates only -- see `models.py::ModelCandidateV1._reject_selected_in_part_1`
for the structural guarantee that nothing in this catalogue can start
Phase 7 Part 1 already marked `selected`.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.evaluation.models import CandidateTask, ModelCandidateCatalogV1, ModelCandidateV1

REPO_ROOT = Path(__file__).resolve().parents[3]
MODEL_CANDIDATE_CATALOG_PATH = REPO_ROOT / "configs" / "benchmarks" / "model-candidates.v1.json"


def load_model_candidate_catalog(
    path: Path = MODEL_CANDIDATE_CATALOG_PATH,
) -> ModelCandidateCatalogV1:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ModelCandidateCatalogV1.model_validate(data)


def candidates_for_task(
    catalog: ModelCandidateCatalogV1, task: CandidateTask
) -> tuple[ModelCandidateV1, ...]:
    return catalog.by_task(task)
