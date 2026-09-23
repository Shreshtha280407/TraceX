"""Gap-Closure WP-7B (G1): the real offline entity-resolution evaluator.

`test_no_api_route_ever_imports_the_evaluator_or_truth_loader` is this
module's real safety guarantee (see `truth_loader.py`'s own docstring):
offline evaluation must never be reachable from a live request path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.modules.graph.entity_models import (
    EntityResolutionCandidateRecord,
    EntityReviewDecisionRecord,
    EntityReviewOutcome,
)
from app.modules.graph.intelligence.evaluation import (
    OfflineEvaluationReport,
    deferred_evaluation_report,
    run_offline_evaluation,
)
from app.modules.graph.intelligence.truth_loader import (
    ENV_SYNTHETIC_DATA_ROOT,
    TRUTH_SPEC_SCHEMA_VERSION,
    TruthLoadError,
    load_truth_spec,
    resolve_synthetic_data_root,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "app"
_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_RULES_HASH = "synthetic-rules-hash"


class _FakeEntityRepository:
    """Duck-typed stand-in for `EntityRepository`: only the two read
    methods the evaluator actually calls."""

    def __init__(
        self,
        candidates: list[EntityResolutionCandidateRecord],
        decisions: dict[object, list[EntityReviewDecisionRecord]],
    ) -> None:
        self._candidates = candidates
        self._decisions = decisions

    async def list_candidates(self, case_id):
        return [c for c in self._candidates if c.case_id == case_id]

    async def list_decisions(self, case_id, entity_resolution_candidate_id):
        return self._decisions.get(entity_resolution_candidate_id, [])


def _candidate(case_id, left, right, *, vector_score=None) -> EntityResolutionCandidateRecord:
    return EntityResolutionCandidateRecord(
        entity_resolution_candidate_id=uuid4(),
        case_id=case_id,
        left_entity_id=left,
        right_entity_id=right,
        reasons=(),
        supporting_observation_ids=(),
        config_version="test",
        vector_score=vector_score,
        created_at=_NOW,
    )


def _decision(outcome: EntityReviewOutcome) -> EntityReviewDecisionRecord:
    return EntityReviewDecisionRecord(
        entity_review_decision_id=uuid4(),
        case_id=uuid4(),
        entity_resolution_candidate_id=uuid4(),
        decision=outcome,
        reviewer_user_id=uuid4(),
        rationale=None,
        rationale_commitment_sha256=None,
        created_at=_NOW,
    )


def _truth_file(tmp_path: Path, case_id, entity_pairs: list[dict]) -> Path:
    root = tmp_path / "synthetic-data"
    case_dir = root / str(case_id)
    case_dir.mkdir(parents=True)
    (case_dir / "entity_resolution_truth.json").write_text(
        f'{{"schema_version": "{TRUTH_SPEC_SCHEMA_VERSION}", "case_id": "{case_id}", '
        f'"entity_pairs": {entity_pairs!r}}}'.replace("'", '"')
    )
    return root


# --- resolve_synthetic_data_root / load_truth_spec --------------------------


def test_resolve_synthetic_data_root_is_none_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv(ENV_SYNTHETIC_DATA_ROOT, raising=False)
    assert resolve_synthetic_data_root() is None


def test_resolve_synthetic_data_root_reads_the_env_var(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(ENV_SYNTHETIC_DATA_ROOT, str(tmp_path))
    assert resolve_synthetic_data_root() == tmp_path


def test_resolve_synthetic_data_root_explicit_argument_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(ENV_SYNTHETIC_DATA_ROOT, "/should-not-be-used")
    explicit = tmp_path / "explicit-root"
    assert resolve_synthetic_data_root(explicit) == explicit


def test_load_truth_spec_raises_for_a_missing_file(tmp_path) -> None:
    with pytest.raises(TruthLoadError):
        load_truth_spec(tmp_path, uuid4())


def test_load_truth_spec_raises_for_a_case_id_mismatch(tmp_path) -> None:
    case_id = uuid4()
    other_id = uuid4()
    root = _truth_file(tmp_path, other_id, [])
    with pytest.raises(TruthLoadError):
        load_truth_spec(root, case_id)


# --- run_offline_evaluation ---------------------------------------------------


async def test_run_offline_evaluation_skips_cleanly_when_no_root_configured(monkeypatch) -> None:
    monkeypatch.delenv(ENV_SYNTHETIC_DATA_ROOT, raising=False)
    repository = _FakeEntityRepository([], {})
    report = await run_offline_evaluation(repository, uuid4(), rules_config_hash=_RULES_HASH)
    assert report == deferred_evaluation_report(rules_config_hash=_RULES_HASH)
    assert report.deferred is True


async def test_run_offline_evaluation_scores_a_perfect_system(tmp_path) -> None:
    case_id = uuid4()
    left, right = uuid4(), uuid4()
    root = _truth_file(
        tmp_path,
        case_id,
        [{"left_entity_id": str(left), "right_entity_id": str(right), "label": "same"}],
    )
    candidate = _candidate(case_id, left, right, vector_score=0.9)
    decisions = {
        candidate.entity_resolution_candidate_id: [_decision(EntityReviewOutcome.VERIFIED_SAME)]
    }
    repository = _FakeEntityRepository([candidate], decisions)

    report = await run_offline_evaluation(
        repository, case_id, rules_config_hash=_RULES_HASH, synthetic_data_root=root
    )
    assert report.deferred is False
    assert report.metrics["candidate_precision"] == 1.0
    assert report.metrics["candidate_recall"] == 1.0
    assert report.metrics["false_link_rate"] == 0.0


async def test_run_offline_evaluation_scores_a_false_merge(tmp_path) -> None:
    """The system verified a pair as same, but truth says different."""
    case_id = uuid4()
    left, right = uuid4(), uuid4()
    root = _truth_file(
        tmp_path,
        case_id,
        [{"left_entity_id": str(left), "right_entity_id": str(right), "label": "different"}],
    )
    candidate = _candidate(case_id, left, right, vector_score=0.5)
    decisions = {
        candidate.entity_resolution_candidate_id: [_decision(EntityReviewOutcome.VERIFIED_SAME)]
    }
    repository = _FakeEntityRepository([candidate], decisions)

    report = await run_offline_evaluation(
        repository, case_id, rules_config_hash=_RULES_HASH, synthetic_data_root=root
    )
    assert report.metrics["false_merge_rate"] == 1.0
    assert report.metrics["candidate_precision"] == 0.0


async def test_run_offline_evaluation_missing_candidate_counts_as_a_miss(tmp_path) -> None:
    """A truth-labeled SAME pair the system never even generated a
    candidate for is a false negative, not silently ignored."""
    case_id = uuid4()
    left, right = uuid4(), uuid4()
    root = _truth_file(
        tmp_path,
        case_id,
        [{"left_entity_id": str(left), "right_entity_id": str(right), "label": "same"}],
    )
    repository = _FakeEntityRepository([], {})

    report = await run_offline_evaluation(
        repository, case_id, rules_config_hash=_RULES_HASH, synthetic_data_root=root
    )
    assert report.metrics["candidate_recall"] == 0.0


async def test_run_offline_evaluation_never_reports_temporal_boundary_correctness(tmp_path) -> None:
    """Deliberately always None -- see this module's own docstring for why."""
    case_id = uuid4()
    root = _truth_file(tmp_path, case_id, [])
    repository = _FakeEntityRepository([], {})
    report = await run_offline_evaluation(
        repository, case_id, rules_config_hash=_RULES_HASH, synthetic_data_root=root
    )
    assert report.metrics["temporal_boundary_correctness"] is None


