"""Metadata-only minimum-count admission: never generates or processes records.

Uses the SAME conservative disk gate as the Docker orchestrator. A failed lower
bound proves that workers/budget changes cannot admit this run on this volume.
An admitted lower bound does not replace the actual all-stage container preflight.
"""
import argparse
import json
import shutil
from pathlib import Path

from app.resources import global_stage_estimates
from app.telemetry import host_metrics


def inspect(root, minimum):
    estimates=global_stage_estimates({"transactions":minimum,"inputs":0,"outputs":0})
    floor=estimates["scratch_bytes"]*2+(8<<30)
    free=shutil.disk_usage(root).free
    return {"status":"BLOCKED" if free<floor else "LOWER_BOUND_ONLY_ACTUAL_PREFLIGHT_REQUIRED",
            "large_run":"NOT RUN", "minimum_canonical_transactions":minimum,
            "disk_floor_bytes":floor,"disk_free_bytes":free,"disk_deficit_bytes":max(0,floor-free),
            "host":host_metrics(),"resource_admission":{"method":"Existing orchestrator source/scratch/WAL/reserve gate, at zero source/input/output bytes (strict lower bound)",
                "allocations":"None; metadata only", "worker_configurations":"All worker/thread choices have this same disk floor; no stage may be skipped",
                "limitations":"Conservative supported admission, not measured actual minimum disk usage. Other volumes must be checked separately."},
            "errors":["Insufficient free SSD space even at the predeclared 1M gate's lower bound; generation/upload safely withheld. No existing data deleted."] if free<floor else [],
            "target_seconds":1800,"total_seconds":None,"canonical_transactions":None,
            "diagnostic_timeout_seconds":7200,"scope":"No fresh 1M processing result or timing/AP acceptance claimed"}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,required=True)
    parser.add_argument("--minimum",type=int,default=1000000)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    if args.minimum<1:
        parser.error("minimum must be positive")
    report=inspect(args.root,args.minimum)
    with args.output.open("x") as stream:
        json.dump(report,stream,indent=2)
    print(json.dumps({k:report[k] for k in ("status","disk_floor_bytes","disk_free_bytes","disk_deficit_bytes")}))
    return 2 if report["status"]=="BLOCKED" else 0


if __name__=="__main__":
    raise SystemExit(main())
