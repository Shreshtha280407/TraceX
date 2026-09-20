"""Gate C release-freeze validation and fail-closed selection controls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.evaluation.release_freeze import (
    RELEASE_FREEZE_PATH,
    ReleaseFreezeError,
    ReleaseFreezeV1,
    get_release_configuration,
    load_release_freeze,
    require_release_component,
)
from app.modules.graph.intelligence.scoring import RULES_CONFIG_HASH


def _payload() -> dict[str, object]:
    return json.loads(RELEASE_FREEZE_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "release-freeze.v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_real_release_freeze_loads_and_classifies_every_catalogue_entry() -> None:
    freeze = load_release_freeze()
    assert freeze.current_evidence_frozen is True
    assert freeze.future_expansion_requires_versioned_cycle is True
    assert freeze.final_relationship_ml_selected is False
    assert len(freeze.datasets) == 14
    assert len(freeze.candidates) == 18
    assert {item.status.value for item in freeze.evidence} == {"succeeded", "unavailable"}


def test_unknown_candidate_in_release_configuration_is_rejected(tmp_path: Path) -> None:
    payload = _payload()
    payload["configurations"][0]["components"][0]["candidate_id"] = "unknown-model"  # type: ignore[index]
    with pytest.raises(ReleaseFreezeError, match="unknown candidate"):
        load_release_freeze(_write(tmp_path, payload))


def test_deferred_candidate_cannot_be_selected(tmp_path: Path) -> None:
    payload = _payload()
    payload["configurations"][0]["components"][0]["candidate_id"] = (  # type: ignore[index]
        "logistic-regression-correlation"
    )
    with pytest.raises(ReleaseFreezeError, match="unapproved candidate"):
        load_release_freeze(_write(tmp_path, payload))


def test_unavailable_candidate_cannot_be_selected(tmp_path: Path) -> None:
    payload = _payload()
    payload["configurations"][0]["components"][0]["candidate_id"] = (  # type: ignore[index]
        "pyannote-community-local"
    )
    with pytest.raises(ReleaseFreezeError, match="unapproved candidate"):
        load_release_freeze(_write(tmp_path, payload))


def test_malformed_hash_is_rejected_structurally() -> None:
    payload = _payload()
    payload["evidence"][0]["result_sha256"] = "not-a-sha256"  # type: ignore[index]
    with pytest.raises(ValidationError):
        ReleaseFreezeV1.model_validate(payload)


def test_approved_evidence_cannot_drop_its_artifact_hash() -> None:
    payload = _payload()
    payload["evidence"][0]["artifact_sha256"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="requires config, dataset, artifact, and result"):
        ReleaseFreezeV1.model_validate(payload)


def test_catalogue_hash_drift_is_rejected(tmp_path: Path) -> None:
    payload = _payload()
    payload["source_hashes"]["model_candidates_sha256"] = "a" * 64  # type: ignore[index]
    with pytest.raises(ReleaseFreezeError, match="source hashes"):
        load_release_freeze(_write(tmp_path, payload))


def test_arbitrary_artifact_path_is_rejected_as_an_unknown_field() -> None:
    payload = _payload()
    payload["configurations"][0]["components"][0]["artifact_path"] = "/tmp/model.bin"  # type: ignore[index]
    with pytest.raises(ValidationError):
        ReleaseFreezeV1.model_validate(payload)


def test_only_exact_active_rules_baseline_resolves() -> None:
    component = require_release_component(
        configuration_id="tracex-release-v1-baseline",
        purpose="relationship_scoring",
        candidate_id="phase5-rules-baseline",
        configuration_sha256=RULES_CONFIG_HASH,
    )
    assert component.artifact_loading == "none"
    assert component.final_relationship_ml_selection is False

    with pytest.raises(ReleaseFreezeError, match="configuration changed"):
        require_release_component(
            configuration_id="tracex-release-v1-baseline",
            purpose="relationship_scoring",
            candidate_id="phase5-rules-baseline",
            configuration_sha256="a" * 64,
        )


def test_unknown_configuration_and_disable_switch_fail_closed() -> None:
    with pytest.raises(ReleaseFreezeError, match="not approved"):
        get_release_configuration(load_release_freeze(), "unknown-configuration")
    with pytest.raises(ReleaseFreezeError, match="disabled"):
        require_release_component(
            configuration_id="tracex-release-v1-baseline",
            purpose="relationship_scoring",
            candidate_id="phase5-rules-baseline",
            configuration_sha256=RULES_CONFIG_HASH,
            disabled=True,
        )


def test_rollback_target_is_explicitly_disabled_and_empty() -> None:
    freeze = load_release_freeze()
    rollback = get_release_configuration(
        freeze, freeze.rollback_configuration_id, allow_disabled=True
    )
    assert rollback.enabled is False
    assert rollback.components == ()
