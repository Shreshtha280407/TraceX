"""Loads and validates the frozen Dataset Manifest V1.

The manifest itself lives in `configs/benchmarks/dataset-manifest.v1.json`
as plain JSON (no new YAML dependency) -- this module is the only place
that reads it, so every consumer gets `DatasetManifestV1`'s pydantic
validation for free, not a raw dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.evaluation.models import DatasetManifestEntryV1, DatasetManifestV1

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_MANIFEST_PATH = REPO_ROOT / "configs" / "benchmarks" / "dataset-manifest.v1.json"


def load_dataset_manifest(path: Path = DATASET_MANIFEST_PATH) -> DatasetManifestV1:
    data = json.loads(path.read_text(encoding="utf-8"))
    return DatasetManifestV1.model_validate(data)


def get_dataset(manifest: DatasetManifestV1, dataset_id: str) -> DatasetManifestEntryV1:
    entry = manifest.get(dataset_id)
    if entry is None:
        raise KeyError(f"no dataset manifest entry for '{dataset_id}'")
    return entry
