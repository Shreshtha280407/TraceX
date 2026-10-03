"""Small native spill-isolation diagnostic, not a pipeline scale acceptance run.

No core dumps are generated. The shared-directory control intentionally recreates
the former configuration; each variant is reported, including failures.
"""
from __future__ import annotations

import argparse
import json
import resource
import shutil
import time
from pathlib import Path

import duckdb

from app.engine.process_pool import ProcessPool
from app.resources import admit_disk_allocation


def probe(job):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    spill = Path(job["spill"])
    spill.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(config={"threads": 1, "memory_limit": "64MB", "temp_directory": str(spill),
                                 "preserve_insertion_order": False})
    try:
        database = "'" + job["database"].replace("'", "''") + "'"
        con.execute(f"ATTACH {database} AS reference (READ_ONLY)")
        result = con.execute("SELECT sum(length(a.payload) + length(b.payload)) FROM reference.data a "
                             "JOIN reference.data b ON a.key=b.key").fetchone()[0]
        return {"sum": int(result), "expected": job["expected"], "pass": result == job["expected"]}
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"diagnostic_only": True, "variants": [], "rows": 300000, "memory_limit_mb_per_child": 64,
              "disk_initial": shutil.disk_usage(output)._asdict()}
    started = time.monotonic()
    try:
        admit_disk_allocation("spill diagnostic", output, 500 << 20)
        path = output / "input.duckdb"
        con = duckdb.connect(str(path), config={"threads": 1, "memory_limit": "128MB"})
        try:
            con.execute("CREATE TABLE data AS SELECT i % 100000 AS key, repeat(md5(i::VARCHAR), 16) AS payload "
                        "FROM range(300000) t(i)")
        finally:
            con.close()
        expected = 300000 * 3 * 1024
        for isolated in (False, True):
            label = "independent-spill" if isolated else "shared-spill-control"
            jobs = [{"database": str(path), "spill": str(output / label / (str(i) if isolated else "shared")),
                     "expected": expected} for i in range(2)]
            result = {"name": label, "pass": False}
            try:
                with ProcessPool(2) as pool:
                    result["results"] = list(pool.imap("scripts.spill_isolation_probe:probe", jobs))
                result["pass"] = all(item["pass"] for item in result["results"])
            except Exception as error:  # noqa: BLE001 - the control may crash/fail
                result["error"] = f"{type(error).__name__}: {error}"
            report["variants"].append(result)
    except Exception as error:  # noqa: BLE001 - persist diagnostics
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["seconds"] = time.monotonic() - started
        with (output / "result.json").open("x") as handle:
            json.dump(report, handle, indent=2)
        print(json.dumps(report))
    return int(not report["variants"] or not report["variants"][-1]["pass"])


if __name__ == "__main__":
    raise SystemExit(main())
