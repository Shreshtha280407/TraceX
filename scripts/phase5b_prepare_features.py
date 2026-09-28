"""Phase 5B feature preparation: real TraceX exporter -> frozen 37-column matrix.

Ingests the Phase 5A 100K fixture through the real pipeline (direct internal calls,
the same pattern `scripts/phase2_100k_smoke.py` already uses at this scale — no
multipart HTTP upload of an 84MB file), then reads the feature rows back through the
real `GET /v1/cases/{case_id}/features/export` and `GET /v1/cases/{case_id}/findings`
HTTP endpoints via an in-process TestClient. No synthetic feature table is fabricated
anywhere in this module — every number here traces back to a real exported row.

`evaluation_truth.json` is read directly off disk, only to compute evaluation-only
labels and split boundaries. It is never ingested and never becomes a feature.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# The exact window size app.engine.findings.deterministic.WINDOW_SECONDS[0] uses for
# peeling-chain/coinjoin key-resolution — filtering to it is what makes a real
# (non-invented) join to evaluation_truth.json's transaction-scoped labels possible.
WINDOW_SECONDS_FILTER = 900

FEATURE_COLUMNS: tuple[str, ...] = (
    "in_event_count", "out_event_count", "received_output_count", "outgoing_output_count",
    "observed_counterparties",
    "received_value_min_sats", "received_value_max_sats", "received_value_median_sats", "received_value_total_sats",
    "outgoing_value_min_sats", "outgoing_value_max_sats", "outgoing_value_median_sats", "outgoing_value_total_sats",
    "outgoing_value_missing",
    "inter_event_gap_count", "inter_event_gap_min_seconds", "inter_event_gap_max_seconds",
    "inter_event_gap_median_seconds", "inter_event_gap_missing",
    "component_node_delta", "component_edge_delta", "component_resolved_spend_edge_delta",
    "first_observed_activity",
    "prior_window_gap_seconds", "baseline_in_event_count_mean", "activity_surge_ratio",
    "baseline_value_sats_mean", "value_surge_ratio",
    "peeling_chain_score", "peeling_chain_length", "peeling_chain_total_duration_sec",
    "peeling_chain_evidence_count",
    "coinjoin_like_score", "equal_output_count", "equal_output_value_sats", "equal_output_value_missing",
    "coinjoin_like_evidence_count",
)

# Denylist asserted by tests/unit/test_phase5b_no_leakage.py against FEATURE_COLUMNS.
FORBIDDEN_SUBSTRINGS = (
    "address", "txid", "entity_ref", "case_id", "source_id", "locator", "scenario",
    "group_id", "seed", "reviewer", "wallet", "script", "_ip", "geo", "asn", "endpoint",
)
HARD_EXCLUDED_FIELDS = frozenset(
    {"risk_propagation_score", "risk_seed_distance", "risk_path_evidence_count", "risk_seed_count"}
)


def feature_columns_sha256() -> str:
    return hashlib.sha256(",".join(FEATURE_COLUMNS).encode("utf-8")).hexdigest()


def _parse_utc(value: str) -> datetime:
    """SQLite doesn't round-trip tzinfo through SQLAlchemy's DateTime(timezone=True), so
    datetimes read back via the real API (window_start/window_end, finding window_start)
    come back naive, while datetimes parsed from the raw fixture's "Z"-suffixed strings
    come back aware. Every datetime in this module is UTC; normalize both to aware UTC
    right at the parse boundary instead of comparing naive against aware later."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class FeaturePackageError(ValueError):
    """A malformed/incomplete/mismatched feature row or manifest. Reject, never guess."""


