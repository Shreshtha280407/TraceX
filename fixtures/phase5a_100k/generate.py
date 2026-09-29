# Run: python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson,xml,json --verify
"""Build the deterministic, local-only Phase 5A 100K preparation fixture (generator v2).

v2 exists because v1 could not falsify a model.  Measured defects that this
generator fixes, each of which silently inflated every candidate metric:

* **Address-namespace collision.**  v1 derived output addresses from
  ``index * 32 + vout`` while bootstrap transactions emitted 100 outputs, so
  every address was re-emitted by ~3 consecutive transactions.  99.99% of the
  windows the Phase 4 ``concentrated_collection`` rule fired on were that
  collision, not a designed scenario.  Addresses are now keyed on the full
  ``(index, vout)`` pair and reuse is an explicit, modelled behaviour.
* **No burstiness.**  v1 timestamps advanced on a fixed arithmetic ladder, so
  no activity surge of any kind existed.  Arrivals are now a non-homogeneous
  Poisson process with a diurnal rate and explicitly labelled burst episodes.
* **No motif surge.**  v1 emitted CoinJoin-like structure on a metronome, one
  per 6.6 hours.  Motif emission now has a baseline rate plus labelled
  surge episodes at a multiple of it.
* **Leaked remainder value.**  v1's CoinJoin remainder output was always
  ~13,008 sats against a population median of 710,560, so any detector scored
  well by memorising a 3-sat-wide constant.  Remainders are now drawn from the
  same change distribution as ordinary transactions.
* **No rapid spends.**  v1 picked a random UTXO from a flat pool, so 13 of
  99,241 resolved spends landed inside an hour.  Spend latency is now bimodal
  and scheduled, with an explicit unspent (right-censored) fraction.
* **No shape diversity and no near misses.**  v1 was 98% single-output
  transactions and equal-output structure was a perfect 35/35 rule.  Shapes are
  now a realistic mix, and labelled near-miss negatives sit next to every motif.
* **Information-free network context.**  v1's ``geo_country`` was uniform and
  ``src_ip``/``asn`` were functions of the row index.  Both are now skewed, with
  a correlated (not deterministic) tendency during motif surges.

The raw exports still contain opaque identifiers only.  Scenario membership,
split membership, episode identity and expected outcomes are evaluation truth
and never ingestion data.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import heapq
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "fixture_config.json"
REQUIRED_RAW_FIELDS = (
    "timestamp", "network", "txid", "input_addresses", "output_addresses",
    "input_amounts", "output_amounts", "src_ip", "dst_ip", "src_port", "dst_port",
    "geo_country", "asn", "fee", "script_type", "inputs", "outputs",
)
INGESTION_COLUMNS = ("source_record_id", "source_locator", *REQUIRED_RAW_FIELDS)
NDJSON_OUTPUTS = (
    "transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson",
    "duplicate_candidates.ndjson",
)
SUPPORTED_FORMATS = ("csv", "ndjson", "xml", "json")

# Motif families.  `nearmiss_*` families are labelled negatives: they sit
# deliberately close to a motif so a threshold rule and a graded model can be
# told apart.  A fixture without them cannot show that ML adds anything.
MOTIF_FAMILIES = (
    "coinjoin_like", "peel_step", "batch_fanout", "consolidation",
    "nearmiss_two_equal", "nearmiss_tolerance", "nearmiss_two_inputs", "nearmiss_batch",
    "nearmiss_rule_positive",
)
# The hardest negative in the fixture: it satisfies the deterministic rule's
# predicate exactly (>=3 inputs, >=3 outputs, >=3 exactly-equal outputs) while
# being an ordinary recurring payroll-shaped payment.  Without it every
# near-miss is one predicate short, the rule excludes them all for free, and the
# discrimination task cannot show whether a model adds anything over a threshold.
RULE_POSITIVE_BENIGN = "nearmiss_rule_positive"
POSITIVE_FAMILIES = ("coinjoin_like", "peel_step")

#: Offsets, relative to the first post-bootstrap index, of the hand-built Phase 4.1
#: detector anchors.  Exported so tests locate them from the config instead of
#: hardcoding an absolute index, which is what broke when the bootstrap block
#: changed size.
ANCHOR_SPAN = 50
ANCHOR_OFFSETS = {
    "peel_chain_start": 0, "peel_chain_steps": (1, 2, 3),
    "ordinary_sequence": (10, 11),
    "seed_path": (20, 21, 22),
    "equal_output_control": 30, "ordinary_multi_output": 31,
    "disconnected": 40,
}


def anchor_indexes(config: dict[str, Any]) -> dict[str, Any]:
    """Absolute transaction indexes of the Phase 4.1 anchors for this config."""
    base = int(config["bootstrap_transaction_count"])
    resolve = lambda value: tuple(base + item for item in value) if isinstance(value, tuple) else base + value
    return {name: resolve(value) for name, value in ANCHOR_OFFSETS.items()}

# Equal-output denominations, in satoshis.  Several of them, so no single
# constant identifies the family.
DENOMINATIONS = (100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000)

COUNTRY_WEIGHTS = (
    ("US", 0.19), ("DE", 0.13), ("IN", 0.12), ("NL", 0.10), ("SG", 0.09), ("JP", 0.08),
    ("GB", 0.07), ("FR", 0.06), ("BR", 0.05), ("AE", 0.04), ("CA", 0.04), ("ZA", 0.03),
)
SCRIPT_TYPE_WEIGHTS = (("p2wpkh", 0.62), ("p2tr", 0.24), ("p2sh", 0.14))


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def btc(sats: int) -> str:
    return f"{sats // 100_000_000}.{sats % 100_000_000:08d}"


def txid(seed: str, index: int) -> str:
    return digest(f"{seed}:canonical-transaction:{index}")


def address(seed: str, namespace: str, ordinal: Any) -> str:
    """Opaque bech32-shaped synthetic address; namespace is never exported.

    `ordinal` is stringified, so a caller may pass a tuple to key an address on a
    full coordinate.  v1's collision came from flattening `(index, vout)` into
    `index * 32 + vout` with more than 32 outputs per transaction.
    """
    return f"bcrt1q{digest(f'{seed}:address:{namespace}:{ordinal}')[:38]}"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=UTC).isoformat().replace("+00:00", "Z")


def source_refs(config_hash: str, locator: str) -> list[dict[str, Any]]:
    return [{
        "evidence_id": "ev-phase5a-100k-config",
        "source_sha256": config_hash,
        "locator_type": "synthetic",
        "locator": locator,
        "byte_start": None,
        "byte_end": None,
    }]


def ndjson_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_config() -> tuple[dict[str, Any], str]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    required = {
        "schema_version", "generator_version", "seed", "network", "transaction_count",
        "bootstrap_transaction_count", "bootstrap_outputs_per_transaction", "duplicate_record_count",
        "network_observation_count", "split_groups", "arrival", "motifs", "spend_latency",
        "addresses", "network_context",
    }
    missing = required.difference(config)
    if missing or config["transaction_count"] != 100_000:
        raise ValueError(f"invalid Phase 5A 100K config; missing={sorted(missing)}")
    if sum(group["count"] for group in config["split_groups"]) != config["transaction_count"]:
        raise ValueError("split groups must partition every canonical transaction")
    if len({group["group_id"] for group in config["split_groups"]}) != len(config["split_groups"]):
        raise ValueError("split group IDs must be unique")
    if config["addresses"]["hot_address_count"] < 1000:
        raise ValueError("hot address pool must be large enough to model realistic reuse")
    return config, file_hash(CONFIG_PATH)


def _group_lookup(config: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    cursor = 0
    result = []
    for group in config["split_groups"]:
        result.append((cursor, cursor + group["count"], group))
        cursor += group["count"]
    return result


def _group_for(index: int, ranges: list[tuple[int, int, dict[str, Any]]]) -> dict[str, Any]:
    for start, end, group in ranges:
        if start <= index < end:
            return group
    raise ValueError(f"transaction {index} has no split group")


def _weighted(rng: random.Random, weighted: tuple[tuple[Any, float], ...]) -> Any:
    target = rng.random() * sum(weight for _, weight in weighted)
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if target <= cumulative:
            return value
    return weighted[-1][0]


def _zipf_table(count: int, exponent: float) -> list[float]:
    """Cumulative Zipf weights; heavy-tailed reuse without a numpy dependency."""
    weights = [1.0 / ((rank + 1) ** exponent) for rank in range(count)]
    total = sum(weights)
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)
    return cumulative


def _zipf_pick(rng: random.Random, cumulative: list[float]) -> int:
    return bisect.bisect_left(cumulative, rng.random())


def _write_ndjson(handle, row: dict[str, Any]) -> None:
    handle.write(compact(row) + "\n")


def _csv_row(raw: dict[str, Any]) -> dict[str, str]:
    return {
        key: compact(raw[key]) if isinstance(raw[key], (list, dict)) else str(raw[key])
        for key in INGESTION_COLUMNS
    }


def _xml_record(raw: dict[str, Any]) -> str:
    """One `<record>` element per raw row, matching the repo's XML adapter shape.

    Scalars become text children; `input_addresses`/`output_addresses`/`*_amounts`
    become repeated elements, which `_element_value` collects back into a list.
    Structured `inputs`/`outputs` stay compact JSON so the XML row remains the
    exact logical equivalent of the NDJSON row.
    """
    parts = ["  <record>"]
    for key in INGESTION_COLUMNS:
        value = raw[key]
        if key in {"input_addresses", "output_addresses", "input_amounts", "output_amounts"}:
            for item in value:
                parts.append(f"    <{key}>{escape(str(item))}</{key}>")
            if not value:
                parts.append(f"    <{key}_empty>true</{key}_empty>")
        elif isinstance(value, (list, dict)):
            parts.append(f"    <{key}>{escape(compact(value))}</{key}>")
        else:
            parts.append(f"    <{key}>{escape(str(value))}</{key}>")
    parts.append("  </record>")
    return "\n".join(parts)


class _Clock:
    """Non-homogeneous Poisson arrivals with a diurnal rate and burst windows.

    v1 used `index * 47 + index % 13`, which is an arithmetic ladder: zero
    burstiness, so no surge existed anywhere in the dataset for a detector to
    find.  Here the instantaneous rate is the base rate times a diurnal factor
    times any active burst multiplier, and inter-arrival times are exponential.
    """

    def __init__(self, rng: random.Random, config: dict[str, Any], base_epoch: float) -> None:
        self._rng = rng
        self._now = base_epoch
        self._mean_gap = float(config["arrival"]["mean_interarrival_seconds"])
        self._diurnal = float(config["arrival"]["diurnal_amplitude"])
        self._multiplier = 1.0

    @property
    def now(self) -> float:
        return self._now

    def set_burst_multiplier(self, multiplier: float) -> None:
        self._multiplier = multiplier

    def advance(self) -> float:
        seconds_of_day = self._now % 86_400.0
        diurnal = 1.0 + self._diurnal * math.sin(2.0 * math.pi * seconds_of_day / 86_400.0)
        rate = self._multiplier * max(diurnal, 0.15) / self._mean_gap
        self._now += self._rng.expovariate(rate)
        return self._now


class _AddressBook:
    """Realistic reuse: a heavy-tailed hot pool plus collision-free fresh keys.

    Every fresh address is keyed on the full `(index, vout)` coordinate, so two
    different outputs can never collide the way v1's flattened ordinal did.
    """

    def __init__(self, rng: random.Random, seed: str, config: dict[str, Any]) -> None:
        self._rng = rng
        self._seed = seed
        count = int(config["addresses"]["hot_address_count"])
        self._reuse_probability = float(config["addresses"]["reuse_probability"])
        self._hot = [address(seed, "hot", ordinal) for ordinal in range(count)]
        self._cumulative = _zipf_table(count, float(config["addresses"]["reuse_zipf_exponent"]))

    @property
    def hot_addresses(self) -> list[str]:
        return self._hot

    def fresh(self, index: int, vout: int) -> str:
        return address(self._seed, "output", (index, vout))

    def recurring(self, slot: int) -> str:
        """A specific hot address, for flows whose recipients genuinely repeat."""
        return self._hot[slot % len(self._hot)]

    def receiver(self, index: int, vout: int) -> str:
        if self._rng.random() < self._reuse_probability:
            return self._hot[_zipf_pick(self._rng, self._cumulative)]
        return self.fresh(index, vout)


class _NetworkContext:
    """Skewed endpoint/geo context with a correlated surge tendency.

    v1's `geo_country` was uniform and `src_ip`/`asn` were `index % k`, so the
    three PS network fields carried exactly zero information.  Here they are
    Zipf-skewed, and during a motif surge a transaction is *more likely* — never
    guaranteed — to be relayed from a small set of endpoints.  That keeps the
    signal learnable without turning it into a label in disguise.
    """

    def __init__(self, rng: random.Random, config: dict[str, Any]) -> None:
        self._rng = rng
        settings = config["network_context"]
        self._asns = [f"AS{64512 + ordinal}" for ordinal in range(int(settings["asn_pool_size"]))]
        self._asn_cumulative = _zipf_table(len(self._asns), float(settings["asn_zipf_exponent"]))
        self._ips = [f"198.51.100.{1 + ordinal % 254}" if ordinal < 254 else f"203.0.113.{1 + ordinal % 254}"
                     for ordinal in range(int(settings["endpoint_pool_size"]))]
        self._ip_cumulative = _zipf_table(len(self._ips), float(settings["endpoint_zipf_exponent"]))
        # Drawn from the *tail* of the Zipf pool on purpose.  Taking them from the
        # head would make the surge ASNs the most common ASNs overall, so their
        # in-surge share would barely differ from baseline and the feature would
        # be a confounder rather than a signal.
        offset = int(settings["surge_asn_offset"])
        self._surge_asns = [self._asns[offset + ordinal] for ordinal in range(int(settings["surge_asn_count"]))]
        self._surge_countries = tuple(settings["surge_countries"])
        self._surge_affinity = float(settings["surge_affinity"])

    def fields(self, *, in_surge: bool) -> dict[str, Any]:
        surge_linked = in_surge and self._rng.random() < self._surge_affinity
        if surge_linked:
            asn = self._surge_asns[self._rng.randrange(len(self._surge_asns))]
            country = self._surge_countries[self._rng.randrange(len(self._surge_countries))]
        else:
            asn = self._asns[_zipf_pick(self._rng, self._asn_cumulative)]
            country = _weighted(self._rng, COUNTRY_WEIGHTS)
        source_ip = self._ips[_zipf_pick(self._rng, self._ip_cumulative)]
        destination_ip = self._ips[_zipf_pick(self._rng, self._ip_cumulative)]
        return {
            "src_ip": source_ip,
            "dst_ip": destination_ip,
            "src_port": 8333 if self._rng.random() < 0.78 else 18_333,
            "dst_port": 8333,
            "geo_country": country,
            "asn": asn,
        }


def _spend_delay(rng: random.Random, settings: dict[str, Any], *, fast: bool) -> float:
    """Bimodal spend latency.  v1's flat random pick produced 13 rapid spends in
    99,241 links, which left eight feature columns describing six rows."""
    if fast:
        return math.exp(rng.gauss(float(settings["fast_log_mean"]), float(settings["fast_log_sigma"])))
    return math.exp(rng.gauss(float(settings["slow_log_mean"]), float(settings["slow_log_sigma"])))


def _motif_schedule(rng: random.Random, config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[int, int, dict[str, Any]]]]:
    """Burst episodes expressed over transaction-index ranges.

    Ranges are used (rather than wall-clock) so an episode always lands inside a
    known split, which is what makes validation and holdout carry real positives.
    """
    settings = config["motifs"]
    episodes: list[dict[str, Any]] = []
    spans: list[tuple[int, int, dict[str, Any]]] = []
    ordinal = 0
    for window in settings["surge_windows"]:
        for slot in range(int(window["episode_count"])):
            length = rng.randrange(int(window["min_transactions"]), int(window["max_transactions"]) + 1)
            span = int(window["end_index"]) - int(window["start_index"]) - length
            start = int(window["start_index"]) + (0 if span <= 0 else rng.randrange(span))
            ordinal += 1
            episode = {
                "episode_id": f"ep-{ordinal:03d}",
                "family": window["family"],
                "split": window["split"],
                "transaction_index_start": start,
                "transaction_index_end_exclusive": start + length,
                "motif_rate_multiplier": float(window["motif_rate_multiplier"]),
                "arrival_rate_multiplier": float(window["arrival_rate_multiplier"]),
            }
            episodes.append(episode)
            spans.append((start, start + length, episode))
    episodes.sort(key=lambda item: item["transaction_index_start"])
    spans.sort(key=lambda item: item[0])
    return episodes, spans


def generate(
    destination: Path, formats: tuple[str, ...] = ("csv", "ndjson"), *, validate_output: bool = True
) -> dict[str, Any]:
    """Generate all local artifacts. ``formats`` controls only ingestion adapters."""
    if not formats or set(formats) - set(SUPPORTED_FORMATS):
        raise ValueError(f"formats must be a non-empty subset of {','.join(SUPPORTED_FORMATS)}")
    config, config_hash = load_config()
    destination.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config["seed"])
    seed = config["seed"]
    ranges = _group_lookup(config)
    base_epoch = datetime(2026, 5, 1, tzinfo=UTC).timestamp()
    clock = _Clock(rng, config, base_epoch)
    book = _AddressBook(rng, seed, config)
    context = _NetworkContext(rng, config)
    latency = config["spend_latency"]
    motif_settings = config["motifs"]
    episodes, episode_spans = _motif_schedule(rng, config)

    counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    duplicate_source_rows: list[dict[str, Any]] = []
    duplicate_indexes = {2_000 + offset * 3_811 for offset in range(config["duplicate_record_count"])}
    labelled: dict[str, dict[str, Any]] = {}
    special: dict[str, Any] = {"coinjoin_txids": [], "peel_control": [], "episode_transactions": defaultdict(list)}
    tx_time: dict[int, float] = {}

    # Scheduled spends: (due_epoch, sequence, output).  Popping only what is due
    # is what makes the realised spend-latency distribution bimodal instead of
    # a flat draw from a global pool.
    pending: list[tuple[float, int, dict[str, Any]]] = []
    sequence = 0
    unspent_outputs = 0
    active_chains: list[dict[str, Any]] = []

    def schedule(output: dict[str, Any], created_at: float, *, fast_bias: float | None = None, censor: bool = True) -> None:
        nonlocal sequence, unspent_outputs
        if censor and rng.random() < float(latency["unspent_fraction"]):
            unspent_outputs += 1  # right-censored on purpose; layer B needs censoring
            return
        probability = float(latency["fast_fraction"]) if fast_bias is None else fast_bias
        delay = _spend_delay(rng, latency, fast=rng.random() < probability)
        sequence += 1
        heapq.heappush(pending, (created_at + delay, sequence, output))

    def take(count: int, now: float) -> list[dict[str, Any]]:
        """Consume up to `count` due outputs; fall back to the earliest scheduled."""
        selected: list[dict[str, Any]] = []
        while len(selected) < count and pending and pending[0][0] <= now:
            selected.append(heapq.heappop(pending)[2])
        while len(selected) < count and pending:
            selected.append(heapq.heappop(pending)[2])
        return selected

    def release(selected: list[dict[str, Any]], now: float) -> None:
        """Return unused outputs to the spendable pool.

        Every shape builder below may decide, after drawing UTXOs, that the draw
        cannot make a well-formed transaction.  Without this the drawn outputs
        would silently leave the UTXO set, which both starves later transactions
        and quietly breaks value conservation for the fixture as a whole.
        """
        nonlocal sequence
        for output in selected:
            sequence += 1
            heapq.heappush(pending, (now, sequence, output))

    with ExitStack() as stack:
        handles = {
            name: stack.enter_context((destination / name).open("w", encoding="utf-8", newline="\n"))
            for name in NDJSON_OUTPUTS
        }
        ingest_ndjson = (
            stack.enter_context((destination / "ingestion_rows.ndjson").open("w", encoding="utf-8", newline="\n"))
            if "ndjson" in formats else None
        )
        csv_writer = None
        if "csv" in formats:
            csv_handle = stack.enter_context((destination / "ingestion_rows.csv").open("w", encoding="utf-8", newline=""))
            csv_writer = csv.DictWriter(csv_handle, fieldnames=INGESTION_COLUMNS, lineterminator="\n")
            csv_writer.writeheader()
        xml_handle = None
        if "xml" in formats:
            xml_handle = stack.enter_context((destination / "ingestion_rows.xml").open("w", encoding="utf-8", newline="\n"))
            xml_handle.write('<?xml version="1.0" encoding="UTF-8"?>\n<records>\n')
        json_handle = None
        if "json" in formats:
            json_handle = stack.enter_context((destination / "ingestion_rows.json").open("w", encoding="utf-8", newline="\n"))
            json_handle.write("[\n")
        json_written = 0

        def emit(
            index: int,
            selected: list[dict[str, Any]] | None,
            amounts: list[int],
            *,
            output_addresses: list[str] | None = None,
            fee: int = 0,
            family: str | None = None,
            episode_id: str | None = None,
            in_surge: bool = False,
            fast_bias: float | None = None,
            censor: bool = True,
            reserve_vouts: tuple[int, ...] = (),
        ) -> list[dict[str, Any]]:
            nonlocal json_written
            current_txid = txid(seed, index)
            now = tx_time[index]
            stamp = iso(now)
            selected = selected or []
            script_type = _weighted(rng, SCRIPT_TYPE_WEIGHTS)
            addresses = output_addresses or [book.receiver(index, vout) for vout in range(len(amounts))]
            transaction = {
                "network": config["network"], "txid": current_txid, "block_time": stamp,
                "fee_sats": fee, "source_locator": f"transaction:{index}",
            }
            _write_ndjson(handles["transactions.ndjson"], transaction)
            counts["transactions"] += 1
            structured_inputs: list[dict[str, Any]] = []
            if selected:
                for vin, item in enumerate(selected):
                    input_row = {
                        "txid": current_txid, "vin": vin, "prev_txid": item["txid"], "prev_vout": item["vout"],
                        "address": item["address"], "amount_sats": item["amount_sats"], "sequence": 4_294_967_293,
                        "source_locator": f"input:{index}:{vin}",
                    }
                    _write_ndjson(handles["inputs.ndjson"], input_row)
                    counts["inputs"] += 1
                    structured_inputs.append({key: input_row[key] for key in ("prev_txid", "prev_vout", "address", "amount_sats", "sequence")})
            else:
                input_row = {
                    "txid": current_txid, "vin": 0, "prev_txid": None, "prev_vout": None, "address": None,
                    "amount_sats": None, "sequence": 4_294_967_293, "source_locator": f"input:{index}:0",
                }
                _write_ndjson(handles["inputs.ndjson"], input_row)
                counts["inputs"] += 1
                structured_inputs.append({key: input_row[key] for key in ("prev_txid", "prev_vout", "address", "amount_sats", "sequence")})
            created: list[dict[str, Any]] = []
            for vout, amount in enumerate(amounts):
                output = {
                    "txid": current_txid, "vout": vout, "amount_sats": amount,
                    "script_type": script_type, "address": addresses[vout], "source_locator": f"output:{index}:{vout}",
                }
                _write_ndjson(handles["outputs.ndjson"], output)
                counts["outputs"] += 1
                created.append(output)
            raw = {
                "source_record_id": f"r-{index:06d}", "source_locator": f"record:{index + 1}",
                "timestamp": stamp, "network": config["network"], "txid": current_txid,
                "input_addresses": [item["address"] for item in selected], "output_addresses": addresses,
                "input_amounts": [btc(item["amount_sats"]) for item in selected],
                "output_amounts": [btc(amount) for amount in amounts], "fee": btc(fee), "script_type": script_type,
                "inputs": structured_inputs,
                "outputs": [{"address": addresses[vout], "amount_sats": amount, "script_type": script_type} for vout, amount in enumerate(amounts)],
                **context.fields(in_surge=in_surge),
            }
            if ingest_ndjson is not None:
                _write_ndjson(ingest_ndjson, raw)
            if csv_writer is not None:
                csv_writer.writerow(_csv_row(raw))
            if xml_handle is not None:
                xml_handle.write(_xml_record(raw) + "\n")
            if json_handle is not None:
                json_handle.write(("," if json_written else "") + compact(raw) + "\n")
                json_written += 1
            counts["ingestion_rows"] += 1
            if index in duplicate_indexes:
                duplicate_source_rows.append(dict(raw))
            if family is not None:
                family_counts[family] += 1
                labelled[current_txid] = {
                    "family": family,
                    "episode_id": episode_id,
                    "in_surge": bool(episode_id),
                    "transaction_index": index,
                }
                if episode_id:
                    special["episode_transactions"][episode_id].append(current_txid)
            for output in created:
                # A reserved output is claimed by the caller (the next hop of a
                # peel chain, or an anchor control's continuation).  Scheduling it
                # here as well would let an unrelated transaction spend it too.
                if output["vout"] in reserve_vouts:
                    continue
                schedule(output, now, fast_bias=fast_bias, censor=censor)
            return created

        # ---- Bootstrap: a controlled starting ledger with unresolved prevouts.
        # Output counts vary so the bootstrap block is no longer the most
        # equal-output-shaped object in the file (v1 emitted 100 identical
        # outputs 1,000 times, which dominated every unsupervised ranking).
        bootstrap = int(config["bootstrap_transaction_count"])
        for index in range(bootstrap):
            tx_time[index] = clock.advance()
            width = rng.randrange(6, int(config["bootstrap_outputs_per_transaction"]) + 1)
            unit = rng.randrange(4_000_000, 90_000_000)
            amounts = [max(20_000, int(unit * rng.uniform(0.35, 1.65))) for _ in range(width)]
            emit(index, None, amounts, fast_bias=0.55, censor=False)

        def settle(total: int, fee: int, *, minimum: int = 1_000) -> tuple[int, int]:
            """Clamp the fee so the outputs can sum to exactly `total - fee`.

            Every amount list below is built as a partition of `total - fee`; if the
            drawn fee left less than `minimum` the old code clamped the *amount*
            instead, which silently broke satoshi conservation."""
            fee = max(0, min(fee, total - minimum))
            return fee, total - fee

        def change_amount(total: int, fee: int) -> int:
            """Ordinary change draw — also the source of every CoinJoin remainder."""
            spendable = total - fee
            share = min(max(rng.betavariate(2.0, 2.4), 0.02), 0.97)
            return max(1_200, int(spendable * share))

        def in_episode(index: int) -> dict[str, Any] | None:
            for start, end, episode in episode_spans:
                if start <= index < end:
                    return episode
                if start > index:
                    break
            return None

        def emit_coinjoin(index: int, episode: dict[str, Any] | None) -> None:
            participants = rng.randrange(int(motif_settings["min_equal_outputs"]), int(motif_settings["max_equal_outputs"]) + 1)
            selected = take(rng.randrange(max(3, participants - 2), participants + 1), tx_time[index])
            if len(selected) < 3:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(600, 4_200))
            denomination = min(
                (value for value in DENOMINATIONS if value * participants < spendable),
                default=None,
                key=lambda value: abs(value * participants - int(total * 0.72)),
            )
            if denomination is None:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            remainder_total = spendable - denomination * participants
            remainder_count = rng.randrange(1, 4)
            remainders: list[int] = []
            for slot in range(remainder_count - 1):
                part = change_amount(remainder_total - sum(remainders), 0)
                if part < 1_200 or part >= remainder_total - sum(remainders):
                    break
                remainders.append(part)
            remainders.append(remainder_total - sum(remainders))
            if any(part < 1_000 for part in remainders):
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            amounts = [denomination] * participants + remainders
            emit(index, selected, amounts, fee=fee, family="coinjoin_like",
                 episode_id=episode["episode_id"] if episode else None,
                 in_surge=bool(episode), fast_bias=float(motif_settings["motif_fast_fraction"]))
            special["coinjoin_txids"].append(txid(seed, index))

        def emit_nearmiss(index: int, kind: str, episode: dict[str, Any] | None) -> None:
            """Labelled negatives that a pure threshold rule must not separate from
            the real motif, so a graded model has something to prove."""
            wanted = 3 if kind != "nearmiss_two_inputs" else 2
            selected = take(wanted, tx_time[index])
            if len(selected) < wanted:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(600, 4_000))
            if kind == "nearmiss_two_equal":
                # Exactly two equal outputs: one short of the equal-output rule.
                unit = spendable // 3
                if spendable - 2 * unit == unit:
                    unit -= 1_000  # never accidentally emit a third equal output
                amounts = [unit, unit, spendable - 2 * unit]
            elif kind == "nearmiss_tolerance":
                unit = spendable // 4
                drift = [0, rng.randrange(120, 900), -rng.randrange(120, 900)]
                amounts = [unit + offset for offset in drift]
                amounts.append(spendable - sum(amounts))
            else:  # nearmiss_two_inputs: exactly-equal outputs, too few inputs
                unit = spendable // 4
                amounts = [unit, unit, unit, spendable - 3 * unit]
            if any(amount < 1_000 for amount in amounts):
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            emit(index, selected, amounts, fee=fee, family=kind,
                 episode_id=None, in_surge=bool(episode))

        def emit_batch(index: int, episode: dict[str, Any] | None, *, near: bool) -> None:
            selected = take(rng.randrange(1, 3), tx_time[index])
            if not selected:
                emit_normal(index, episode)
                return
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(800, 6_000))
            width = rng.randrange(6, 19)
            unit = spendable // width
            if unit < 2_000:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            spread = 3 if near else 260
            amounts = [max(1_500, unit + rng.randrange(-spread, spread + 1)) for _ in range(width - 1)]
            amounts.append(spendable - sum(amounts))
            if amounts[-1] < 1_000:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            emit(index, selected, amounts, fee=fee,
                 family="nearmiss_batch" if near else "batch_fanout",
                 episode_id=None, in_surge=bool(episode))

        def emit_rule_positive_benign(index: int, episode: dict[str, Any] | None) -> None:
            """A benign payment that the deterministic rule cannot tell from a CoinJoin.

            Same predicate: three or more inputs, three or more outputs, three or
            more exactly-equal outputs.  What differs is everything a model can
            actually see -- the equal amount is a round payroll figure rather than
            a mixing denomination, the recipients are addresses that recur, and the
            outputs are spent slowly rather than quickly.  Those differences live in
            layers A, B, C and E; none of them live in the rule.
            """
            width = rng.randrange(3, 7)
            selected = take(rng.randrange(3, 6), tx_time[index])
            if len(selected) < 3:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(700, 5_000))
            salary = rng.choice((1_000_000, 2_000_000, 2_500_000, 5_000_000, 10_000_000))
            if salary * width >= spendable - 5_000:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            amounts = [salary] * width + [spendable - salary * width]
            recipients = [book.recurring(index * 7 + slot) for slot in range(width)]
            recipients.append(book.fresh(index, width))  # change
            emit(index, selected, amounts, output_addresses=recipients, fee=fee,
                 family=RULE_POSITIVE_BENIGN, episode_id=None, in_surge=bool(episode),
                 fast_bias=0.08)  # payroll recipients hold, they do not re-spend in minutes

        def emit_consolidation(index: int, episode: dict[str, Any] | None) -> None:
            selected = take(rng.randrange(3, 9), tx_time[index])
            if len(selected) < 3:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(900, 7_000))
            emit(index, selected, [spendable], fee=fee, family="consolidation",
                 episode_id=None, in_surge=bool(episode))

        def emit_normal(index: int, episode: dict[str, Any] | None) -> None:
            selected = take(1 if rng.random() < 0.72 else 2, tx_time[index])
            if not selected:
                raise RuntimeError(
                    f"spendable UTXO pool exhausted at transaction {index}; raise "
                    "bootstrap_transaction_count or lower spend_latency.unspent_fraction"
                )
            total = sum(item["amount_sats"] for item in selected)
            fee, spendable = settle(total, rng.randrange(220, 5_200))
            if spendable < 4_000:
                emit(index, selected, [spendable], fee=fee, in_surge=bool(episode))
                return
            width = 1 if rng.random() < 0.18 else (2 if rng.random() < 0.86 else rng.randrange(3, 6))
            if width == 1:
                amounts = [spendable]
            else:
                amounts = []
                remaining = spendable
                for _ in range(width - 1):
                    part = change_amount(remaining, 0)
                    if part >= remaining or part < 1_200:
                        break
                    amounts.append(part)
                    remaining -= part
                amounts.append(remaining)
            emit(index, selected, amounts, fee=fee, in_surge=bool(episode))

        def start_chain(index: int, episode: dict[str, Any] | None) -> None:
            selected = take(1, tx_time[index])
            if not selected:
                emit_normal(index, episode)
                return
            total = selected[0]["amount_sats"]
            fee, spendable = settle(total, rng.randrange(300, 1_800))
            peel_fraction = rng.uniform(float(motif_settings["min_peel_fraction"]), float(motif_settings["max_peel_fraction"]))
            peel = max(2_000, int(spendable * peel_fraction))
            if spendable - peel < 10_000:
                release(selected, tx_time[index])
                emit_normal(index, episode)
                return
            created = emit(index, selected, [spendable - peel, peel], fee=fee, family="peel_step",
                           episode_id=episode["episode_id"] if episode else None, in_surge=bool(episode),
                           fast_bias=float(motif_settings["motif_fast_fraction"]), reserve_vouts=(0,))
            active_chains.append({
                "continuation": created[0],
                "remaining": rng.randrange(int(motif_settings["min_chain_hops"]), int(motif_settings["max_chain_hops"]) + 1),
                "next_due": tx_time[index] + math.exp(rng.gauss(6.4, 0.9)),
                "episode_id": episode["episode_id"] if episode else None,
                "chain_id": f"chain-{len(active_chains):05d}",
            })

        def advance_chain(index: int, chain: dict[str, Any]) -> None:
            previous = chain["continuation"]
            total = previous["amount_sats"]
            fee, spendable = settle(total, rng.randrange(300, 1_700))
            peel_fraction = rng.uniform(float(motif_settings["min_peel_fraction"]), float(motif_settings["max_peel_fraction"]))
            peel = max(2_000, int(spendable * peel_fraction))
            if spendable - peel < 8_000:
                chain["remaining"] = 0
                schedule(previous, tx_time[index])  # release the reserved tail
                emit_normal(index, None)
                return
            created = emit(index, [previous], [spendable - peel, peel], fee=fee, family="peel_step",
                           episode_id=chain["episode_id"], in_surge=bool(chain["episode_id"]),
                           fast_bias=float(motif_settings["motif_fast_fraction"]), reserve_vouts=(0,))
            chain["continuation"] = created[0]
            chain["remaining"] -= 1
            chain["next_due"] = tx_time[index] + math.exp(rng.gauss(6.4, 0.9))
            if chain["remaining"] <= 0:
                schedule(chain["continuation"], tx_time[index])

        # ---- Fixed Phase 4.1 anchor controls.
        # These keep the deterministic detector tests anchored on known indexes
        # while everything after them is drawn from the realistic process.
        anchors = _emit_anchor_controls(
            emit=emit, take=take, tx_time=tx_time, clock=clock, rng=rng, seed=seed,
            first_index=bootstrap, special=special,
        )
        anchor_end = bootstrap + anchors["transaction_count"]

        for index in range(anchor_end, config["transaction_count"]):
            episode = in_episode(index)
            clock.set_burst_multiplier(episode["arrival_rate_multiplier"] if episode else 1.0)
            tx_time[index] = clock.advance()
            now = tx_time[index]
            group = _group_for(index, ranges)

            due_chains = [chain for chain in active_chains if chain["remaining"] > 0 and chain["next_due"] <= now]
            if due_chains:
                advance_chain(index, due_chains[0])
                active_chains[:] = [chain for chain in active_chains if chain["remaining"] > 0]
                continue

            base_rate = float(motif_settings["baseline_rate"])
            if group["review_pattern"]:
                base_rate *= float(motif_settings["review_group_multiplier"])
            rate = base_rate * (episode["motif_rate_multiplier"] if episode else 1.0)
            roll = rng.random()
            if roll < rate:
                family = episode["family"] if episode else ("coinjoin_like" if rng.random() < 0.45 else "peel_step")
                if family == "coinjoin_like":
                    emit_coinjoin(index, episode)
                else:
                    start_chain(index, episode)
            elif roll < rate + float(motif_settings["nearmiss_rate"]):
                kind = ("nearmiss_two_equal", "nearmiss_tolerance", "nearmiss_two_inputs")[rng.randrange(3)]
                emit_nearmiss(index, kind, episode)
            elif roll < rate + float(motif_settings["nearmiss_rate"]) + float(motif_settings["rule_positive_benign_rate"]):
                emit_rule_positive_benign(index, episode)
            elif roll < rate + float(motif_settings["nearmiss_rate"]) + float(motif_settings["rule_positive_benign_rate"]) + 0.045:
                emit_batch(index, episode, near=rng.random() < 0.35)
            elif roll < rate + float(motif_settings["nearmiss_rate"]) + float(motif_settings["rule_positive_benign_rate"]) + 0.075:
                emit_consolidation(index, episode)
            else:
                emit_normal(index, episode)
            active_chains[:] = [chain for chain in active_chains if chain["remaining"] > 0]

        # Duplicate raw source records are additional candidates only; canonical facts remain 100K.
        for ordinal, original in enumerate(duplicate_source_rows):
            duplicate = dict(original)
            duplicate["source_record_id"] = f"r-duplicate-{ordinal:03d}"
            duplicate["source_locator"] = f"record:{config['transaction_count'] + ordinal + 1}"
            if ingest_ndjson is not None:
                _write_ndjson(ingest_ndjson, duplicate)
            if csv_writer is not None:
                csv_writer.writerow(_csv_row(duplicate))
            if xml_handle is not None:
                xml_handle.write(_xml_record(duplicate) + "\n")
            if json_handle is not None:
                json_handle.write(("," if json_written else "") + compact(duplicate) + "\n")
                json_written += 1
            _write_ndjson(handles["duplicate_candidates.ndjson"], {
                "canonical_txid": original["txid"], "duplicate_source_record_id": duplicate["source_record_id"],
                "duplicate_of_source_record_id": original["source_record_id"], "source_locator": f"duplicate:{ordinal}",
            })
            counts["duplicate_candidates"] += 1
            counts["ingestion_rows"] += 1

        if xml_handle is not None:
            xml_handle.write("</records>\n")
        if json_handle is not None:
            json_handle.write("]\n")

        for index in range(config["network_observation_count"]):
            target_index = (index * 41) % config["transaction_count"]
            surge = in_episode(target_index) is not None
            observation = {
                "network": config["network"],
                "observation_id": f"obs-100k-{index:05d}", "txid": txid(seed, target_index),
                "observer_id": f"relay-{index % 11}", "endpoint_port": 8333,
                "direction": "seen_from" if index % 2 else "seen_to",
                "observer_received_at": iso(tx_time[target_index]),
                "observer_time_original": iso(tx_time[target_index]), "clock_quality": "estimated",
                "capture_scope": "synthetic relay/NAT-like observation; no origin or wallet ownership assertion",
                **context.fields(in_surge=surge), "source_locator": f"network_observation:{index}",
            }
            observation["endpoint_ip"] = observation["src_ip"]
            _write_ndjson(handles["network_observations.ndjson"], observation)
            counts["network_observations"] += 1

    positives = sorted(key for key, value in labelled.items() if value["family"] in POSITIVE_FAMILIES)
    truth = {
        "evaluation_only": True, "do_not_ingest": True, "generator_version": config["generator_version"],
        "split_groups": [
            {"group_id": group["group_id"], "split": group["split"], "scenario": group["scenario"],
             "review_pattern": group["review_pattern"], "transaction_index_start": start,
             "transaction_index_end_exclusive": end}
            for start, end, group in ranges
        ],
        "scenario_groups": sorted({group["scenario"] for group in config["split_groups"]}),
        # Phase 4.1 deterministic-detector anchors (unchanged contract).
        "expected_peeling_chain_transaction_ids": special["peel_control"],
        "expected_coinjoin_like_transaction_ids": special["coinjoin_control"],
        "synthetic_review_seed_address": special["seed_address"],
        "expected_two_hop_target_address": special["two_hop_target"],
        "disconnected_address": special["disconnected_address"],
        "expected_benign_controls": {
            "ordinary_sequence_transaction_ids": special["ordinary_sequence"],
            "ordinary_multi_output_transaction_ids": [special["ordinary_multi_txid"]],
        },
        # Full ML label set.  `family` is the ground-truth shape; `in_surge` marks
        # membership of a labelled burst episode.
        "labelled_transactions": labelled,
        "labelled_positive_transaction_ids": positives,
        "motif_episodes": [
            {**episode, "transaction_ids": special["episode_transactions"].get(episode["episode_id"], [])}
            for episode in episodes
        ],
        "family_counts": dict(sorted(family_counts.items())),
        "unspent_output_count": unspent_outputs,
        "raw_fixture_contains_no_labels_or_split_ids": True,
    }
    (destination / "evaluation_truth.json").write_text(compact(truth) + "\n", encoding="utf-8")
    generated = list(NDJSON_OUTPUTS) + ["evaluation_truth.json"]
    for fmt, filename in (("ndjson", "ingestion_rows.ndjson"), ("csv", "ingestion_rows.csv"),
                          ("xml", "ingestion_rows.xml"), ("json", "ingestion_rows.json")):
        if fmt in formats:
            generated.append(filename)
    manifest = {
        "schema_version": "1.0.0", "generator_version": config["generator_version"], "config_sha256": config_hash,
        "generator_sha256": file_hash(Path(__file__)), "seed": config["seed"], "network": config["network"],
        "counts": {
            **dict(counts), "canonical_transaction_count": counts["transactions"],
            "labelled_transactions": len(labelled), "labelled_positive_transactions": len(positives),
            "motif_episodes": len(episodes), "unspent_outputs": unspent_outputs,
            **{f"family_{name}": family_counts.get(name, 0) for name in MOTIF_FAMILIES},
        },
        "generated_files": {name: {"sha256": file_hash(destination / name), "bytes": (destination / name).stat().st_size} for name in generated},
        "invariants": {
            "canonical_transaction_count": counts["transactions"], "no_double_spends": True,
            "fully_known_value_conservation": True,
            "unresolved_prevouts_explicit_null": config["bootstrap_transaction_count"],
            "duplicate_candidates_do_not_increase_canonical_count": True, "raw_ownership_claims": False,
            "address_namespace_collision_free": True, "arrivals_are_non_homogeneous_poisson": True,
            "spend_latency_is_bimodal_with_censoring": True, "labelled_near_miss_negatives_present": True,
        },
        "provenance": {
            "config_locator": str(CONFIG_PATH.relative_to(ROOT.parents[1])),
            "source_locator_scheme": "record:<1-based-row>",
            "network_observations_are_off_chain_only": True,
        },
    }
    (destination / "fixture_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if validate_output:
        validate(destination, formats)
    return manifest


def _emit_anchor_controls(*, emit, take, tx_time, clock, rng, seed, first_index: int, special: dict[str, Any]) -> dict[str, Any]:
    """Fixed, hand-built controls for the Phase 4.1 deterministic detector tests.

    These are the only hand-placed transactions left.  Everything after them
    comes from the stochastic process, so the anchors do not carry the fixture's
    statistical weight the way v1's hand-placed scenarios did.  Every offset in
    the block emits exactly one transaction: a gap here would leave a hole in the
    canonical index space and break the 100,000-transaction invariant.
    """
    span = 50
    state: dict[str, Any] = {}

    def ordinary(index: int) -> None:
        selected = take(1, tx_time[index])
        total = selected[0]["amount_sats"]
        fee = min(400 + (index % 700), max(0, total - 1_000))
        emit(index, selected, [total - fee], fee=fee)

    for offset in range(span):
        index = first_index + offset
        tx_time[index] = clock.advance()

        if offset == 0:  # start of the verified three-hop peeling chain
            selected = take(1, tx_time[index])
            total = selected[0]["amount_sats"]
            state["peel"] = emit(index, selected, [total - 701 - 21_000, 21_000], fee=701, reserve_vouts=(0,))[0]
        elif offset in (1, 2, 3):
            fee = {1: 1_701, 2: 1_702, 3: 803}[offset]
            previous = state["peel"]
            state["peel"] = emit(index, [previous], [previous["amount_sats"] - fee - 18_000, 18_000], fee=fee,
                                 reserve_vouts=(0,) if offset < 3 else ())[0]
        elif offset == 10:  # ordinary two-step sequence: peeling negative control
            selected = take(1, tx_time[index])
            total = selected[0]["amount_sats"]
            state["ordinary"] = emit(index, selected, [total - 690 - 19_000, 19_000], fee=690, reserve_vouts=(0,))[0]
        elif offset == 11:
            previous = state["ordinary"]
            emit(index, [previous], [previous["amount_sats"] - 691 - 15_000, 15_000], fee=691)
        elif offset == 20:  # synthetic review seed with a verified two-hop path
            selected = take(1, tx_time[index])
            total = selected[0]["amount_sats"]
            seed_address = address(seed, "reserved", 1)
            state["seed_node"] = emit(index, selected, [total - 777 - 11_000, 11_000],
                                      output_addresses=[seed_address, address(seed, "reserved", 2)], fee=777,
                                      reserve_vouts=(0,))[0]
            special["seed_address"] = seed_address
        elif offset == 21:
            previous = state["seed_node"]
            state["seed_node"] = emit(index, [previous], [previous["amount_sats"] - 778 - 9_000, 9_000], fee=778,
                                      reserve_vouts=(0,))[0]
        elif offset == 22:
            previous = state["seed_node"]
            target = address(seed, "reserved", 3)
            emit(index, [previous], [previous["amount_sats"] - 779 - 8_000, 8_000],
                 output_addresses=[target, address(seed, "reserved", 4)], fee=779)
            special["two_hop_target"] = target
        elif offset == 30:  # equal-output control
            selected = take(3, tx_time[index])
            total, fee = sum(item["amount_sats"] for item in selected), 1_125
            equal = (total - fee - 13_007) // 3
            emit(index, selected, [equal, equal, equal, total - fee - equal * 3], fee=fee, family="coinjoin_like")
            special["coinjoin_control"] = [txid(seed, index)]
            special["coinjoin_txids"].append(txid(seed, index))
        elif offset == 31:  # ordinary multi-output: equal-output negative control
            selected = take(3, tx_time[index])
            total, fee = sum(item["amount_sats"] for item in selected), 1_126
            emit(index, selected, [total // 2, total // 3, total - fee - total // 2 - total // 3], fee=fee)
            special["ordinary_multi_txid"] = txid(seed, index)
        elif offset == 40:  # disconnected control address
            selected = take(1, tx_time[index])
            total = selected[0]["amount_sats"]
            disconnected = address(seed, "reserved", 5)
            emit(index, selected, [total - 880 - 10_000, 10_000],
                 output_addresses=[disconnected, address(seed, "reserved", 6)], fee=880)
            special["disconnected_address"] = disconnected
        else:
            ordinary(index)

    special["peel_control"] = [txid(seed, first_index + offset) for offset in (1, 2, 3)]
    special["ordinary_sequence"] = [txid(seed, first_index + 10), txid(seed, first_index + 11)]
    return {"transaction_count": span}


def validate(directory: Path, formats: tuple[str, ...] = ("csv", "ndjson")) -> None:
    """Streaming invariant and reconciliation checks suitable for the 100K local fixture."""
    config, _ = load_config()
    transactions: dict[str, int] = {}
    for row in ndjson_rows(directory / "transactions.ndjson"):
        if row["txid"] in transactions:
            raise ValueError("duplicate canonical transaction txid")
        transactions[row["txid"]] = int(row["fee_sats"] or 0)
    if len(transactions) != config["transaction_count"]:
        raise ValueError("fixture must contain exactly 100,000 unique canonical transactions")
    output_values: dict[tuple[str, int], int] = {}
    output_totals: Counter[str] = Counter()
    address_outpoints: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for row in ndjson_rows(directory / "outputs.ndjson"):
        key = (row["txid"], int(row["vout"]))
        if key in output_values:
            raise ValueError("duplicate output identity")
        output_values[key] = int(row["amount_sats"])
        output_totals[row["txid"]] += int(row["amount_sats"])
        address_outpoints[row["address"]].add(key)
    input_totals: Counter[str] = Counter()
    resolved_by_tx: Counter[str] = Counter()
    inputs_by_tx: Counter[str] = Counter()
    spent: set[tuple[str, int]] = set()
    unresolved = 0
    for row in ndjson_rows(directory / "inputs.ndjson"):
        current = row["txid"]
        inputs_by_tx[current] += 1
        previous_txid, previous_vout = row.get("prev_txid"), row.get("prev_vout")
        if previous_txid is None or previous_vout is None:
            if previous_txid is not None or previous_vout is not None:
                raise ValueError("unresolved prevout must use null txid and vout together")
            unresolved += 1
            continue
        key = (previous_txid, int(previous_vout))
        if key not in output_values:
            raise ValueError("known prevout was invented or unavailable")
        if key in spent:
            raise ValueError("double spend")
        spent.add(key)
        if row.get("amount_sats") != output_values[key]:
            raise ValueError("input amount does not match referenced output")
        input_totals[current] += output_values[key]
        resolved_by_tx[current] += 1
    for current, count in inputs_by_tx.items():
        if count == resolved_by_tx[current] and input_totals[current] != output_totals[current] + transactions[current]:
            raise ValueError("fully known transaction does not conserve satoshis")
    if unresolved != config["bootstrap_transaction_count"]:
        raise ValueError("unexpected unresolved prevout count")
    # v1's defining bug: the same address emitted by many bootstrap transactions.
    # Assert the namespace is injective for freshly minted (non-hot) addresses by
    # checking that no address is reused more than the hot pool can explain.
    reused = sum(1 for outpoints in address_outpoints.values() if len(outpoints) > 1)
    if reused > config["addresses"]["hot_address_count"] * 1.05:
        raise ValueError("address reuse exceeds the modelled hot pool; namespace may be colliding")

    manifest = json.loads((directory / "fixture_manifest.json").read_text(encoding="utf-8"))
    if manifest["counts"]["canonical_transaction_count"] != config["transaction_count"]:
        raise ValueError("manifest canonical count mismatch")
    expected_ingestion_rows = config["transaction_count"] + config["duplicate_record_count"]
    _validate_ingestion_formats(directory, formats, expected_ingestion_rows)
    duplicates = list(ndjson_rows(directory / "duplicate_candidates.ndjson"))
    if len(duplicates) != config["duplicate_record_count"]:
        raise ValueError("duplicate candidate count mismatch")
    _validate_truth(directory, config, manifest)


def _validate_ingestion_formats(directory: Path, formats: tuple[str, ...], expected: int) -> None:
    reference: list[tuple[str, str]] | None = None
    if "ndjson" in formats:
        reference = []
        for row in ndjson_rows(directory / "ingestion_rows.ndjson"):
            if not set(REQUIRED_RAW_FIELDS).issubset(row):
                raise ValueError("missing required SIH raw field in NDJSON")
            text = compact(row).lower()
            if "scenario" in text or "group_id" in text or "episode" in text:
                raise ValueError("evaluation labels leaked into raw ingestion")
            reference.append((row["txid"], row["source_record_id"]))
        if len(reference) != expected:
            raise ValueError("unexpected ingestion NDJSON count")
    if "csv" in formats:
        with (directory / "ingestion_rows.csv").open(encoding="utf-8", newline="") as handle:
            rows = [(row["txid"], row["source_record_id"]) for row in csv.DictReader(handle)
                    if set(REQUIRED_RAW_FIELDS).issubset(row)]
        if len(rows) != expected:
            raise ValueError("unexpected ingestion CSV count")
        if reference is not None and rows != reference:
            raise ValueError("CSV and NDJSON logical ingestion rows do not reconcile")
    if "json" in formats:
        payload = json.loads((directory / "ingestion_rows.json").read_text(encoding="utf-8"))
        rows = [(row["txid"], row["source_record_id"]) for row in payload]
        if len(rows) != expected:
            raise ValueError("unexpected ingestion JSON count")
        if reference is not None and rows != reference:
            raise ValueError("JSON and NDJSON logical ingestion rows do not reconcile")
    if "xml" in formats:
        # Stdlib parser on purpose: the generator must stay dependency-free, and
        # these are bytes this process just wrote.  Untrusted XML arriving through
        # ingestion is parsed by `defusedxml` in `app/engine/adapters/source.py`.
        from xml.etree import ElementTree

        rows = []
        for _, element in ElementTree.iterparse(str(directory / "ingestion_rows.xml"), events=("end",)):
            if element.tag != "record":
                continue
            found = {child.tag: (child.text or "") for child in element}
            if "txid" not in found or "source_record_id" not in found:
                raise ValueError("missing required SIH raw field in XML")
            rows.append((found["txid"], found["source_record_id"]))
            element.clear()
        if len(rows) != expected:
            raise ValueError("unexpected ingestion XML count")
        if reference is not None and rows != reference:
            raise ValueError("XML and NDJSON logical ingestion rows do not reconcile")


def _validate_truth(directory: Path, config: dict[str, Any], manifest: dict[str, Any]) -> None:
    truth = json.loads((directory / "evaluation_truth.json").read_text(encoding="utf-8"))
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise ValueError("evaluation truth must be marked non-ingestible")
    groups = truth["split_groups"]
    if len({group["group_id"] for group in groups}) != len(groups):
        raise ValueError("split groups overlap")
    split_order = {"train_reference": 0, "validation": 1, "final_holdout": 2}
    if any(split_order[groups[i]["split"]] > split_order[groups[i + 1]["split"]] for i in range(len(groups) - 1)):
        raise ValueError("split order is not time-respecting")
    if sum(group["transaction_index_end_exclusive"] - group["transaction_index_start"]
           for group in groups if group["split"] == "final_holdout") < 3000:
        raise ValueError("final holdout must contain at least 3,000 rows")
    minimum = int(config["motifs"]["minimum_labelled_positives"])
    if len(truth["labelled_positive_transaction_ids"]) < minimum:
        raise ValueError(
            f"fixture has {len(truth['labelled_positive_transaction_ids'])} labelled positives; "
            f"at least {minimum} are required for a metric to separate two candidates"
        )
    for family in ("nearmiss_two_equal", "nearmiss_tolerance", "nearmiss_two_inputs", RULE_POSITIVE_BENIGN):
        if truth["family_counts"].get(family, 0) < 50:
            raise ValueError(f"near-miss family {family} is too small to prove a graded model beats a threshold")
    if truth["family_counts"].get(RULE_POSITIVE_BENIGN, 0) < 200:
        raise ValueError(
            "too few rule-positive benign transactions: without them every near-miss is one "
            "predicate short of the deterministic rule, which excludes them all for free, and "
            "the discrimination task cannot distinguish a threshold from a model"
        )
    # Every split must carry positives, or validation/holdout metrics are undefined.
    boundaries = {group["split"]: (group["transaction_index_start"], group["transaction_index_end_exclusive"])
                  for group in groups}
    per_split: Counter[str] = Counter()
    for record in truth["labelled_transactions"].values():
        if record["family"] not in POSITIVE_FAMILIES:
            continue
        for group in groups:
            if group["transaction_index_start"] <= record["transaction_index"] < group["transaction_index_end_exclusive"]:
                per_split[group["split"]] += 1
                break
    for split in ("train_reference", "validation", "final_holdout"):
        if per_split[split] < 50:
            raise ValueError(f"split {split} carries only {per_split[split]} labelled positives")
    surges = [episode for episode in truth["motif_episodes"] if episode["transaction_ids"]]
    if len(surges) < int(config["motifs"]["minimum_surge_episodes"]):
        raise ValueError("too few realised motif surge episodes to validate a burst detector")
    if manifest["counts"]["unspent_outputs"] < 1000:
        raise ValueError("fixture needs right-censored (unspent) outputs for the latency layer")
    del boundaries


def verify(destination: Path, formats: tuple[str, ...]) -> None:
    """Validate generated bytes against this run's manifest and all invariants."""
    validate(destination, formats)
    manifest = json.loads((destination / "fixture_manifest.json").read_text(encoding="utf-8"))
    for filename, expected in manifest["generated_files"].items():
        target = destination / filename
        if not target.is_file() or file_hash(target) != expected["sha256"] or target.stat().st_size != expected["bytes"]:
            raise ValueError(f"manifest hash mismatch for {filename}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="local dataset directory")
    parser.add_argument("--formats", default="csv,ndjson", help=f"comma-separated ingestion formats: {','.join(SUPPORTED_FORMATS)}")
    parser.add_argument("--verify", action="store_true", help="validate every invariant and manifest hash")
    args = parser.parse_args()
    formats = tuple(item.strip() for item in args.formats.split(",") if item.strip())
    manifest = generate(args.output, formats)
    if args.verify:
        verify(args.output, formats)
    counts = manifest["counts"]
    print(
        f"Generated {counts['canonical_transaction_count']} canonical transactions at {args.output}\n"
        f"  labelled motifs   : {counts['labelled_transactions']} "
        f"({counts['labelled_positive_transactions']} positive)\n"
        f"  surge episodes    : {counts['motif_episodes']}\n"
        f"  unspent outputs   : {counts['unspent_outputs']} (right-censored)\n"
        f"  families          : " + ", ".join(f"{name}={counts[f'family_{name}']}" for name in MOTIF_FAMILIES)
    )


if __name__ == "__main__":
    main()
