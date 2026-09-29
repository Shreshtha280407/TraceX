"""Generator v2 properties, each pinned to the measured v1 defect it fixes.

These are deliberately *statistical* assertions with wide margins.  Their job is
to fail if a future change silently reintroduces a defect that made the fixture
unable to falsify a model, not to pin exact counts that drift with the seed.
"""

from __future__ import annotations

import collections
import importlib.util
import itertools
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "phase5a_100k"


def _generator():
    spec = importlib.util.spec_from_file_location("phase5a_v2_generator_test", FIXTURE / "generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory):
    generator = _generator()
    output = tmp_path_factory.mktemp("phase5a_v2") / "dataset"
    manifest = generator.generate(output, ("csv", "ndjson", "xml", "json"))
    truth = json.loads((output / "evaluation_truth.json").read_text(encoding="utf-8"))
    return generator, output, manifest, truth


def _times(generator, output: Path) -> dict[str, int]:
    return {
        row["txid"]: int(datetime.fromisoformat(row["block_time"]).timestamp())
        for row in generator.ndjson_rows(output / "transactions.ndjson")
    }


def test_address_namespace_does_not_collide(dataset) -> None:
    """v1 flattened (index, vout) into index*32+vout with 100 outputs per bootstrap
    transaction, so 100,000 outputs collapsed onto 32,068 addresses and manufactured
    99.99% of the rule baseline's hits."""
    generator, output, _, _ = dataset
    config, _ = generator.load_config()
    per_address = collections.Counter()
    for row in generator.ndjson_rows(output / "outputs.ndjson"):
        per_address[row["address"]] += 1
    reused = sum(1 for count in per_address.values() if count > 1)
    # Reuse must be explained by the modelled hot pool, not by a namespace clash.
    assert reused <= config["addresses"]["hot_address_count"] * 1.05
    assert len(per_address) > 50_000


def test_arrivals_are_bursty(dataset) -> None:
    """v1's timestamps were an arithmetic ladder: index of dispersion ~0, so no
    activity surge existed anywhere for a detector to find."""
    generator, output, _, _ = dataset
    stamps = sorted(_times(generator, output).values())
    gaps = [later - earlier for earlier, later in itertools.pairwise(stamps)]
    dispersion = statistics.pvariance(gaps) / statistics.mean(gaps)
    assert dispersion > 5.0, f"arrivals are too regular (dispersion {dispersion:.2f})"
    per_hour = collections.Counter(stamp // 3600 for stamp in stamps)
    assert max(per_hour.values()) >= 3 * statistics.median(per_hour.values())


def test_motif_surges_exist_in_every_split(dataset) -> None:
    """v1 emitted CoinJoin on a metronome -- one per 6.6 hours, never more than one
    per hour -- so the primary requirement had zero positive instances."""
    generator, output, _, truth = dataset
    times = _times(generator, output)
    for family in ("coinjoin_like", "peel_step"):
        stamps = sorted(
            times[txid] for txid, record in truth["labelled_transactions"].items()
            if record["family"] == family
        )
        assert len(stamps) >= 200
        per_hour = collections.Counter(stamp // 3600 for stamp in stamps)
        assert max(per_hour.values()) >= 8, f"{family} never bursts"
    realised = [episode for episode in truth["motif_episodes"] if episode["transaction_ids"]]
    assert len(realised) >= 14
    by_split = collections.Counter(episode["split"] for episode in realised)
    for split in ("train_reference", "validation", "final_holdout"):
        assert by_split[split] >= 2, f"{split} carries no surge episode"


def test_spend_latency_is_bimodal_and_censored(dataset) -> None:
    """v1 landed 13 of 99,241 resolved spends inside an hour, which left eight of the
    frozen 37 feature columns describing six rows."""
    generator, output, manifest, _ = dataset
    times = _times(generator, output)
    delays = []
    for row in generator.ndjson_rows(output / "inputs.ndjson"):
        if row["prev_txid"] is None:
            continue
        spent, created = times.get(row["txid"]), times.get(row["prev_txid"])
        if spent is not None and created is not None and spent >= created:
            delays.append(spent - created)
    rapid = sum(1 for delay in delays if delay <= 3600)
    assert rapid / len(delays) > 0.02, "rapid-spend behaviour is too rare to model"
    # Two separated modes in log space, not one broad hump.
    buckets = collections.Counter(int(math.log10(max(delay, 1))) for delay in delays)
    assert sum(count for decade, count in buckets.items() if decade <= 3) > 3_000
    assert sum(count for decade, count in buckets.items() if decade >= 5) > 3_000
    assert manifest["counts"]["unspent_outputs"] > 1_000, "no right-censored outputs for the survival layer"


def test_coinjoin_remainder_is_not_a_constant(dataset) -> None:
    """v1's remainder was always 13,007-13,009 sats against a population median of
    710,560, so an anomaly detector scored 0.72 P@50 by memorising a 3-sat window."""
    generator, output, _, truth = dataset
    coinjoins = {txid for txid, record in truth["labelled_transactions"].items()
                 if record["family"] == "coinjoin_like"}
    by_tx = collections.defaultdict(list)
    for row in generator.ndjson_rows(output / "outputs.ndjson"):
        if row["txid"] in coinjoins:
            by_tx[row["txid"]].append(row["amount_sats"])
    remainders = []
    for values in by_tx.values():
        counts = collections.Counter(values)
        equal_value = max(counts.items(), key=lambda item: (item[1], item[0]))[0]
        remainders.extend(value for value in values if value != equal_value)
    assert len(set(remainders)) > 200, "remainder values are near-constant and will leak"


def test_rule_positive_benign_negatives_exist(dataset) -> None:
    """Without transactions that satisfy the deterministic rule *and* are benign,
    every near-miss is one predicate short, the rule excludes them all for free,
    and no measurement can distinguish a threshold from a model."""
    generator, output, _, truth = dataset
    counts = truth["family_counts"]
    assert counts.get("nearmiss_rule_positive", 0) >= 200
    targets = {txid for txid, record in truth["labelled_transactions"].items()
               if record["family"] == "nearmiss_rule_positive"}
    outputs = collections.defaultdict(list)
    inputs = collections.Counter()
    for row in generator.ndjson_rows(output / "outputs.ndjson"):
        if row["txid"] in targets:
            outputs[row["txid"]].append(row["amount_sats"])
    for row in generator.ndjson_rows(output / "inputs.ndjson"):
        if row["txid"] in targets and row["prev_txid"] is not None:
            inputs[row["txid"]] += 1
    satisfied = 0
    for txid, values in outputs.items():
        equal = max(collections.Counter(values).values())
        if inputs[txid] >= 3 and len(values) >= 3 and equal >= 3:
            satisfied += 1
    assert satisfied / max(len(outputs), 1) > 0.9, (
        "rule-positive benign transactions must actually satisfy the rule predicate"
    )


def test_network_context_carries_information(dataset) -> None:
    """v1's geo_country was uniform and src_ip/asn were index%29 / index%19 -- pure
    functions of row order, so four of the twelve PS fields were information-free."""
    generator, output, _, truth = dataset
    config, _ = generator.load_config()
    settings = config["network_context"]
    surge_asns = {f"AS{64512 + settings['surge_asn_offset'] + offset}"
                  for offset in range(settings["surge_asn_count"])}
    labelled = truth["labelled_transactions"]
    countries = collections.Counter()
    in_surge = out_surge = in_total = out_total = 0
    for row in generator.ndjson_rows(output / "ingestion_rows.ndjson"):
        countries[row["geo_country"]] += 1
        record = labelled.get(row["txid"])
        if record and record["in_surge"]:
            in_total += 1
            in_surge += row["asn"] in surge_asns
        else:
            out_total += 1
            out_surge += row["asn"] in surge_asns
    share = [count / sum(countries.values()) for count in countries.values()]
    assert max(share) > 2 * min(share), "geo_country is uniform and carries no information"
    lift = (in_surge / max(in_total, 1)) / max(out_surge / max(out_total, 1), 1e-9)
    assert lift > 2.0, "surge relay tendency is not learnable"
    assert lift < 50.0, "surge relay tendency is a label in disguise"


def test_every_ingestion_format_reconciles(dataset) -> None:
    """The PS asks for bulk CSV, JSON and XML intake; v1's generator emitted only
    CSV and NDJSON and refused any other format by assertion."""
    generator, output, _, _ = dataset
    for name in ("ingestion_rows.csv", "ingestion_rows.ndjson", "ingestion_rows.xml", "ingestion_rows.json"):
        assert (output / name).is_file(), f"{name} was not generated"
    # generate() already ran validate(), which cross-reconciles all four by
    # (txid, source_record_id) and fails the run on any mismatch.
    payload = json.loads((output / "ingestion_rows.json").read_text(encoding="utf-8"))
    assert len(payload) == 100_025
    assert set(generator.REQUIRED_RAW_FIELDS).issubset(payload[0])
