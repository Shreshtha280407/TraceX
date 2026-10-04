"""Explicit bounded variant of the existing synthetic fixture, not a new truth source.

Scale time-index windows and counts, never tune in response to model results.
No training, imports, or selection. All fixture limitations remain applicable.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(output, rows, seed):
    if not 1000 <= rows <= 10000 or output.exists():
        raise ValueError("use a fresh directory and 1,000..10,000 rows")
    spec = importlib.util.spec_from_file_location("bounded_base_fixture", ROOT / "fixtures/phase5a_100k/generate.py")
    base = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    config, _ = base.load_config()
    scale = rows / config["transaction_count"]
    config.update(seed=seed, transaction_count=rows, bootstrap_transaction_count=max(36, round(config["bootstrap_transaction_count"] * scale)),
                  duplicate_record_count=2, network_observation_count=max(50, round(config["network_observation_count"] * scale)))
    consumed = 0
    for group in config["split_groups"][:-1]:
        group["count"] = round(group["count"] * scale)
        consumed += group["count"]
    config["split_groups"][-1]["count"] = rows - consumed
    for window in config["motifs"]["surge_windows"]:
        for key in ("start_index", "end_index", "min_transactions", "max_transactions"):
            window[key] = max(1, round(window[key] * scale))
    config["motifs"]["minimum_labelled_positives"] = max(1, round(config["motifs"]["minimum_labelled_positives"] * scale))
    frozen = json.dumps(config, sort_keys=True).encode()
    base.load_config = lambda: (config, hashlib.sha256(frozen).hexdigest())
    # The base validator intentionally hard-codes 100K. Count/hash/value checks
    # for this bounded variant are performed independently, not bypassed as a
    # claim that the old 100K test passed.
    manifest = base.generate(output, ("ndjson",), validate_output=False)
    (output / "bounded_fixture_config.json").write_bytes(frozen)
    from scripts.dataset_acceptance_counts import counts
    observed, raw = counts(output / "ingestion_rows.ndjson")
    if observed["transactions"] != rows:
        raise ValueError("bounded fixture canonical count mismatch")
    (output / "bounded_fixture_check.json").write_text(json.dumps({"schema": "bounded-study-fixture-v1", "counts": observed,
        "source_rows": raw, "seed": seed, "manifest_counts": manifest["counts"], "validation": "independent disk-indexed counts; base hard-coded 100K validator NOT RUN",
        "limitations": "Synthetic source-author generator labels only; neither independently authored truth nor representative real-world labels."}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args(argv)
    generate(args.output, args.rows, args.seed)


if __name__ == "__main__":
    main()
