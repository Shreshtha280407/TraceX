"""Exact production ML finding parity for isolated replays of the same snapshot.

Compares every published ML finding field except generated row IDs and database
timestamps. It does not refit, promote or substitute a research candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def fingerprint(work, *, scoring_only=False, normalize_run_identities=False):
    db = sqlite3.connect(f"file:{work / 'control.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        stages = db.execute("SELECT details FROM analysis_stages WHERE name='ml_scoring' "
                            "ORDER BY attempt DESC LIMIT 1").fetchone()
        details = json.loads(stages[0]) if stages else {}
        identities = {}
        if normalize_run_identities:
            for table, marker in (("cases", "CASE"), ("evidence_sources", "SOURCE"), ("snapshots", "SNAPSHOT"),
                                  ("graph_snapshots", "GRAPH"), ("import_jobs", "JOB")):
                identities.update((row[0], f"<{marker}>") for row in db.execute(f"SELECT id FROM {table}"))
            if details.get("model_run_id"):
                identities[details["model_run_id"]] = "<MODEL_RUN>"

        def normalized(value):
            if isinstance(value, dict):
                return {key: normalized(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalized(item) for item in value]
            if isinstance(value, str):
                for identity, marker in identities.items():
                    value = value.replace(identity, marker)
            return value

        excluded = {"id", "created_at", "updated_at"}
        if scoring_only:
            # Explanatory metadata was added without changing the v2 procedure.
            # This explicitly weaker mode NEVER claims full published-field parity.
            excluded |= {"explanations", "feature_vector_hash"}
        encoded = []
        for row in db.execute("SELECT * FROM findings WHERE rule_version='anomaly-stack-v2' ORDER BY entity_ref,rule_id"):
            if normalize_run_identities:
                original = json.loads(row["feature_vector"])
                original_hash = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if original_hash != row["feature_vector_hash"] or original.get("model_run_id") != details.get("model_run_id"):
                    raise ValueError("Stored feature hash or model-run linkage is invalid")
            document = {}
            for key, value in dict(row).items():
                if key in excluded:
                    continue
                if key in {"feature_vector", "coverage", "source_refs", "explanations", "benign_alternatives", "opposing_evidence"}:
                    value = json.loads(value)
                if scoring_only and key == "feature_vector":
                    value.pop("local_if_sensitivity", None)
                if scoring_only and key == "coverage":
                    # Historical wording was corrected to acknowledge D's
                    # retrospective bucket scope; numerical coverage is kept.
                    value.pop("notes", None)
                document[key] = value
            document = normalized(document)
            if normalize_run_identities and "feature_vector_hash" in document:
                # Original hashes were independently verified above. Their
                # random snapshot/model IDs differ across fresh HTTP imports.
                document["feature_vector_hash"] = hashlib.sha256(json.dumps(
                    document["feature_vector"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            encoded.append(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False))
        result = {"findings": len(encoded), "sha256": hashlib.sha256("\n".join(encoded).encode()).hexdigest(),
                  "scored_transactions": details.get("scored_transactions"), "release_id": details.get("release_id")}
        if not scoring_only:
            pins = [json.dumps(normalized([row[0], row[1], json.loads(row[2])]), sort_keys=True, separators=(",", ":"), allow_nan=False)
                    for row in db.execute("SELECT f.entity_ref,f.rule_id,c.provenance FROM findings f "
                                          "JOIN finding_confidence c ON c.finding_id=f.id "
                                          "WHERE f.rule_version='anomaly-stack-v2' ORDER BY f.entity_ref,f.rule_id")]
            result["confidence_pins"] = {"count": len(pins), "sha256": hashlib.sha256("\n".join(pins).encode()).hexdigest()}
            if len(pins) != len(encoded):
                raise ValueError("Not every published ML finding has pinned confidence provenance")
        return result
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--reference-report", type=Path, help="Compare one new run against a saved passing full-field parity report")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scoring-only", action="store_true", help="Compare scoring/evidence, excluding newly added explanation metadata")
    parser.add_argument("--normalize-run-identities", action="store_true", help="Fresh same-source imports: normalize case/source/snapshot/graph/model-run IDs, independently verifying original feature hashes")
    args = parser.parse_args()
    if len(args.runs) != (1 if args.reference_report else 2):
        parser.error("Expected one run with --reference-report, otherwise two runs")
    with args.output.open("x") as handle:
        report = {"pass": False, "runs": [str(path) for path in args.runs],
                  "scope": "scoring/evidence only; NOT full published-field parity" if args.scoring_only else "full published-field parity",
                  "run_identity_normalization": args.normalize_run_identities}
        try:
            summaries = []
            if args.reference_report:
                frozen = json.loads(args.reference_report.read_text())
                if (not frozen.get("pass") or not frozen.get("reports") or
                        frozen["scope"] != report["scope"] or
                        frozen.get("run_identity_normalization", False) != args.normalize_run_identities or
                        any(r != frozen["reports"][0] for r in frozen["reports"])):
                    raise ValueError("Reference must have passing parity with the same comparison scope")
                summaries.append(frozen["reports"][0])
                report["frozen_reference_report"] = {"path": str(args.reference_report),
                    "sha256": hashlib.sha256(args.reference_report.read_bytes()).hexdigest()}
            summaries.extend(fingerprint(path, scoring_only=args.scoring_only,
                                         normalize_run_identities=args.normalize_run_identities) for path in args.runs)
            report["reports"] = summaries
            report["pass"] = report["reports"][0] == report["reports"][1]
        except Exception as error:  # noqa: BLE001 - retain verification failure
            report["error"] = f"{type(error).__name__}: {error}"
        json.dump(report, handle, indent=2)
    print(json.dumps(report))
    return int(not report["pass"])


if __name__ == "__main__":
    raise SystemExit(main())