def flatten_feature_row(features: dict[str, Any]) -> dict[str, float]:
    """One exported `features` dict -> the frozen 37-column row. Pure, no I/O.

    Raises FeaturePackageError for a row missing a required (non-nullable) field —
    this is how the pre-existing fallback-path stub rows (no matching output-address
    window; documented, unrelated gap) get rejected rather than silently zero-filled.
    """

    def require(key: str) -> Any:
        if key not in features or features[key] is None:
            raise FeaturePackageError(f"feature row missing required field {key!r}")
        return features[key]

    received = features.get("received_value_distribution_sats")
    if not isinstance(received, dict):
        raise FeaturePackageError("received_value_distribution_sats must be present")
    outgoing = features.get("outgoing_value_distribution_sats")
    gaps = features.get("inter_event_gaps_seconds")
    if not isinstance(gaps, dict) or "count" not in gaps:
        raise FeaturePackageError("inter_event_gaps_seconds must be present")
    component = features.get("bounded_component_change")
    if not isinstance(component, dict):
        raise FeaturePackageError("bounded_component_change must be present")
    if "first_observed_activity" not in features:
        raise FeaturePackageError("feature row missing required field 'first_observed_activity'")

    gap_count = gaps["count"]
    equal_output_value = features.get("equal_output_value_sats")
    activity_surge = features.get("activity_surge_ratio")
    value_surge = features.get("value_surge_ratio")

    row = {
        "in_event_count": require("in_event_count"),
        "out_event_count": require("out_event_count"),
        "received_output_count": require("received_output_count"),
        "outgoing_output_count": require("outgoing_output_count"),
        "observed_counterparties": require("observed_counterparties"),
        "received_value_min_sats": received["min_sats"],
        "received_value_max_sats": received["max_sats"],
        "received_value_median_sats": received["median_sats"],
        "received_value_total_sats": received["total_sats"],
        "outgoing_value_min_sats": outgoing["min_sats"] if outgoing else 0,
        "outgoing_value_max_sats": outgoing["max_sats"] if outgoing else 0,
        "outgoing_value_median_sats": outgoing["median_sats"] if outgoing else 0,
        "outgoing_value_total_sats": outgoing["total_sats"] if outgoing else 0,
        "outgoing_value_missing": 0 if outgoing else 1,
        "inter_event_gap_count": gap_count,
        "inter_event_gap_min_seconds": gaps["minimum_seconds"] if gap_count >= 2 else 0,
        "inter_event_gap_max_seconds": gaps["maximum_seconds"] if gap_count >= 2 else 0,
        "inter_event_gap_median_seconds": gaps["median_seconds"] if gap_count >= 2 else 0,
        "inter_event_gap_missing": 0 if gap_count >= 2 else 1,
        "component_node_delta": component["node_delta"],
        "component_edge_delta": component["edge_delta"],
        "component_resolved_spend_edge_delta": component["resolved_spend_edge_delta"],
        "first_observed_activity": 1 if features["first_observed_activity"] else 0,
        "prior_window_gap_seconds": features.get("prior_window_gap_seconds") or 0,
        "baseline_in_event_count_mean": features.get("baseline_in_event_count_mean") or 0,
        "activity_surge_ratio": activity_surge if activity_surge is not None else 1.0,
        "baseline_value_sats_mean": features.get("baseline_value_sats_mean") or 0,
        "value_surge_ratio": value_surge if value_surge is not None else 1.0,
        "peeling_chain_score": require("peeling_chain_score"),
        "peeling_chain_length": require("peeling_chain_length"),
        "peeling_chain_total_duration_sec": require("peeling_chain_total_duration_sec"),
        "peeling_chain_evidence_count": require("peeling_chain_evidence_count"),
        "coinjoin_like_score": require("coinjoin_like_score"),
        "equal_output_count": require("equal_output_count"),
        "equal_output_value_sats": equal_output_value or 0,
        "equal_output_value_missing": 0 if equal_output_value is not None else 1,
        "coinjoin_like_evidence_count": require("coinjoin_like_evidence_count"),
    }
    return {column: float(row[column]) for column in FEATURE_COLUMNS}


