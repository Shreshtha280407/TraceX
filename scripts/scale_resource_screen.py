"""Preserve honest admission decisions; a screen is NOT an end-to-end run."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.resources import current_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads((args.reference_run / "result.json").read_text())
    count = reference["rows_accepted"]
    retained = sum(p.stat().st_size for p in args.reference_run.rglob("*") if p.is_file())
    plan = current_plan()
    disk = shutil.disk_usage(args.reference_run)
    reserve = 2 * 1024 ** 3
    estimates = []
    for rows in (100_000, 300_000, 1_000_000, 3_000_000):
        # Includes generated canonical inputs and retained evidence plus 25%
        # scratch headroom. Growth/skew can make the true cost larger.
        evidence = int(retained / count * rows * 1.25)
        generated = int(reference["source_bytes"] / count * rows * 2)
        ml = int(rows * (1600 + 384 * 2.62338 + 80 * 1.46484))
        reasons = []
        if evidence + generated + reserve > disk.free:
            reasons.append("insufficient free disk for generator + retained evidence + scratch + 2 GiB reserve")
        if ml > plan.memory_budget_bytes:
            reasons.append("ML global allocation exceeds admitted memory budget")
        estimates.append({"rows": rows, "status": "BLOCKED" if reasons else "ADMITTED_NOT_RUN",
            "reason": reasons, "projected_evidence_scratch_bytes": evidence, "projected_generator_bytes": generated,
            "projected_ml_global_bytes": ml, "timing_measured": False})
    worker_screens = []
    for workers in (1, 2, 4, 6, 8):
        supported = plan.supported_workers()
        worker_screens.append({"workers": workers, "status": "ADMITTED_NOT_RUN" if workers <= supported else "BLOCKED",
                               "supported_under_joint_budget": supported, "speedup_measured": False})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump({"scope": "resource admission screening only; not generator/pipeline completion",
                   "reference_run": str(args.reference_run), "reference_status": reference["status"],
                   "free_disk_bytes": disk.free, "resource_plan": plan.as_dict(), "scales": estimates,
                   "workers": worker_screens}, handle, indent=2)
    print(str(args.output))
    return int(any(r["status"] == "BLOCKED" for r in estimates))


if __name__ == "__main__":
    raise SystemExit(main())
