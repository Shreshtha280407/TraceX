"""Pure-function coverage for `intelligence/pipeline.py::build_case_correlation_submission`.

The I/O half (`run_case_correlation_pass`/`run_case_analytics_snapshot`/
`run_case_motif_snapshot`, which reach real PostgreSQL) is covered live in
`tests/integration/graph/test_intelligence_pipeline_live.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.evaluation.release_freeze import ReleaseFreezeError
from app.modules.graph.intelligence import pipeline as pipeline_module
from app.modules.graph.intelligence.models import CandidateStatus
from app.modules.graph.intelligence.pipeline import (
    build_case_correlation_submission,
    run_case_correlation_pass,
)

_EXTRACTOR = Extractor(name="fixture", version="1.0.0", config_hash="h", model_version="n/a")


def _observation(
    *,
    case_id,
    observation_type: str,
    attributes: dict[str, object] | None = None,
    extracted_entities: list[ExtractedEntityMention] | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type=observation_type,
        extracted_entities=extracted_entities or [],
        attributes=attributes or {},
        extraction_confidence=0.9,
        source_locator=SourceLocator(page=1, span_start=0, span_end=5),
        extractor=_EXTRACTOR,
        created_at=datetime.now(UTC),
    )


def _phone_pair(case_id, value: str = "9876543210"):
    return (
        _observation(
            case_id=case_id,
            observation_type="phone_number_mention",
            extracted_entities=[
                ExtractedEntityMention(text=value, entity_type_hint="phone_number")
            ],
        ),
        _observation(
            case_id=case_id,
            observation_type="phone_number_mention",
            extracted_entities=[
                ExtractedEntityMention(text=value, entity_type_hint="phone_number")
            ],
        ),
    )


# --- Scenario 1: case isolation for every retrieval path --------------------


def test_rejects_observations_that_do_not_match_the_requested_case_id() -> None:
    case_id = uuid4()
    wrong_case_observation = _observation(
        case_id=uuid4(),
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="9876543210", entity_type_hint="phone_number")
        ],
    )
    with pytest.raises(ValueError, match="case_id"):
        build_case_correlation_submission([wrong_case_observation], case_id)


def test_two_cases_worth_of_identical_observations_never_cross_contaminate() -> None:
    """A mixed-case observation list is rejected outright -- never silently
    filtered down to just the requested case, and never partially processed."""
    case_a, case_b = uuid4(), uuid4()
    obs_a1, obs_a2 = _phone_pair(case_a)
    obs_b1, obs_b2 = _phone_pair(case_b)
    with pytest.raises(ValueError, match="case_id"):
        build_case_correlation_submission([obs_a1, obs_a2, obs_b1, obs_b2], case_a)
    # Each case processed on its own, unmixed, still works correctly.
    submission_a = build_case_correlation_submission([obs_a1, obs_a2], case_a)
    submission_b = build_case_correlation_submission([obs_b1, obs_b2], case_b)
    assert submission_a is not None and submission_b is not None
    assert obs_b1.observation_id not in submission_a.supporting_observation_ids
    assert obs_a1.observation_id not in submission_b.supporting_observation_ids


# --- Scenario 7/8: feature snapshot / rules hash determinism ----------------


def test_identical_observations_produce_an_identical_submission() -> None:
    case_id = uuid4()
    first, second = _phone_pair(case_id)
    submission_a = build_case_correlation_submission([first, second], case_id)
    submission_b = build_case_correlation_submission([first, second], case_id)
    assert submission_a is not None and submission_b is not None
    assert submission_a.idempotency_key == submission_b.idempotency_key
    assert submission_a.feature_snapshot is not None
    assert submission_b.feature_snapshot is not None
    assert submission_a.feature_snapshot.config_hash == submission_b.feature_snapshot.config_hash


def test_a_changed_observation_changes_the_idempotency_key_and_hash() -> None:
    case_id = uuid4()
    first, second = _phone_pair(case_id, value="9876543210")
    changed_first, changed_second = _phone_pair(case_id, value="9876500000")
    baseline = build_case_correlation_submission([first, second], case_id)
    changed = build_case_correlation_submission([changed_first, changed_second], case_id)
    assert baseline is not None and changed is not None
    assert baseline.idempotency_key != changed.idempotency_key
    assert baseline.feature_snapshot.config_hash != changed.feature_snapshot.config_hash  # type: ignore[union-attr]


def test_too_few_retrieval_eligible_observations_returns_none_not_an_error() -> None:
    case_id = uuid4()
    (only,) = (_phone_pair(case_id)[0],)
    assert build_case_correlation_submission([only], case_id) is None
    assert build_case_correlation_submission([], case_id) is None


def test_no_shared_reason_between_observations_returns_none() -> None:
    case_id = uuid4()
    unrelated_a = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="9876543210", entity_type_hint="phone_number")
        ],
    )
    unrelated_b = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="1112223334", entity_type_hint="phone_number")
        ],
    )
    assert build_case_correlation_submission([unrelated_a, unrelated_b], case_id) is None


# --- Scenario: candidate-only status, never a verified/merge outcome -------


def test_submission_status_is_needs_review_never_a_verified_status() -> None:
    case_id = uuid4()
    first, second = _phone_pair(case_id)
    submission = build_case_correlation_submission([first, second], case_id)
    assert submission is not None
    from app.modules.graph.integration_models import PropositionStatus

    assert submission.status in {PropositionStatus.CANDIDATE, PropositionStatus.NEEDS_REVIEW}
    for link in submission.candidate_links:
        assert link.status in {PropositionStatus.CANDIDATE, PropositionStatus.NEEDS_REVIEW}
    # `CandidateStatus`/`PropositionStatus` both deliberately have no
    # "verified"/"merged"/"identity_confirmed" member at all.
    assert "verified" not in {status.value for status in CandidateStatus}


async def test_disabled_release_configuration_fails_before_case_data_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval_called = False

    async def _fetch(*_args: object, **_kwargs: object) -> list[ObservationV1]:
        nonlocal retrieval_called
        retrieval_called = True
        return []

    monkeypatch.setattr(pipeline_module, "fetch_case_observations", _fetch)
    monkeypatch.setattr(
        pipeline_module,
        "get_settings",
        lambda: SimpleNamespace(
            release_configuration_id="tracex-release-v1-baseline",
            release_configuration_disabled=True,
        ),
    )

    with pytest.raises(ReleaseFreezeError, match="disabled"):
        await run_case_correlation_pass(object(), object(), uuid4())  # type: ignore[arg-type]
    assert retrieval_called is False
