"""Phase 6 bounded graph query latency benchmark.

Builds one real, persistent case (via the same HTTP + worker pipeline every
other Phase 6 script uses -- no shortcuts into internals) and then issues at
least 200 warm `GET /v1/cases/{id}/graph` calls against it: a mix of random
address/transaction seeds and the graph's actual highest-degree node (found
by querying the built DuckDB graph directly, not guessed), at both a single
reader and four concurrent readers. Every query goes through the real FastAPI
route, including case-membership authorization and JSON serialization, so the
reported numbers are what a client actually experiences, not a bare function
call.

DuckDB's default configuration allows multiple read-only connections against
the same file concurrently (`query_neighbourhood` opens `read_only=True`
per call), so the 4-reader run exercises that, not a lock queue -- and the
benchmark reports if it saw a lock/exception instead of assuming.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.main import app
from workers import runner

REPO = Path(__file__).resolve().parents[1]


def _ingest_fixture(source_path: Path, *, persist_root: Path) -> dict:
    """Ingest `source_path` into a persistent (not auto-deleted) evidence root/DB."""
    payload = source_path.read_bytes()
    engine = make_engine(f"sqlite:///{persist_root / 'control.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{persist_root / 'control.db'}",
        evidence_root=persist_root / "evidence",
        max_upload_bytes=len(payload) + 1,
        lease_seconds=600,
        event_heartbeat_seconds=5,
        ml_findings_enabled=False,
    )

    def override_session():
        with sessions() as session:
            yield session

    old_route_settings, old_worker_settings, old_sessions = routes.settings, runner.settings, runner.SessionLocal
    app.dependency_overrides[get_session] = override_session
    routes.settings, runner.settings, runner.SessionLocal = settings, settings, sessions
    client = TestClient(app)
    headers = {"X-TraceX-Actor": "phase6-query-latency"}
    try:
        case_id = client.post("/v1/cases", headers=headers, json={"name": "Phase 6 query latency", "synthetic": True}).json()["case_id"]
        upload = client.post(
            f"/v1/cases/{case_id}/imports",
            headers={**headers, "Idempotency-Key": "phase6-query-latency"},
            files={"file": (source_path.name, payload, "application/x-ndjson")},
        )
        upload.raise_for_status()
        job_id = upload.json()["job_id"]
        if not runner.process_one("phase6-query-latency-worker"):
            raise RuntimeError("worker did not process the query-latency fixture")
        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        if job["state"] != "completed":
            raise RuntimeError(f"fixture ingestion failed: {job}")
        with sessions() as session:
            from app.models import GraphSnapshot

            graph = session.query(GraphSnapshot).filter_by(case_id=case_id).one()
            graph_path = settings.evidence_root / graph.storage_relative_path
            node_count, edge_count = graph.node_count, graph.edge_count
    finally:
        app.dependency_overrides.clear()
        routes.settings, runner.settings, runner.SessionLocal = old_route_settings, old_worker_settings, old_sessions
    return {
        "case_id": case_id,
        "headers": headers,
        "settings": settings,
        "sessions": sessions,
        "graph_path": graph_path,
        "node_count": node_count,
        "edge_count": edge_count,
    }


def _pick_seeds(graph_path: Path, *, random_count: int, seed_value: int = 20260929) -> dict:
    import random

    connection = duckdb.connect(str(graph_path), read_only=True)
    try:
        degree_rows = connection.execute(
            """
            WITH deg AS (
                SELECT from_node AS node_id FROM edges
                UNION ALL
                SELECT to_node AS node_id FROM edges
            )
            SELECT node_id, COUNT(*) AS degree FROM deg GROUP BY node_id ORDER BY degree DESC LIMIT 5
            """
        ).fetchall()
        all_nodes = [row[0] for row in connection.execute("SELECT node_id FROM nodes").fetchall()]
    finally:
        connection.close()
    if not degree_rows:
        raise RuntimeError("graph has no edges to find a high-degree node from")
    rng = random.Random(seed_value)
    random_seeds = [rng.choice(all_nodes) for _ in range(random_count)]
    return {
        "high_degree_node": degree_rows[0][0],
        "high_degree_value": degree_rows[0][1],
        "top_degree_nodes": degree_rows,
        "random_seeds": random_seeds,
        "total_nodes": len(all_nodes),
    }


def _build_query_plan(seeds: dict, *, queries_per_depth_on_hub: int) -> list[dict]:
    plan: list[dict] = []
    for depth in (1, 2, 3):
        for _ in range(queries_per_depth_on_hub):
            plan.append({"seed": seeds["high_degree_node"], "depth": depth, "node_limit": 200, "edge_limit": 500})
    for seed in seeds["random_seeds"]:
        plan.append({"seed": seed, "depth": 1, "node_limit": 200, "edge_limit": 500})
    return plan


def _run_plan(client: TestClient, *, case_id: str, headers: dict, plan: list[dict], workers: int) -> dict:
    def one(query: dict) -> dict:
        started = time.perf_counter()
        response = client.get(
            f"/v1/cases/{case_id}/graph",
            headers=headers,
            params={"seed": query["seed"], "depth": query["depth"], "node_limit": query["node_limit"], "edge_limit": query["edge_limit"]},
        )
        elapsed = time.perf_counter() - started
        ok = response.status_code == 200
        body = response.json() if ok else {}
        return {
            "elapsed_sec": elapsed,
            "status_code": response.status_code,
            "truncated_nodes": body.get("truncated_nodes", 0) if ok else None,
            "truncated_edges": body.get("truncated_edges", 0) if ok else None,
        }

    wall_started = time.perf_counter()
    if workers <= 1:
        results = [one(q) for q in plan]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, plan))
    wall_elapsed = time.perf_counter() - wall_started

    latencies = sorted(r["elapsed_sec"] for r in results)
    failures = [r for r in results if r["status_code"] != 200]
    truncated = [r for r in results if (r["truncated_nodes"] or 0) > 0 or (r["truncated_edges"] or 0) > 0]

    def percentile(data: list[float], p: float) -> float:
        if not data:
            return 0.0
        k = (len(data) - 1) * p
        f, c = int(k), min(int(k) + 1, len(data) - 1)
        return data[f] if f == c else data[f] + (data[c] - data[f]) * (k - f)

    return {
        "workers": workers,
        "query_count": len(plan),
        "wall_clock_sec": round(wall_elapsed, 3),
        "queries_per_sec": round(len(plan) / wall_elapsed, 1) if wall_elapsed else None,
        "p50_sec": round(percentile(latencies, 0.50), 5),
        "p95_sec": round(percentile(latencies, 0.95), 5),
        "p99_sec": round(percentile(latencies, 0.99), 5),
        "min_sec": round(latencies[0], 5) if latencies else None,
        "max_sec": round(latencies[-1], 5) if latencies else None,
        "mean_sec": round(statistics.mean(latencies), 5) if latencies else None,
        "failures": len(failures),
        "queries_with_truncation": len(truncated),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=REPO / "datasets" / "phase5a_100k" / "ingestion_rows.ndjson")
    parser.add_argument("--random-queries", type=int, default=150)
    parser.add_argument("--hub-queries-per-depth", type=int, default=20)
    parser.add_argument("--warmup-queries", type=int, default=10, help="queries run and discarded before timing (page cache warmup)")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"missing {args.source} -- run `make dataset` first or pass --source")

    # `/tmp` is tmpfs (RAM-backed) on this host -- a 100K-fixture graph plus
    # its control DB there competes with the ingestion process's own heap for
    # the same physical memory instead of using real disk. `var/` is ext4.
    work_root = REPO / "var" / "phase6-tmp"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="query-latency-", dir=work_root) as temp:
        persist_root = Path(temp)
        print(f"Ingesting {args.source} ...")
        ingest_started = time.perf_counter()
        context = _ingest_fixture(args.source, persist_root=persist_root)
        print(
            f"Ingested in {time.perf_counter() - ingest_started:.1f}s: "
            f"{context['node_count']} nodes, {context['edge_count']} edges"
        )

        seeds = _pick_seeds(context["graph_path"], random_count=args.random_queries)
        print(f"High-degree seed: {seeds['high_degree_node']} (degree {seeds['high_degree_value']})")
        plan = _build_query_plan(seeds, queries_per_depth_on_hub=args.hub_queries_per_depth)
        warmup_plan = plan[: args.warmup_queries]

        old_route_settings = routes.settings
        old_worker_settings, old_sessions = runner.settings, runner.SessionLocal
        routes.settings = context["settings"]
        client = TestClient(app)
        try:

            def override_session():
                with context["sessions"]() as session:
                    yield session

            app.dependency_overrides[get_session] = override_session
            print(f"Warming up with {len(warmup_plan)} queries ...")
            _run_plan(client, case_id=context["case_id"], headers=context["headers"], plan=warmup_plan, workers=1)

            print(f"Running {len(plan)} queries at 1 reader ...")
            single = _run_plan(client, case_id=context["case_id"], headers=context["headers"], plan=plan, workers=1)
            print(f"Running {len(plan)} queries at 4 readers ...")
            concurrent = _run_plan(client, case_id=context["case_id"], headers=context["headers"], plan=plan, workers=4)
        finally:
            app.dependency_overrides.clear()
            routes.settings, runner.settings, runner.SessionLocal = old_route_settings, old_worker_settings, old_sessions

    result = {
        "source": str(args.source),
        "graph_node_count": context["node_count"],
        "graph_edge_count": context["edge_count"],
        "high_degree_node": seeds["high_degree_node"],
        "high_degree_value": seeds["high_degree_value"],
        "top_degree_nodes": seeds["top_degree_nodes"],
        "single_reader": single,
        "four_readers": concurrent,
    }
    print(json.dumps(result, indent=2, default=str))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
