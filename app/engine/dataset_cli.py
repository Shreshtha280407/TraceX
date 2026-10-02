"""`tracex-dataset`: inspect and register a bulk source file before importing it.

    tracex-dataset inspect FILE [--sample N | --full] [--register] [--source-id ID]

Reports, without importing anything: the file's SHA-256 and size, its format
and record count, how its header maps onto the PS minimum field list, a
normalisation trial (accepted / quarantined with reasons), field fill rates,
timestamp parsing, and the IP scope / Geo-IP coverage of its network fields.
`--register` records the result as a source entry in `data_manifest.json`, the
intake checklist the requirement register asks for (R-03, docs/requirements.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.engine import geoip
from app.engine.adapters import SourceParseError, count_records, rows_for_source
from app.engine.canonical import NormalizationError, normalize_row
from app.engine.canonical.profiles import canonicalize, describe

FORMATS = {".csv": "csv", ".ndjson": "ndjson", ".jsonl": "ndjson", ".json": "json", ".xml": "xml"}


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in FORMATS:
        if suffix == ".json":
            with path.open("rb") as handle:
                head = handle.read(4096).lstrip()
            return "json" if head.startswith(b"[") else "ndjson"
        return FORMATS[suffix]
    raise SystemExit(f"cannot tell the format of {path.name}; use .csv, .ndjson/.jsonl, .json or .xml")


def inspect(path: Path, *, sample: int | None) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    source_format = detect_format(path)
    record_count = count_records(path, source_format)
    keys: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    filled: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    seen_txids: set[str] = set()
    duplicates = accepted = examined = 0
    database = geoip.open_database()
    ip_seen: dict[str, str] = {}
    try:
        for row in rows_for_source(path, source_format):
            if sample is not None and examined >= sample:
                break
            examined += 1
            keys.update(row.value.keys())
            canonical, _ = canonicalize(row.value)
            for name, value in canonical.items():
                if value not in (None, "", [], {}):
                    filled[name] += 1
            try:
                normalized = normalize_row(case_id="inspection", source_id="inspection",
                                           source_sha256=digest.hexdigest(), row=row)
            except NormalizationError as error:
                reasons[str(error)] += 1
                continue
            txid = normalized.facts["transactions"][0]["txid"]
            if txid in seen_txids:
                duplicates += 1
                continue
            seen_txids.add(txid)
            accepted += 1
            for observation in normalized.facts["network_observations"]:
                ip = observation.get("src_ip")
                if not ip:
                    continue
                if ip not in ip_seen:
                    if database is not None:
                        hit = database.lookup(ip)
                        ip_seen[ip] = hit.scope
                        if hit.country:
                            countries[hit.country] += 1
                    else:
                        ip_seen[ip] = geoip.ip_scope(ip)[0]
                scopes[ip_seen[ip]] += 1
    except SourceParseError as error:
        reasons[f"parse error: {error}"] += 1
    header = list(keys)
    return {
        "file": path.name,
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
        "format": source_format,
        "records": record_count,
        "examined_records": examined,
        "sampled": sample is not None and record_count is not None and examined < record_count,
        "schema": describe(header),
        "normalisation": {
            "accepted": accepted,
            "duplicate_txids": duplicates,
            "quarantined": sum(reasons.values()),
            "quarantine_reasons": dict(reasons.most_common(20)),
        },
        "field_fill_rates": {name: round(count / max(examined, 1), 4) for name, count in sorted(filled.items())},
        "network": {
            "distinct_src_ips": len(ip_seen),
            "src_ip_scope_observations": dict(scopes),
            "geoip_installed": database is not None,
            "geoip_countries_top": dict(countries.most_common(10)),
        },
    }


def register(report: dict, *, manifest: Path, source_id: str | None, note: str | None) -> dict:
    data = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {
        "schema_version": "1.0.0", "sources": []
    }
    entry = {
        "source_id": source_id or f"src-{report['sha256'][:12]}",
        "kind": "bulk_source_inspected",
        "status": "bytes_verified_locally",
        "path": report["file"],
        "sha256": report["sha256"],
        "bytes": report["bytes"],
        "format": report["format"],
        "record_count": report["records"],
        "inspected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "schema_mapping": report["schema"]["renames"],
        "ps_minimum_fields": report["schema"]["ps_minimum_fields"],
        "missing_ps_fields": report["schema"]["missing"],
        "normalisation_trial": report["normalisation"],
        "authority_or_licence": note,
    }
    data["sources"] = [source for source in data.get("sources", []) if source.get("source_id") != entry["source_id"]]
    data["sources"].append(entry)
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracex-dataset", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("inspect", help="inspect a CSV / NDJSON / JSON / XML source file")
    check.add_argument("file", type=Path)
    scope = check.add_mutually_exclusive_group()
    scope.add_argument("--sample", type=int, default=20_000, help="records to trial-normalise (default 20000)")
    scope.add_argument("--full", action="store_true", help="trial-normalise every record")
    check.add_argument("--register", action="store_true", help="record the result in data_manifest.json")
    check.add_argument("--manifest", type=Path, default=Path("data_manifest.json"))
    check.add_argument("--source-id", default=None)
    check.add_argument("--licence", default=None, help="authority / licence note for the manifest entry")
    args = parser.parse_args(argv)
    report = inspect(args.file, sample=None if args.full else args.sample)
    if args.register:
        report["manifest_entry"] = register(report, manifest=args.manifest, source_id=args.source_id,
                                            note=args.licence)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
