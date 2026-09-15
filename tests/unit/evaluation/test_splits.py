"""Coverage for case-level split rules and the real synthetic case plan.

Proof points 12, 13, and 14 from the Phase 7 Part 1 task spec.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.evaluation.models import CaseGroupKind, CaseGroupV1, SyntheticCasePlanV1
from app.modules.evaluation.splits import (
    assert_case_not_reserved_for_tuning,
    case_ids_for_group,
    load_synthetic_case_plan,
)

_REQUIRED_MODALITIES = ("document",)
_REQUIRED_SCENARIOS = (
    "shared_identifier_across_modalities",
    "time_proximate_event_sequence",
    "location_camera_tower_linkage",
    "intentional_ambiguity_or_contradiction",
    "evidence_supported_true_link",
    "plausible_unsupported_non_link",
    "source_locators_for_every_finding",
)
_REQUIRED_ASSERTIONS = (
    "evidence_and_provenance_report_required",
    "investigator_intelligence_lead_report_required",
)


def _plan(**overrides: object) -> SyntheticCasePlanV1:
    fields: dict[str, object] = {
        "schema_version": "v1",
        "case_count_minimum": 12,
        "case_count_maximum": 15,
        "split_strategy": "case-level split",
        "case_groups": (
            CaseGroupV1(
                group=CaseGroupKind.DEVELOPMENT,
                case_ids=tuple(f"case-dev-{i}" for i in range(6)),
            ),
            CaseGroupV1(
                group=CaseGroupKind.VALIDATION,
                case_ids=tuple(f"case-val-{i}" for i in range(3)),
            ),
            CaseGroupV1(
                group=CaseGroupKind.HOLDOUT,
                case_ids=tuple(f"case-hold-{i}" for i in range(3)),
            ),
        ),
        "leakage_rules": ("a case appears in exactly one group",),
        "operation_nightfall_policy": "separate private showcase holdout, never tuned on",
        "truth_file_policy": "never committed, never shown in the investigator UI",
        "required_modalities": _REQUIRED_MODALITIES,
        "required_cross_modal_scenarios": _REQUIRED_SCENARIOS,
        "required_report_assertions": _REQUIRED_ASSERTIONS,
    }
    fields.update(overrides)
    return SyntheticCasePlanV1.model_validate(fields)


def test_real_synthetic_case_plan_loads_and_is_within_the_frozen_bounds() -> None:
    plan = load_synthetic_case_plan()
    total_cases = sum(len(group.case_ids) for group in plan.case_groups)
    assert 12 <= total_cases <= 15
    dev = case_ids_for_group(plan, CaseGroupKind.DEVELOPMENT)
    val = case_ids_for_group(plan, CaseGroupKind.VALIDATION)
    holdout = case_ids_for_group(plan, CaseGroupKind.HOLDOUT)
    assert not (set(dev) & set(val) & set(holdout))


def test_case_level_split_rejects_the_same_case_in_two_groups() -> None:
    """Proof point 12."""
    with pytest.raises(ValidationError, match="leakage"):
        _plan(
            case_groups=(
                CaseGroupV1(group=CaseGroupKind.DEVELOPMENT, case_ids=("shared-case",)),
                CaseGroupV1(group=CaseGroupKind.VALIDATION, case_ids=("shared-case",)),
            )
        )


@pytest.mark.parametrize("count", [11, 16])
def test_case_plan_count_outside_12_to_15_is_rejected(count: int) -> None:
    """Proof point 13."""
    with pytest.raises(ValidationError):
        _plan(case_count_minimum=count, case_count_maximum=count)


def test_case_plan_total_cases_must_match_its_own_declared_bounds() -> None:
    """Proof point 13 (in part): the declared min/max must match reality, not just be in [12,15]."""
    with pytest.raises(ValidationError, match="total planned case count"):
        _plan(
            case_count_minimum=14,
            case_count_maximum=15,
            case_groups=(
                CaseGroupV1(group=CaseGroupKind.DEVELOPMENT, case_ids=("only-one-case",)),
            ),
        )


def test_operation_nightfall_cannot_appear_in_a_tunable_case_group() -> None:
    """Proof point 14."""
    with pytest.raises(ValidationError, match="Nightfall"):
        CaseGroupV1(group=CaseGroupKind.DEVELOPMENT, case_ids=("operation-nightfall-case-1",))


def test_operation_nightfall_cannot_appear_in_holdout_either() -> None:
    """Proof point 14: not even the plan's own regular holdout group -- Nightfall is separate."""
    with pytest.raises(ValidationError, match="Nightfall"):
        CaseGroupV1(group=CaseGroupKind.HOLDOUT, case_ids=("nightfall-holdout-case",))


def test_assert_case_not_reserved_for_tuning_blocks_validation_and_holdout_cases() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="reserved for validation/holdout"):
        assert_case_not_reserved_for_tuning(plan, "case-val-0")
    with pytest.raises(ValueError, match="reserved for validation/holdout"):
        assert_case_not_reserved_for_tuning(plan, "case-hold-0")
    assert_case_not_reserved_for_tuning(plan, "case-dev-0")  # does not raise
