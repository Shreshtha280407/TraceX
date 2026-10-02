"""Phase 0 contract checks; intentionally stdlib-only for offline replay."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCHEMAS = REPO / "schemas" / "v1"
EXAMPLES = REPO / "examples"
FIXTURE = REPO / "fixtures" / "demo_100k"


class PhaseZeroContractTests(unittest.TestCase):
    def test_every_schema_and_example_is_json_and_examples_meet_top_level_contract(self) -> None:
        schema_files = sorted(path for path in SCHEMAS.glob("*.schema.json") if path.name != "common.schema.json")
        self.assertEqual(10, len(schema_files))
        for schema_path in schema_files:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual("object", schema["type"], schema_path.name)
            self.assertFalse(schema["additionalProperties"], schema_path.name)
            required = set(schema["required"])
            example = json.loads((EXAMPLES / schema_path.name.replace(".schema", "")).read_text(encoding="utf-8"))
            self.assertTrue(required.issubset(example), schema_path.name)
            self.assertEqual("1.0.0", example["schema_version"])
            self.assertFalse(set(example).difference(schema["properties"]), schema_path.name)

    def test_manifest_is_honest_about_unavailable_official_data(self) -> None:
        data_manifest = json.loads((REPO / "data_manifest.json").read_text(encoding="utf-8"))
        official = next(item for item in data_manifest["sources"] if item["source_id"] == "src-sih-ps-linked-material")
        # The PS document itself was reviewed: it publishes no dataset (synthetic data is expected),
        # so no official bytes or labels may be claimed.
        self.assertEqual("reviewed_no_official_dataset_published", official["status"])
        self.assertIn("Dataset Link: Nil", official["dataset_statement"])
        self.assertNotIn("sha256", official)
        synthetic = next(item for item in data_manifest["sources"] if item["source_id"] == "src-demo-100k-config")
        self.assertEqual(hashlib.sha256((FIXTURE / "fixture_config.json").read_bytes()).hexdigest(), synthetic["sha256"])

    def test_fixture_manifest_matches_contract(self) -> None:
        manifest = json.loads((FIXTURE / "fixture_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(100000, manifest["counts"]["transactions"])
        self.assertEqual(100000, manifest["invariants"]["canonical_transaction_count"])
        self.assertGreater(manifest["invariants"]["unresolved_outpoint_transaction_count"], 0)
        self.assertGreater(manifest["invariants"]["exchange_batch_transaction_count"], 0)
        self.assertGreater(manifest["invariants"]["duplicate_candidate_count"], 0)
        self.assertFalse(manifest["invariants"]["ownership_labels_present_in_public_fixture"])
        for name, details in manifest["generated_files"].items():
            self.assertRegex(details["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertGreater(details["bytes"], 0, name)

    def test_fixture_regenerates_byte_for_byte(self) -> None:
        completed = subprocess.run([sys.executable, str(FIXTURE / "generate.py"), "--verify"], cwd=REPO, check=False, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("fixture verification passed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
