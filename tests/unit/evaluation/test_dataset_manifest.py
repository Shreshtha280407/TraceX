"""Coverage for the real, frozen Dataset Manifest V1.

See `configs/benchmarks/dataset-manifest.v1.json`. Proof points 1, 5, 6, 7,
and 16 from the Phase 7 Part 1 task spec.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.evaluation.manifest import get_dataset, load_dataset_manifest
from app.modules.evaluation.models import DatasetRole, GitStoragePolicy

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_dataset_manifest_entry_parses() -> None:
    """Proof point 1."""
    manifest = load_dataset_manifest()
    assert len(manifest.datasets) >= 14
    for entry in manifest.datasets:
        assert entry.dataset_id
        assert entry.display_name


def test_every_primary_dataset_declares_tasks_claims_and_limitations() -> None:
    """Proof point 5."""
    manifest = load_dataset_manifest()
    primary = [d for d in manifest.datasets if d.role == DatasetRole.PRIMARY_BENCHMARK]
    assert len(primary) >= 7
    for entry in primary:
        assert entry.allowed_tasks, f"{entry.dataset_id} has no allowed_tasks"
        assert entry.prohibited_claims, f"{entry.dataset_id} has no prohibited_claims"
        assert entry.known_limitations, f"{entry.dataset_id} has no known_limitations"


def test_operation_nightfall_is_protected_as_private_showcase_holdout() -> None:
    """Proof point 6."""
    manifest = load_dataset_manifest()
    nightfall = get_dataset(manifest, "operation_nightfall_v1")
    assert nightfall.role == DatasetRole.PRIVATE_SHOWCASE_HOLDOUT
    assert nightfall.git_storage_policy == GitStoragePolicy.EXCLUDED_PRIVATE_NEVER_DOCUMENTED


def test_manifest_rejects_operation_nightfall_with_the_wrong_role() -> None:
    """Proof point 6: the manifest-level validator, not just the frozen JSON, enforces this."""
    manifest = load_dataset_manifest()
    tampered = manifest.model_dump(mode="json")
    for entry in tampered["datasets"]:
        if entry["dataset_id"] == "operation_nightfall_v1":
            entry["role"] = "primary_benchmark"
    from app.modules.evaluation.models import DatasetManifestV1

    with pytest.raises(ValidationError, match="operation_nightfall_v1"):
        DatasetManifestV1.model_validate(tampered)


def test_public_benchmark_datasets_cannot_be_marked_as_one_real_investigation() -> None:
    """Proof point 7."""
    manifest = load_dataset_manifest()
    assert manifest.composite_investigation_claim_allowed is False

    tampered = manifest.model_dump(mode="json")
    tampered["composite_investigation_claim_allowed"] = True
    from pydantic import ValidationError as PydanticValidationError

    from app.modules.evaluation.models import DatasetManifestV1

    with pytest.raises(PydanticValidationError):
        DatasetManifestV1.model_validate(tampered)


def test_gitignore_has_targeted_evaluation_data_safety_rules() -> None:
    """Proof point 16."""
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required_entry in (
        "/local-data/",
        "/model-cache/",
        "/benchmark-runs/",
        "/private-evaluation/",
        "/operation-nightfall-truth/",
    ):
        assert required_entry in gitignore_text, f"{required_entry} missing from .gitignore"
