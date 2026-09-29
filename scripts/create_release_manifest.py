#!/usr/bin/env python3
"""Generate `experiments/releases/anomaly-stack-v1.json` from code, not by hand.

The scoring release has no static model artifact -- `A_global`/`D_burst` are
refit on each snapshot's own reference period at scoring time -- so there is no
`.joblib` file to hash and version. What this script produces instead is a
**scoring release manifest**: a committed, reviewable record of the exact frozen
procedure (`app.ml.findings.release_identity()`), the fixture identity it was
measured against, the dependency/interpreter constraints it requires, the
already-generated final-holdout report this release is backed by, and the git
commit the manifest was cut at.

Final-holdout evidence is *read*, never recomputed: Phase 5B's holdout has
already been consumed once and must stay that way. This script never invokes
`scripts/run_anomaly_stack.py` itself -- `--holdout-run` must point at a JSON
file `make anomaly-stack-holdout` already wrote (default:
`experiments/runs/anomaly_stack_holdout.json`, gitignored). It reads that file,
checks every task's non-baseline winner still matches the frozen release layers
(`app.ml.findings.DEFAULT_LAYERS`), and records the file's own SHA-256 in the
manifest -- so the committed evidence stays traceable to one exact artifact
instead of a hand-transcribed copy of its numbers that can drift from it.
Validation numbers, by contrast, ARE read from a real `make anomaly-stack` run,
because re-running validation is exactly what that target is for.

    uv run --extra ml python scripts/create_release_manifest.py \\
        --holdout-run experiments/runs/anomaly_stack_holdout.json \\
        --validation-run experiments/runs/anomaly_stack_validation.json

Never pass a validation-split file as --holdout-run, or a final_holdout-split
file as --validation-run -- both are rejected below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# General, non-numeric caveats about the whole stack -- not run-specific evidence,
# so there's nothing here that could drift from a real run and nothing to hash.
KNOWN_LIMITATIONS = [
    "Synthetic-fixture evaluation only. This does not measure real-world detection accuracy.",
    "No ownership, origin, criminality, or IP-to-wallet attribution claim is made anywhere in this stack.",
    ("Unsupervised scoring cannot reliably separate a structurally identical benign payroll run from a "
     "real CoinJoin (see the holdout report's discrimination task: rule-only wins). Analyst-labelled "
     "reviews are required before any supervised model may score production cases; see "
     "app.ml.findings.review_decision_labels."),
]


def _git_commit() -> dict[str, object]:
    """Capture the exact commit this manifest was cut at, and whether the tree was dirty.

    A release manifest that cannot say which commit produced it is not audit
    evidence. `git rev-parse` is used rather than reading `.git/HEAD` by hand so
    this works identically in a normal checkout, a worktree, or a CI clone.
    """
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
        ).stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
        dirty = run("status", "--porcelain") != ""
        branch = run("rev-parse", "--abbrev-ref", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        return {"commit": None, "dirty": None, "branch": None, "error": str(error)}
    return {"commit": commit, "dirty": dirty, "branch": branch}


def _fixture_identity() -> dict[str, object]:
    """Dataset identity from the generated manifest, if the fixture exists locally."""
    manifest_path = REPO / "datasets" / "phase5a_100k" / "fixture_manifest.json"
    if not manifest_path.is_file():
        return {
            "available": False,
            "note": "no fixture generated in this environment; run `make dataset` "
            "and regenerate this manifest to record its identity",
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "available": True,
        "generator_version": manifest["generator_version"],
        "generator_sha256": manifest["generator_sha256"],
        "config_sha256": manifest["config_sha256"],
        "seed": manifest["seed"],
        "network": manifest["network"],
        "canonical_transaction_count": manifest["counts"]["canonical_transaction_count"],
    }


def _dependency_constraints() -> dict[str, object]:
    """Static constraints from pyproject.toml, plus installed versions if importable."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    requires_python = next(
        (line.split("=", 1)[1].strip().strip('"') for line in pyproject.splitlines()
         if line.strip().startswith("requires-python")),
        None,
    )
    ml_line = next((line for line in pyproject.splitlines() if line.strip().startswith("ml = [")), "")
    installed: dict[str, str | None] = {}
    for module_name in ("numpy", "scipy", "sklearn"):
        try:
            installed[module_name] = __import__(module_name).__version__
        except ImportError:
            installed[module_name] = None
    return {
        "requires_python": requires_python,
        "ml_extra_specifiers": ml_line.strip(),
        "installed_versions_at_manifest_creation": installed,
        "interpreter_at_manifest_creation": platform.python_version(),
    }