@dataclass
class BuiltMatrix:
    columns: tuple[str, ...]
    rows: list[dict[str, float]]
    entity_refs: list[str]
    window_starts: list[datetime]
    rejected_incomplete: int = 0


def build_feature_matrix(export_rows: list[dict[str, Any]], *, window_seconds: int = WINDOW_SECONDS_FILTER) -> BuiltMatrix:
    """Filter to `window_seconds`, flatten every row, reject (and count) malformed ones."""
    rows: list[dict[str, float]] = []
    entity_refs: list[str] = []
    window_starts: list[datetime] = []
    rejected = 0
    for export_row in export_rows:
        window_start = export_row["window_start"]
        window_end = export_row["window_end"]
        if isinstance(window_start, str):
            window_start = _parse_utc(window_start)
        if isinstance(window_end, str):
            window_end = _parse_utc(window_end)
        if int((window_end - window_start).total_seconds()) != window_seconds:
            continue
        try:
            flat = flatten_feature_row(export_row["features"])
        except FeaturePackageError:
            rejected += 1
            continue
        rows.append(flat)
        entity_refs.append(export_row["entity_ref"])
        window_starts.append(window_start)
    if not rows:
        raise FeaturePackageError(
            f"no usable {window_seconds}s-window feature rows after flattening "
            f"({rejected} rejected as incomplete) — refusing to build an empty package"
        )
    return BuiltMatrix(columns=FEATURE_COLUMNS, rows=rows, entity_refs=entity_refs, window_starts=window_starts, rejected_incomplete=rejected)


def window_bucket_start(observed: datetime, window_seconds: int = WINDOW_SECONDS_FILTER) -> datetime:
    epoch_seconds = int(observed.timestamp())
    bucket = epoch_seconds // window_seconds * window_seconds
    return datetime.fromtimestamp(bucket, tz=UTC)


@dataclass
class SplitBoundaries:
    train_reference_end: datetime
    validation_end: datetime

    def assign(self, window_start: datetime) -> str:
        if window_start < self.train_reference_end:
            return "train_reference"
        if window_start < self.validation_end:
            return "validation"
        return "final_holdout"


def compute_split_boundaries(transactions_ndjson: Path, truth: dict[str, Any]) -> SplitBoundaries:
    """Real boundaries from real transaction timestamps at the real group index
    ranges in evaluation_truth.json's split_groups — never re-derived from the
    exported feature rows themselves, and asserted strictly ordered before use."""
    groups = sorted(truth["split_groups"], key=lambda g: g["transaction_index_start"])
    train_groups = [g for g in groups if g["split"] == "train_reference"]
    validation_groups = [g for g in groups if g["split"] == "validation"]
    if not train_groups or not validation_groups:
        raise FeaturePackageError("evaluation_truth.json split_groups missing train_reference or validation groups")
    train_end_index = max(g["transaction_index_end_exclusive"] for g in train_groups)
    validation_end_index = max(g["transaction_index_end_exclusive"] for g in validation_groups)

    def timestamp_at(index: int) -> datetime:
        with transactions_ndjson.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle):
                if line_number == index:
                    return _parse_utc(json.loads(line)["block_time"])
        raise FeaturePackageError(f"transaction index {index} not found in {transactions_ndjson}")

    train_reference_end = timestamp_at(train_end_index)
    validation_end = timestamp_at(validation_end_index)
    if not (train_reference_end < validation_end):
        raise FeaturePackageError(
            f"split boundaries are not strictly time-ordered: train_reference_end={train_reference_end} "
            f"validation_end={validation_end} — refusing to proceed with a malformed split"
        )
    return SplitBoundaries(train_reference_end=train_reference_end, validation_end=validation_end)


@dataclass
class LabelJoinResult:
    positive_keys: dict[str, set[tuple[str, datetime]]] = field(default_factory=dict)
    dropped_counts: dict[str, int] = field(default_factory=dict)


