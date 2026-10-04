"""Export the measured grouped-review decision, never runs or estimates a workload.

Inputs are existing compact results. Output is exclusive and sanitized; weights,
truth rows, case evidence, session credentials and private exports are not copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from app.engine.investigations import POLICY, PROCEDURE_SHA256, QUEUE_POLICY, VERSION
from app.ml.promotion import assess
from scripts.export_review_package import main as export
from scripts.export_review_package import sanitize


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--small-acceptance", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--screen", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, help="Actual container/PG metadata-only large admission report")
    parser.add_argument("--frontend-summary", type=Path, help="Actually executed frontend check summary, not predicted results")
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    selection = read(args.study / "selection-decision.json")
    if selection["grouping_sha256"] != PROCEDURE_SHA256:
        raise ValueError("Grouping changed after this study; register new finals, never reinterpret consumed labels")
    if selection["selected_scorer"] != "anomaly-stack-v2" or selection["final_labels_opened_for_selection"]:
        raise ValueError("This report entry point requires the pre-final conservative v2 selection")
    wrapper = read(args.small_acceptance)
    actual_path = Path(wrapper["result"])
    actual, admission, browser = read(actual_path), read(args.admission), read(args.browser)
    if actual["status"] != "pass" or actual["final_group_retrieval"]["grouping_sha256"] != PROCEDURE_SHA256 or browser["status"] != "pass":
        raise ValueError("Do not report small functional acceptance without a passing matched worker/browser result")
    manifest_path = args.study / "comparison/hybrid/manifest.json"
    manifest = read(manifest_path)
    finals = [args.study / f"final-{variant}-transfer.json" for variant in ("base", "shift", "coverage")]
    frozen = read(args.study / "protocol.json.frozen.json")
    gate = assess(manifest, [read(path) for path in finals], frozen=frozen)
    if gate["status"] != "BLOCKED":
        raise ValueError("A passing candidate requires an explicit independent owner applicability decision, not this v2 report shortcut")
    tests = [{key: suite.attrib.get(key) for key in ("name", "tests", "failures", "errors", "skipped", "time")}
             for suite in ET.parse(args.junit).getroot().iter("testsuite")]
    if any(int(suite["failures"] or 0) or int(suite["errors"] or 0) for suite in tests):
        raise ValueError("Latest test report still has failures; retain its diagnostics and fix before release reporting")
    stages = {row["name"]: {key: row.get(key) for key in ("status", "duration_seconds", "reason")}
              for row in actual["job"]["analysis"]["stages"]}
    release = {
        "schema": "grouped-deployment-policy-v1", "selected_scorer": "anomaly-stack-v2",
        "release_decision": "FINALIZED_V2_NO_CANDIDATE_PROMOTED", "selection_reason": selection["selection_reason"],
        "domain_policy": {"real_or_unknown": "unsupervised v2; no approved candidate is shipped",
                          "synthetic_demo": "explicit opt-in only; frozen hybrid is a diagnostic artifact, not the chosen deployment",
                          "future_domain_promotion": "exact trusted artifact, frozen protocol/source/truth/queue/grouping provenance, measured full-population gates AND owner representative-label approval"},
        "fallback": "v2 on missing/ineligible/incompatible candidate; retain durable failure/applicability reason",
        "procedure": "v2 refits label-free on each snapshot's earliest reference period; retrospective burst; CPU path, not GPU accelerated",
        "model": actual["container_runtime"]["release_identity"],
        "manifest_sha256": actual["container_runtime"]["release_manifest_sha256"],
        "grouping_version": VERSION, "grouping_sha256": PROCEDURE_SHA256, "protocol_sha256": selection["protocol_sha256"],
        "group_queue_policy": QUEUE_POLICY, "group_capacity": 100, "group_policy": POLICY,
        "quality": {"candidate_promotion": gate, "transaction_metric_scope": "499 labelled of 5000 eligible per final; not product-queue precision or arbitrary-domain accuracy",
                    "group_p_at_100": None, "group_status": "NOT EVALUABLE", "merchant_false_positive_reduction": "NOT DEMONSTRATED",
                    "calibration": "synthetic task frequency only; v2 confidence is applicability-pinned, not criminality probability"},
        "benchmark": {"fresh_1m": admission, "small": {"status": actual["status"], "canonical_counts": actual["inspection"]["canonical_counts"],
                    "total_seconds": actual["total_seconds"], "upload_seconds": actual["upload_seconds"], "stages": stages,
                    "groups": actual["final_group_retrieval"], "resource_peaks": actual.get("resource_peaks"),
                    "scope": "128-TX functional check only; not large-scale parity or 1M acceptance"}},
        "test_summary": tests, "browser_summary": {"status": browser["status"], "checks_passed": sum(c["ok"] for c in browser["checks"]),
                    "external_requests": len(browser["externalRequests"]), "javascript_errors": len(browser["consoleErrors"]), "scope": "small fixture only"},
        "limitations": ["No fresh 1M run: every configuration fails the supported disk floor; the screened 3500 MiB memory budget also rejects estimated large counts. Other memory budgets were not certified; no 3M run",
                    "No candidate satisfies both task objectives with nonregression/full eligible-population and applicable group truth",
                    "Final variants are correlated derivatives, not three independent populations; shifted surge labels require applicability review",
                    "Finals were consumed once; any later selection needs new registered finals",
                    "Existing reports predate final promotion/capacity hardening; their unpinned capacity provenance cannot be retroactively repaired into passing evidence",
                    "No independently representative real-case labels, address/entity-disjoint generalization evidence or official PS certification"],
        "reproduction": ["See docs/laptop_1m_runbook.md. Stop on rejected admission; no force override or data deletion.",
                    "Review git status/diff and compact evidence before owner commit/push; no agent commit/push."],
    }
    # Export actual underlying compact results and measured stage/metric tables.
    inputs = [actual_path, args.admission, args.study / "comparison/comparison.json", *finals,
              args.study / "quality-matrix.json", args.study / "protocol.json", args.study / "protocol.json.frozen.json", args.screen, args.browser]
    if args.preflight:
        inputs.append(args.preflight)
    if args.frontend_summary:
        inputs.append(args.frontend_summary)
    flags = [flag for path in inputs for flag in ("--result", str(path))]
    export([*flags, "--junit", str(args.junit), "--output", str(args.output)])
    (args.output / "release-decision.json").write_text(json.dumps(sanitize(release), indent=2, sort_keys=True))
    (args.output / "source-map.json").write_text(json.dumps({f"result-{i:02d}.json": {"purpose": str(p.relative_to(p.parents[1])) if p.is_relative_to(args.study) else p.name,
                "original_sha256": digest(p)} for i, p in enumerate(inputs)}, indent=2))
    group_quality = {
        "schema": "group-quality-status-v1", "status": "NOT EVALUABLE",
        "grouping_version": VERSION, "grouping_sha256": PROCEDURE_SHA256,
        "group_queue_policy": QUEUE_POLICY, "review_capacity": 100,
        "target": "Review-worthiness of the stated bounded, observed pattern episode; not wrongdoing and not any-positive-transaction membership",
        "precision_at_100": None, "recall_at_review_capacity": None,
        "mixed_groups": None, "overmerged_groups": None, "episode_fragmentation": None,
        "reasons": [
            "No independently declared applicable group-proposition truth was available for these registered final populations.",
            "Only 499 of 5000 eligible transactions have task labels per final; unknown labels cannot become negatives or group truth.",
            "The actual 128-TX worker case verifies grouping functionality, not a group-quality final population."
        ],
        "functional_case_only": actual["final_group_retrieval"],
        "not_claimed": ["P@100 pass", "benign merchant false-positive reduction", "episode-quality improvement", "arbitrary-domain accuracy"]
    }
    (args.output / "group-quality-status.json").write_text(json.dumps(sanitize(group_quality), indent=2, sort_keys=True))
    (args.output / "README.md").write_text(
        "# Final grouped-review evidence — 2026-10-04\n\n"
        "Selected deployment: **anomaly-stack-v2**; no candidate promoted. "
        "Fresh 1M: **BLOCKED / NOT RUN**. No 3M run or large-runtime estimate.\n\n"
        "- `release-decision.json`: versioned domain/fallback/grouping/queue policy, actual small-stage results and eligibility reasons.\n"
        "- `group-quality-status.json`: unavailable applicable group truth, null precision/recall, no inflated any-positive group metric.\n"
        "- `result-00.json`: final native PostgreSQL/API/worker **128-TX** fresh acceptance, counts, source/code hashes, timings and resource samples.\n"
        "- `result-01.json`: actual host resource detection and rejected 1M conservative disk floor, **metadata only**.\n"
        "- `result-02.json`: bounded HGB/XGBoost/LightGBM/hybrid validation comparison.\n"
        "- `result-03.json` through `result-05.json`: frozen diagnostic final transfer metrics; correlated variants, not independent real populations.\n"
        "- `result-06.json`: INCOMPLETE quality matrix with dataset-specific missing-metric reasons.\n"
        "- `result-07.json` and `result-08.json`: registered protocol and exact frozen source/model/feature/calibration identities.\n"
        "- `result-09.json`: small worker screening (not extrapolated throughput).\n"
        "- `result-10.json`: actual browser checks, including source replay and cross-case denial.\n"
        "- Additional optional results: actual container/PG 1M admission and frontend checks; identify via `source-map.json`.\n"
        "- `tests-00.json`: latest audited small-suite results. `inventory.json` verifies compact output hashes.\n\n"
        "Reproduction and owner-only Git commands: `docs/laptop_1m_runbook.md`. "
        "Readable results/limits: `docs/grouped_review_2026-10-04.md`. "
        "Report export reads measured summaries only; do not rerun consumed final evaluation for selection.\n\n"
        "Manually review before Git publication. Credentials, raw evidence, truth rows, private exports and fitted model binaries are excluded. "
        "The parent package is an earlier task-created generation, retained unchanged.\n"
    )
    inventory = read(args.output / "inventory.json")
    for filename in ("release-decision.json", "source-map.json", "group-quality-status.json", "README.md"):
        inventory.append({"file": filename, "sha256": digest(args.output / filename)})
    (args.output / "inventory.json").write_text(json.dumps(inventory, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
