"""Compare full canonical graph/features/deterministic outputs of two SQLite runs.

Ignores random run identifiers and Parquet/DuckDB container bytes, not evidence
locators, graph attributes, scores, ranks, feature values or source checksums.
Usage: uv run --extra ml python scripts/canonical_parity.py RUN_A RUN_B --output NEW.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import duckdb
import pyarrow.parquet as pq


def fingerprints(work):
    database = sqlite3.connect(f"file:{work / 'control.db'}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    identities = {}
    for table, marker in (("cases", "CASE"), ("evidence_sources", "SOURCE"), ("snapshots", "SNAPSHOT"),
                          ("graph_snapshots", "GRAPH"), ("import_jobs", "JOB")):
        for row in database.execute(f"SELECT id FROM {table}"):
            identities[row[0]] = marker

    def normalize(value):
        if isinstance(value, str):
            for original, marker in identities.items():
                value = value.replace(original, f"<{marker}>")
            return value
        if isinstance(value, dict):
            return {key: normalize(val) for key, val in value.items()}
        if isinstance(value, list | tuple):
            return [normalize(val) for val in value]
        return value.isoformat() if hasattr(value, "isoformat") else value

    def digest(rows):
        # Only fixed-size digests remain resident; no decoded graph/fact objects.
        hashes = [hashlib.sha256(json.dumps(normalize(row), sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).digest() for row in rows]
        return {"count": len(hashes), "sha256": hashlib.sha256(b"".join(sorted(hashes))).hexdigest()}

    results = {}
    graph_record = database.execute("SELECT * FROM graph_snapshots").fetchone()
    con = duckdb.connect(str(work / "evidence" / graph_record["storage_relative_path"]), read_only=True)

    def graph_rows(sql):
        cursor = con.cursor()
        try:
            cursor.execute(sql)
            while batch := cursor.fetchmany(5000):
                for row in batch:
                    yield [json.loads(item) if index == len(row) - 1 else item for index, item in enumerate(row)]
        finally:
            cursor.close()

    try:
        results["graph_nodes"] = digest(graph_rows("SELECT node_id,node_type,label,attributes_json FROM nodes"))
        # Edge IDs hash the run-scoped observation identifier; independently
        # verify them, then compare complete normalized semantics.
        def edge_rows():
            cursor = con.cursor()
            cursor.execute("SELECT edge_id,from_node,to_node,edge_type,uncertainty,attributes_json FROM edges")
            try:
                while batch := cursor.fetchmany(5000):
                    for identity, source, target, kind, uncertainty, attrs in batch:
                        assert identity == hashlib.sha256(f"{source}|{target}|{kind}".encode()).hexdigest()
                        yield [source, target, kind, uncertainty, json.loads(attrs)]
            finally:
                cursor.close()
        results["graph_edges"] = digest(edge_rows())
        results["graph_coverage"] = normalize(json.loads(graph_record["coverage"]))
    finally:
        con.close()

    def feature_rows():
        for record in database.execute("SELECT * FROM feature_stores"):
            for batch in pq.ParquetFile(work / "evidence" / record["storage_relative_path"]).iter_batches(batch_size=5000):
                for row in batch.to_pylist():
                    yield {key: json.loads(value) if key in {"feature_vector", "source_refs"} else value
                           for key, value in row.items() if key != "feature_row_id"}
    results["features"] = digest(feature_rows())
    def findings():
        for row in database.execute("SELECT entity_ref,window_start,window_end,rule_id,rule_version,raw_score,rank,"
                                    "feature_vector,source_refs,coverage FROM findings WHERE rule_version='deterministic-v1'"):
            yield [json.loads(value) if index >= 7 else value for index, value in enumerate(row)]
    results["deterministic_findings"] = digest(findings())
    results["receipts"] = {row[0]: row[1] for row in database.execute(
        "SELECT record_type,sum(record_count) FROM fragment_receipts GROUP BY record_type")}
    database.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--reference-report", type=Path, help="Compare one run against saved verified full canonical hashes after approved vault retirement")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if len(args.runs) != (1 if args.reference_report else 2):
        parser.error("Expected one run with --reference-report, otherwise two runs")
    reports, error = [], None
    reference = None
    try:
        if args.reference_report:
            frozen = json.loads(args.reference_report.read_text())
            if not frozen.get("pass") or not frozen.get("reports") or any(r != frozen["reports"][0] for r in frozen["reports"]):
                raise ValueError("Reference must be a passing verified canonical parity report")
            reports.append(frozen["reports"][0])
            reference = {"path": str(args.reference_report), "sha256": hashlib.sha256(args.reference_report.read_bytes()).hexdigest()}
        reports.extend(fingerprints(path) for path in args.runs)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump({"runs": [str(p) for p in args.runs], "reports": reports, "error": error,
                       "frozen_reference_report": reference,
                       "pass": len(reports) == 2 and reports[0] == reports[1]}, handle, indent=2)
    print(str(args.output))
    return int(reports[0] != reports[1])


if __name__ == "__main__":
    raise SystemExit(main())