def test_offline_evaluation_report_is_json_serializable() -> None:
    report = deferred_evaluation_report(rules_config_hash=_RULES_HASH)
    assert isinstance(report, OfflineEvaluationReport)
    assert report.model_dump_json()


async def test_report_names_hardware_and_git_commit_additive_to_existing_fields(
    tmp_path,
) -> None:
    """Master plan §23.3: "Performance and accuracy reports name dataset
    hash, source profile, model/config, hardware and commit." Both new
    fields must populate on a REAL report generation (not a stub), and
    every pre-existing field (`dataset_hash`/`truth_hash`/
    `rules_config_hash`) must still be present unchanged -- additive, not
    a replacement."""
    case_id = uuid4()
    left, right = uuid4(), uuid4()
    root = _truth_file(
        tmp_path,
        case_id,
        [{"left_entity_id": str(left), "right_entity_id": str(right), "label": "same"}],
    )
    candidate = _candidate(case_id, left, right, vector_score=0.9)
    decisions = {
        candidate.entity_resolution_candidate_id: [_decision(EntityReviewOutcome.VERIFIED_SAME)]
    }
    repository = _FakeEntityRepository([candidate], decisions)

    report = await run_offline_evaluation(
        repository, case_id, rules_config_hash=_RULES_HASH, synthetic_data_root=root
    )

    # New fields: real values, this repo's own actual commit/hostname --
    # never a crash, never silently blank.
    assert report.git_commit != "unknown"
    assert len(report.git_commit) == 40
    assert report.hardware != "unknown"
    assert report.hardware.strip() != ""
    # Pre-existing fields: still present, unchanged shape.
    assert report.dataset_hash
    assert report.truth_hash
    assert report.rules_config_hash == _RULES_HASH
    assert report.deferred is False

    # The deferred (no-truth-configured) path names them too -- not only
    # the real-evaluation path.
    deferred = deferred_evaluation_report(rules_config_hash=_RULES_HASH)
    assert deferred.git_commit != "unknown"
    assert deferred.hardware != "unknown"


# --- Import boundary: never reachable from a live request path --------------


def test_no_api_route_ever_imports_the_evaluator_or_truth_loader() -> None:
    forbidden_modules = ("graph.intelligence.evaluation", "graph.intelligence.truth_loader")
    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        relative = path.relative_to(APP_ROOT)
        is_route_file = (
            relative.parts[:2] == ("api",)
            or relative.name.endswith("_api.py")
            or relative == Path("main.py")
        )
        if not is_route_file:
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_modules:
            if re.search(rf"\bimport {re.escape(forbidden)}\b", text) or re.search(
                rf"\bfrom app\.modules\.{re.escape(forbidden)} import\b", text
            ):
                offenders.append(f"{relative}: imports {forbidden}")
    assert not offenders, f"offline-evaluation modules imported from a route file: {offenders}"