def resolve_label_positives(*, records: dict[str, list[dict]], truth: dict[str, Any], feature_keys: set[tuple[str, datetime]]) -> LabelJoinResult:
    """Resolve evaluation_truth.json txids to (entity_ref, window_start) keys using the
    *real* detectors and the *same* key-resolution app.engine.findings.deterministic's
    materialize_findings performs — never a parallel invented heuristic. A label whose
    resolved key isn't in the prepared population is dropped and counted, not padded."""
    from app.engine.motifs.deterministic import detect_coinjoin_like_transactions, detect_peeling_chains

    transactions = {t["txid"]: t for t in records["transactions"]}
    transaction_times = {
        txid: _parse_utc(t["block_time"]) for txid, t in transactions.items() if t.get("block_time")
    }
    outputs_by_tx: dict[str, list[dict]] = defaultdict(list)
    for output in records["outputs"]:
        outputs_by_tx[output["txid"]].append(output)

    def resolve_via_own_outputs(txid: str) -> tuple[str, datetime] | None:
        observed = transaction_times.get(txid)
        if not observed:
            return None
        start = window_bucket_start(observed)
        for output in outputs_by_tx.get(txid, []):
            candidate = output.get("address") or output.get("script_id")
            if candidate and (str(candidate), start) in feature_keys:
                return str(candidate), start
        return None

    peeling = detect_peeling_chains(transactions=transactions, inputs=records["inputs"], outputs=records["outputs"])
    coinjoin = detect_coinjoin_like_transactions(transactions=transactions, inputs=records["inputs"], outputs=records["outputs"])

    peeling_by_txid: dict[str, tuple[str, datetime] | None] = {}
    for signal in peeling:
        address = signal["entity_ref"].removeprefix("address:")
        observed = transaction_times.get(signal["transaction_ids"][0])
        key = (address, window_bucket_start(observed)) if observed else None
        for hop_txid in signal["transaction_ids"]:
            peeling_by_txid.setdefault(hop_txid, key if key and key in feature_keys else None)

    coinjoin_by_txid = {signal["transaction_id"]: resolve_via_own_outputs(signal["transaction_id"]) for signal in coinjoin}

    def resolve_many(txids: list[str], preferred: dict[str, tuple[str, datetime] | None] | None = None) -> tuple[set[tuple[str, datetime]], int]:
        keys: set[tuple[str, datetime]] = set()
        dropped = 0
        for txid in txids:
            key = (preferred or {}).get(txid) if preferred else None
            if key is None:
                key = resolve_via_own_outputs(txid)
            if key is None or key not in feature_keys:
                dropped += 1
                continue
            keys.add(key)
        return keys, dropped

    result = LabelJoinResult()
    for label_name, txids, preferred in (
        ("peeling_chain", truth.get("expected_peeling_chain_transaction_ids", []), peeling_by_txid),
        ("coinjoin_like", truth.get("expected_coinjoin_like_transaction_ids", []), coinjoin_by_txid),
        ("benign_ordinary_sequence", truth.get("expected_benign_controls", {}).get("ordinary_sequence_transaction_ids", []), None),
        ("benign_ordinary_multi_output", truth.get("expected_benign_controls", {}).get("ordinary_multi_output_transaction_ids", []), None),
    ):
        keys, dropped = resolve_many(txids, preferred)
        result.positive_keys[label_name] = keys
        result.dropped_counts[label_name] = dropped
    return result