def _holdout_evidence(run_path: Path, *, expected_layers: tuple[str, ...]) -> dict[str, object]:
    """Read (never recompute) the already-generated final-holdout report.

    Phase 5B's holdout has already been consumed once and must stay that way (see
    the module docstring), so this only reads the JSON `make anomaly-stack-holdout`
    already wrote: it checks every task's non-baseline winner still matches the
    frozen release layers, and hashes the file so the manifest is traceable to the
    exact artifact instead of a hand-transcribed copy of its numbers.
    """
    if not run_path.is_file():
        raise SystemExit(
            f"no holdout report at {run_path}. This script reads an already-generated report; "
            "it does not rerun the holdout. Generate it once with:\n  make anomaly-stack-holdout"
        )
    raw = run_path.read_bytes()
    payload = json.loads(raw)
    if payload.get("split") != "final_holdout":
        raise SystemExit(
            f"{run_path} has split={payload.get('split')!r}, not 'final_holdout'. Pass the report "
            "`make anomaly-stack-holdout` wrote as --holdout-run, not a validation run."
        )

    expected_candidate = "+".join(expected_layers)
    winners = payload.get("winners", {})
    for task, winner in winners.items():
        candidate = winner.get("candidate")
        if candidate not in ("rule_baseline", expected_candidate):
            raise SystemExit(
                f"{run_path} task {task!r} winner is {candidate!r}, not {expected_candidate!r} or "
                "'rule_baseline'. The frozen release decision (app.ml.findings.DEFAULT_LAYERS) no "
                "longer matches what this holdout report measured -- resolve that as a human "
                "decision (experiments/model_decision.md), not by editing this check."
            )

    return {
        "source_file": str(run_path.relative_to(REPO)) if run_path.is_relative_to(REPO) else str(run_path),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "split": payload["split"],
        "budget": payload.get("budget"),
        "deployable_configuration": payload.get("deployable_configuration"),
        "peak_rss_mb": payload.get("peak_rss_mb"),
        "label_counts": payload.get("label_counts"),
        "winners_by_task": winners,
        "run_limitations": payload.get("limitations"),
    }


def _validation_metrics(run_path: Path | None) -> dict[str, object]:
    """Validation-split metrics read from a real `make anomaly-stack` output.

    Unlike the holdout, re-running validation is exactly what that target is
    for, so these numbers are read live rather than hardcoded -- but only ever
    from `--split validation` output; final_holdout output is rejected below.
    """
    if run_path is None or not run_path.is_file():
        return {
            "available": False,
            "note": "no validation run JSON supplied; run `make anomaly-stack` and pass "
            "--validation-run experiments/runs/anomaly_stack_validation.json",
        }
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    if payload.get("split") == "final_holdout":
        raise SystemExit(
            f"{run_path} is a final_holdout run. This script only accepts validation-split "
            "evidence here; pass a final_holdout run as --holdout-run instead."
        )
    winners = payload.get("winners", {})
    return {
        "available": True,
        "source_file": str(run_path.relative_to(REPO)) if run_path.is_relative_to(REPO) else str(run_path),
        "split": payload.get("split"),
        "budget": payload.get("budget"),
        "deployable_configuration": payload.get("deployable_configuration"),
        "winners_by_task": winners,
    }


def build_manifest(*, validation_run: Path | None, holdout_run: Path) -> dict[str, object]:
    from app.ml.findings import DEFAULT_BUDGET, DEFAULT_LAYERS, RELEASE_ID, release_identity, release_manifest_sha256

    identity = release_identity()
    assert identity["release_id"] == RELEASE_ID
    assert tuple(identity["layers"]) == DEFAULT_LAYERS == ("A_global", "D_burst"), (
        "the frozen Phase 5B decision has changed in code; this script must not be used to "
        "silently document a different combination than the one that was actually selected"
    )
    assert identity["default_review_budget"] == DEFAULT_BUDGET == 0.01

    return {
        "$schema": "release-manifest-v1",
        "release_id": RELEASE_ID,
        "release_manifest_sha256": release_manifest_sha256(),
        "frozen_scoring_procedure": identity,
        "git": _git_commit(),
        "fixture_identity": _fixture_identity(),
        "dependency_constraints": _dependency_constraints(),
        "frozen_holdout_evidence": _holdout_evidence(holdout_run, expected_layers=DEFAULT_LAYERS),
        "validation_evidence": _validation_metrics(validation_run),
        "model_run_id_strategy": (
            "app.ml.findings.model_run_id(snapshot_id, budget) -- sha256(release_id + "
            "release_manifest_sha256[:16] + snapshot_id + budget)[:24]. Deterministic: the same "
            "snapshot scored again under an unchanged release reproduces the same ID. It changes "
            "automatically if release_identity() changes, which is what makes it a safe substitute "
            "for a versioned static-artifact hash when no static artifact exists."
        ),
        "fallback_behaviour": {
            "TRACEX_ML_FINDINGS=0": "ml_status=disabled; ingestion, graph, deterministic findings, "
            "exports and review all proceed unaffected.",
            "ml extra not installed": "ml_status=unavailable; import still completes.",
            "scoring raises": "ml_status=error:<ExceptionType>; import still completes.",
            "TRACEX_ML_REVIEW_BUDGET outside (0, 0.5]": "ml_status=invalid_budget; import still "
            "completes; a non-numeric value falls back to the 0.01 default with a logged warning "
            "rather than crashing application startup.",
            "successful score, nothing clears the budget": "ml_status=no_rows_flagged.",
            "successful score": "ml_status=written; ml_finding_count>0; ml_model_run_id set.",
        },
        "known_limitations": KNOWN_LIMITATIONS,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--holdout-run", type=Path, default=REPO / "experiments" / "runs" / "anomaly_stack_holdout.json",
        help="path to the already-generated `make anomaly-stack-holdout` (final_holdout split) output "
        "JSON; read and hashed, never regenerated by this script",
    )
    parser.add_argument(
        "--validation-run", type=Path, default=REPO / "experiments" / "runs" / "anomaly_stack_validation.json",
        help="path to a `make anomaly-stack` (validation split) output JSON",
    )
    parser.add_argument(
        "--output", type=Path, default=REPO / "experiments" / "releases" / "anomaly-stack-v1.json",
    )
    args = parser.parse_args()

    manifest = build_manifest(
        validation_run=args.validation_run if args.validation_run.is_file() else None,
        holdout_run=args.holdout_run,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    print(f"release_manifest_sha256: {manifest['release_manifest_sha256']}")
    print(f"git commit: {manifest['git'].get('commit')} (dirty={manifest['git'].get('dirty')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
