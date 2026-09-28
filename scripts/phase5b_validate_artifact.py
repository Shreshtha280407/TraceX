"""Phase 5B artifact compatibility guard.

Checks a saved candidate artifact's manifest (feature columns/order/hash,
preprocessing spec, python/scikit-learn/numpy versions) against a target feature
package's manifest, and rejects — clear message, non-zero exit — on any mismatch.
This is a comparison-phase safety guard only: it does not load the model into any
production path. Phase 5C/6 integration is separate, later, authorized work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


class ArtifactIncompatibleError(ValueError):
    pass


def load_json(path: Path, *, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise ArtifactIncompatibleError(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactIncompatibleError(f"{what} at {path} is not valid JSON: {exc}") from exc


def validate_compatibility(artifact_manifest: dict[str, Any], feature_manifest: dict[str, Any]) -> None:
    """Raises ArtifactIncompatibleError on the first mismatch found."""
    required_artifact_keys = {
        "candidate", "version", "feature_columns", "feature_columns_sha256",
        "preprocessing", "python_version", "sklearn_version", "numpy_version", "training_seed",
    }
    missing = required_artifact_keys - set(artifact_manifest)
    if missing:
        raise ArtifactIncompatibleError(f"artifact manifest is missing required keys: {sorted(missing)}")

    if "feature_columns_sha256" not in feature_manifest or "feature_columns" not in feature_manifest:
        raise ArtifactIncompatibleError("feature package manifest is missing feature_columns/feature_columns_sha256")

    if artifact_manifest["feature_columns"] != feature_manifest["feature_columns"]:
        raise ArtifactIncompatibleError(
            "feature column list/order mismatch between artifact and target feature package — "
            f"artifact has {len(artifact_manifest['feature_columns'])} columns, "
            f"target has {len(feature_manifest['feature_columns'])}"
        )
    if artifact_manifest["feature_columns_sha256"] != feature_manifest["feature_columns_sha256"]:
        raise ArtifactIncompatibleError(
            "feature_columns_sha256 mismatch — artifact was fit on a different frozen feature contract "
            f"({artifact_manifest['feature_columns_sha256'][:16]}… vs {feature_manifest['feature_columns_sha256'][:16]}…)"
        )
    if feature_manifest.get("feature_schema_version") and artifact_manifest.get("feature_schema_version") not in (
        None,
        feature_manifest["feature_schema_version"],
    ):
        raise ArtifactIncompatibleError(
            f"feature_schema_version mismatch: artifact={artifact_manifest.get('feature_schema_version')!r} "
            f"target={feature_manifest['feature_schema_version']!r}"
        )
    if artifact_manifest.get("preprocessing", {}).get("scaler") != "StandardScaler":
        raise ArtifactIncompatibleError("artifact's preprocessing spec is not the frozen StandardScaler contract")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument("--feature-manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        artifact_manifest = load_json(args.artifact_manifest, what="artifact manifest")
        feature_manifest = load_json(args.feature_manifest, what="feature package manifest")
        validate_compatibility(artifact_manifest, feature_manifest)
    except ArtifactIncompatibleError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"OK: artifact {artifact_manifest['candidate']!r} is compatible with the target feature package.")


if __name__ == "__main__":
    main()