def ingest_fixture_in_process(fixture_dir: Path, *, database_url: str, evidence_root: Path) -> tuple[str, str]:
    """Ingest the 100K fixture through the real pipeline via direct internal calls
    (the same pattern scripts/phase2_100k_smoke.py already uses at this scale — no
    HTTP multipart upload of the ~85MB source file). Returns (case_id, job_id)."""
    import hashlib as _hashlib

    from sqlalchemy.orm import sessionmaker

    from app.config import Settings
    from app.db import Base, make_engine
    from app.jobs.service import create_or_reuse_job
    from app.models import Case, CaseMembership, EvidenceSource, User
    from workers import runner

    source_path = fixture_dir / "ingestion_rows.ndjson"
    if not source_path.is_file():
        raise FeaturePackageError(f"{source_path} not found — generate the fixture first")
    source_hash = _hashlib.sha256(source_path.read_bytes()).hexdigest()

    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as session:
        user = User(external_subject="phase5b-prepare")
        session.add(user)
        session.flush()
        case = Case(name="Phase 5B 100K comparison", synthetic=True, created_by=user.id)
        session.add(case)
        session.flush()
        session.add(CaseMembership(case_id=case.id, user_id=user.id, role="case_lead"))
        relative = Path(case.id) / source_hash / "original"
        destination = evidence_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        source = EvidenceSource(
            case_id=case.id, sha256=source_hash, byte_size=source_path.stat().st_size,
            original_filename=source_path.name, source_format="ndjson",
            storage_relative_path=relative.as_posix(), synthetic=True,
        )
        session.add(source)
        session.flush()
        job, _ = create_or_reuse_job(session, case_id=case.id, source=source, idempotency_key="phase5b-prepare", actor=user)
        session.commit()
        case_id, job_id = case.id, job.id

    settings = Settings(
        database_url=database_url, evidence_root=evidence_root,
        max_upload_bytes=source_path.stat().st_size + 1, lease_seconds=600,
        event_heartbeat_seconds=1, ingestion_batch_records=32768,
    )
    old_settings, old_sessions = runner.settings, runner.SessionLocal
    runner.settings, runner.SessionLocal = settings, sessions
    try:
        if not runner.process_one("phase5b-prepare-worker"):
            raise FeaturePackageError("worker did not claim the Phase 5B ingestion job")
    finally:
        runner.settings, runner.SessionLocal = old_settings, old_sessions
    return case_id, job_id


@dataclass
class FeaturePackage:
    matrix: BuiltMatrix
    splits: list[str]
    label_positives: dict[str, set[tuple[str, datetime]]]
    label_dropped: dict[str, int]
    baseline_rank_by_key: dict[tuple[str, datetime], int]
    manifest: dict[str, Any]


def _load_truth(fixture_dir: Path) -> dict[str, Any]:
    truth_path = fixture_dir / "evaluation_truth.json"
    if not truth_path.is_file():
        raise FeaturePackageError(f"{truth_path} not found — generate the fixture first")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise FeaturePackageError("evaluation_truth.json is not marked evaluation_only/do_not_ingest — refusing to use it")
    return truth


