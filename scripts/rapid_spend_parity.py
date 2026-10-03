"""Full 1M changed-relation parity inside the isolated review appliance.

Read-only PostgreSQL metadata; verifies every existing receipted file, stages
owned temporary facts from a terminal synthetic benchmark, compares every raw
rapid-spend row against the original independent SQL, then removes only its
owned scratch. This is component correctness, NOT an end-to-end timing run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import make_engine
from app.engine.bounded import FactStore, _prepare_rapid_spends
from app.models import ImportJob, Snapshot
from scripts.disposable_appliance_cleanup import archive

REFERENCE_SQL = """
    SELECT i.seq, i.j, po.seq, po.j, po.address, ts.us, tr.us
    FROM inputs i
    JOIN outputs po ON po.txid = i.prev_txid AND po.vout = i.prev_vout
    JOIN tx_time ts ON ts.txid = i.txid
    JOIN tx_time tr ON tr.txid = i.prev_txid
    WHERE i.prev_txid IS NOT NULL AND i.prev_vout IS NOT NULL AND NULLIF(po.address, '') IS NOT NULL
      AND ts.us IS NOT NULL AND tr.us IS NOT NULL
      AND ts.us - tr.us BETWEEN 0 AND 3600000000
"""


def digest(rows):
    values = [hashlib.sha256(json.dumps(row, separators=(",", ":"), allow_nan=False).encode()).digest() for row in rows]
    return {"count": len(values), "sha256": hashlib.sha256(b"".join(sorted(values))).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if settings.evidence_root != Path("/data/evidence") or not settings.database_url.startswith("postgresql"):
        raise ValueError("Only the isolated copied review appliance is eligible")
    result = json.loads(args.result.read_text())
    if result.get("status") != "pass" or result.get("acceptance_errors"):
        raise ValueError("A complete passing baseline benchmark is required")
    report = {"status": "failed", "scope": "every changed rapid-spend row; NOT full pipeline throughput or entire 1M output parity",
              "baseline_result_sha256": hashlib.sha256(args.result.read_bytes()).hexdigest()}
    engine, store, started = make_engine(), None, time.monotonic()
    try:
        with Session(engine) as session:
            existing = archive(session, result, name=args.name, root=settings.evidence_root)
            report["receipt_hashes_verified"] = existing["receipt_hashes_verified"]
            report["baseline_source_sha256"] = result["source_sha256"]
            job = session.get(ImportJob, result["job_id"])
            snapshot = session.get(Snapshot, job.snapshot_id)
            store = FactStore.build(session, evidence_root=settings.evidence_root, snapshot=snapshot)
            partitions = store.plan.partitions(records=store.record_count) * store.plan.worker_processes(records=store.record_count)
            report.update(transactions=store.record_count, partitions=partitions, resource_plan=store.plan.as_dict())
            _prepare_rapid_spends(store, partitions=partitions)
            # This verification has no pool; the original global relation needs
            # the same serial half-budget as cache construction, not child RAM.
            store.con.execute(f"SET memory_limit = '{store.plan.duckdb_memory_limit_mb}MB'")
            store.con.execute("SET threads = 4")
            reference = digest(store.rows(REFERENCE_SQL))
            cached = digest(store.rows("SELECT seq,input_raw,previous_seq,previous_raw,address,spent_us,received_us FROM rapid_spends"))
            unique = store.con.execute("SELECT count(DISTINCT seq) FROM rapid_spends").fetchone()[0]
            bad_bucket = store.con.execute(f"SELECT count(*) FROM rapid_spends WHERE bucket != hash(address)%{partitions}").fetchone()[0]
            report.update(reference=reference, cached=cached, unique_input_sequences=unique, wrong_partition_rows=bad_bucket)
            # Unique sequence + identical complete relation + unchanged ORDER BY
            # seq proves the same event order within every hash partition.
            report["status"] = "pass" if reference == cached and unique == cached["count"] and bad_bucket == 0 else "failed"
    except Exception as error:  # noqa: BLE001 - preserve every verification failure
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if store is not None:
            store.close()
        engine.dispose()
        report["diagnostic_seconds"] = time.monotonic() - started
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
        print(json.dumps(report))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
