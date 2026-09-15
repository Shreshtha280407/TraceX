"""Case-level split loading and anti-leakage helpers.

Frozen rule 1: split by case, never by individual observation/frame/page/
row/clip/message -- see `models.py::CaseGroupV1`/`SyntheticCasePlanV1`,
whose own validators already reject a case_id appearing in two groups.
Frozen rule 2: never tune a model using validation/holdout cases -- this
module's `assert_case_not_reserved_for_tuning` is the reusable guard a
future benchmark-driving script calls before it lets a development-time
decision (a hyperparameter choice, a threshold tweak) see a case's data.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.evaluation.models import CaseGroupKind, SyntheticCasePlanV1

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_CASE_PLAN_PATH = REPO_ROOT / "configs" / "benchmarks" / "synthetic-case-plan.v1.json"


def load_synthetic_case_plan(path: Path = SYNTHETIC_CASE_PLAN_PATH) -> SyntheticCasePlanV1:
    data = json.loads(path.read_text(encoding="utf-8"))
    return SyntheticCasePlanV1.model_validate(data)


def case_ids_for_group(plan: SyntheticCasePlanV1, group: CaseGroupKind) -> tuple[str, ...]:
    for case_group in plan.case_groups:
        if case_group.group == group:
            return case_group.case_ids
    return ()


def assert_case_not_reserved_for_tuning(plan: SyntheticCasePlanV1, case_id: str) -> None:
    """Frozen rule 2: raise if `case_id` is reserved for validation/holdout.

    Call this before any development-time tuning decision (a threshold
    change, a hyperparameter choice, a manual review of a candidate's
    output) is allowed to see a case's data.
    """
    reserved = case_ids_for_group(plan, CaseGroupKind.VALIDATION) + case_ids_for_group(
        plan, CaseGroupKind.HOLDOUT
    )
    if case_id in reserved:
        raise ValueError(
            f"case '{case_id}' is reserved for validation/holdout and must never be tuned on"
        )