def prepare_feature_package(fixture_dir: Path, *, fixture_manifest_sha256: str) -> FeaturePackage:
    """The full, real pipeline: ingest -> real exporter pull -> flatten -> split -> label join.
    Uses a disposable temp sqlite DB/evidence root for the ingestion scratch state —
    only the returned package (and whatever the caller persists from it) is a deliverable."""
    import tempfile

    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from app.api import routes
    from app.db import get_session, make_engine
    from app.engine.graph.builder import _facts
    from app.main import app
    from app.models import ImportJob

    truth = _load_truth(fixture_dir)

    with tempfile.TemporaryDirectory(prefix="tracex-phase5b-prepare-") as scratch:
        root = Path(scratch)
        database_url = f"sqlite:///{root / 'control.db'}"
        evidence_root = root / "evidence"
        case_id, job_id = ingest_fixture_in_process(fixture_dir, database_url=database_url, evidence_root=evidence_root)

        engine = make_engine(database_url)
        sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def override_session():
            with sessions() as session:
                yield session

        from app.config import Settings

        settings = Settings(database_url=database_url, evidence_root=evidence_root, max_upload_bytes=1, lease_seconds=600, event_heartbeat_seconds=1)
        app.dependency_overrides[get_session] = override_session
        old_route_settings = routes.settings
        routes.settings = settings
        headers = {"X-TraceX-Actor": "phase5b-prepare"}
        try:
            client = TestClient(app)
            with sessions() as session:
                job = session.get(ImportJob, job_id)
                if job is None or job.state != "completed":
                    raise FeaturePackageError(f"ingestion job did not complete: state={job.state if job else 'missing'}")
                snapshot_id = job.snapshot_id
                records = _facts(session, evidence_root, snapshot_id)

            exported = client.get(f"/v1/cases/{case_id}/features/export", headers=headers).json()
            if exported.get("feature_schema_version") != "phase4.1-feature-v1" or not exported.get("rows"):
                raise FeaturePackageError("feature export is empty or has an unexpected schema version")

            matrix = build_feature_matrix(exported["rows"])

            findings_rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = client.get(f"/v1/cases/{case_id}/findings?limit=200&offset={offset}", headers=headers).json()
                findings_rows.extend(page["findings"])
                if len(page["findings"]) < 200:
                    break
                offset += 200
        finally:
            app.dependency_overrides.clear()
            routes.settings = old_route_settings

        boundaries = compute_split_boundaries(fixture_dir / "transactions.ndjson", truth)
        splits = [boundaries.assign(start) for start in matrix.window_starts]

        feature_keys = {
            (entity_ref.removeprefix("address:"), start) for entity_ref, start in zip(matrix.entity_refs, matrix.window_starts, strict=True)
        }
        label_join = resolve_label_positives(records=records, truth=truth, feature_keys=feature_keys)

        baseline_rank_by_key: dict[tuple[str, datetime], int] = {}
        for finding in findings_rows:
            if finding.get("rank") is None:
                continue
            entity = str(finding["entity_or_transaction_id"]).removeprefix("address:")
            start = finding["window_start"]
            if isinstance(start, str):
                start = _parse_utc(start)
            key = (entity, start)
            if key in feature_keys:
                baseline_rank_by_key[key] = min(baseline_rank_by_key.get(key, finding["rank"]), finding["rank"])

    manifest = {
        "feature_schema_version": "phase4.1-feature-v1",
        "window_seconds_filter": WINDOW_SECONDS_FILTER,
        "feature_columns": list(FEATURE_COLUMNS),
        "feature_columns_sha256": feature_columns_sha256(),
        "fixture_manifest_sha256": fixture_manifest_sha256,
        "row_count": len(matrix.rows),
        "rejected_incomplete_rows": matrix.rejected_incomplete,
        "split_counts": {split: splits.count(split) for split in ("train_reference", "validation", "final_holdout")},
        "label_positive_counts": {name: len(keys) for name, keys in label_join.positive_keys.items()},
        "label_dropped_counts": label_join.dropped_counts,
        "baseline_flagged_count": len(baseline_rank_by_key),
        "training_seed": 42,
    }

    return FeaturePackage(
        matrix=matrix, splits=splits, label_positives=label_join.positive_keys, label_dropped=label_join.dropped_counts,
        baseline_rank_by_key=baseline_rank_by_key, manifest=manifest,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=REPO / "datasets" / "phase5a_100k")
    parser.add_argument("--output", type=Path, required=True, help="run directory to write feature_manifest.json into")
    args = parser.parse_args()

    manifest_path = args.fixture / "fixture_manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"Phase 5A 100K fixture not found at {args.fixture}. Generate it first:\n"
            "  python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson --verify"
        )
    fixture_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    package = prepare_feature_package(args.fixture, fixture_manifest_sha256=fixture_manifest_sha256)

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "feature_manifest.json").write_text(json.dumps(package.manifest, indent=2), encoding="utf-8")
    print(f"Prepared {package.manifest['row_count']} rows across splits {package.manifest['split_counts']}")


if __name__ == "__main__":
    main()
