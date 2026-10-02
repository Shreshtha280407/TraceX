#!/usr/bin/env python3
"""TraceX synthetic dataset generator: millions of PS-shaped rows with ground truth.

    python generator.py --rows 3000000 --output datasets/synthetic_3m
    python generator.py --rows 300000  --output datasets/synthetic_300k --formats ndjson,csv --verify

Needs only Python 3.11+ (standard library): every contributor gets byte-identical
output for the same --seed and --rows, on any machine.

How the data is built
---------------------
1. **Base traffic** -- the dataset is made of 100K-transaction *parts*. Each part
   is a full run of the labelled fixture generator (fixtures/phase5a_100k/
   generate.py) with its own seed, so every part has fresh, independent
   randomness: non-homogeneous Poisson arrivals with a diurnal cycle, heavy-
   tailed address reuse, bimodal spend latency with censoring, exchange
   batches, treasury transfers, consolidations, peeling chains and CoinJoin-
   like transactions in labelled surge episodes, and benign near-misses that
   sit deliberately close to each motif. Parts run in parallel.

2. **Injected investigation scenarios** -- every part then receives labelled
   end-to-end scenarios for each pattern the PS names (each with ground truth):

   * ransomware: victims pay collection addresses -> multi-round consolidation
     (common-input-ownership links the operator's addresses) -> peeling chain ->
     CoinJoin with unrelated participants -> exchange cash-out;
   * darknet market: many buyers pay rotating deposit addresses -> periodic
     hot-wallet sweeps (one relay endpoint for every sweep) -> vendor payouts;
   * layering: one source split into many outputs, each re-spent within
     minutes (rapid redistribution) and moved on again;
   * extortion: a burst of small payments swept almost immediately;
   * illicit wallets (address -> wallet id), seed (reported) illicit addresses
     and every downstream tainted address with its hop distance;
   * network layer: each illicit operator broadcasts from one dedicated
     *public* IP (relay concentration); ~35% of operators also report spoofed
     country / ASN metadata that contradicts the open Geo-IP databases
     (DB-IP country lite, IPtoASN);
   * benign controls that look similar on purpose: merchant consolidations,
     payroll fan-outs and an exchange that sweeps its deposit addresses.

3. **Merge** -- parts are placed back to back in time (45 days each) to form
   one continuous history. Splits are by time: the first 80% of parts are
   `train_reference`, the next 10% `validation`, the last 10% `final_holdout`.

Outputs (in --output)
---------------------
* ingestion_rows.ndjson (+ .csv with --formats ndjson,csv) -- the file to import
* transactions / inputs / outputs / network_observations .ndjson -- canonical facts
  used by the offline evaluation harness (scripts/run_anomaly_stack.py)
* evaluation_truth.json -- labels, splits, scenarios, entity / risk / network
  truth. EVALUATION ONLY: never import it (`do_not_ingest`)
* dataset_manifest.json -- seed, counts, per-file SHA-256

The data is synthetic: no real seized data, no real people. Public IPs are
real routable ranges used only so the offline Geo-IP lookup has something to
resolve; they say nothing about who operates them.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BASE_GENERATOR = ROOT / "fixtures" / "phase5a_100k" / "generate.py"
GENERATOR_VERSION = "3.0.0"
PART_TRANSACTIONS = 100_000
PART_SPAN = timedelta(days=45)
BASE_EPOCH = datetime(2026, 5, 1, tzinfo=UTC)
SEQUENCE = 4_294_967_293

#: Public IPv4 /24s and the country / ASN the open Geo-IP databases assign them
#: (DB-IP country lite + IPtoASN snapshot, June 2026). Honest observations
#: report exactly these; spoofed ones report something else.
PUBLIC_RELAYS = (
    ("81.2.69", "GB", 20712), ("8.8.4", "US", 15169), ("9.9.9", "US", 19281), ("185.220.101", "DE", 60729),
    ("45.155.205", "RU", 208677), ("104.16.0", "CA", 13335), ("151.101.1", "CA", 54113),
    ("13.107.42", "US", 8068), ("52.95.110", "US", 16509), ("34.117.59", "US", 396982),
    ("157.240.1", "AU", 32934), ("77.88.55", "RU", 208398),
    ("94.100.180", "RU", 47764), ("195.154.122", "FR", 12876), ("51.15.0", "NL", 12876),
    ("178.62.0", "GB", 14061), ("139.59.0", "IN", 14061), ("165.227.0", "US", 14061),
    ("46.101.0", "GB", 14061), ("210.140.92", "JP", 4694), ("180.149.59", "IN", 55824),
    ("200.160.2", "BR", 22548), ("196.216.2", "ZA", 33764), ("197.248.0", "KE", 37061),
    ("102.89.0", "NG", 29465), ("62.149.128", "IT", 31034), ("80.67.169", "FR", 20766),
    ("176.9.0", "DE", 24940), ("88.198.0", "DE", 24940),
)
SPOOF_COUNTRIES = ("PA", "SC", "VG", "CH", "SG", "AE", "CY", "MT")
DOC_COUNTRIES = ("US", "DE", "IN", "NL", "SG", "JP", "GB", "FR", "BR", "AE", "CA", "ZA")
SCRIPT_TYPES = (("p2wpkh", 0.62), ("p2tr", 0.24), ("p2sh", 0.14))

#: Scenario instances per 100K-transaction part (scaled by --scenario-scale).
SCENARIOS_PER_PART = {
    "ransomware": 5, "darknet_market": 2, "layering": 4, "extortion": 4,
    "benign_merchant": 4, "benign_payroll": 3,
}
ILLICIT_KINDS = {"ransomware", "darknet_market", "layering", "extortion"}


def _load_base():
    spec = importlib.util.spec_from_file_location("tracex_fixture_generate", BASE_GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tracex_fixture_generate"] = module
    spec.loader.exec_module(module)
    return module


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _shift(value: str | None, offset: timedelta) -> str | None:
    if not value:
        return value
    return _iso(datetime.fromisoformat(value) + offset)


def _btc(sats: int) -> str:
    return f"{sats // 100_000_000}.{sats % 100_000_000:08d}"


# --------------------------------------------------------------------------- #
# Scenario injection
# --------------------------------------------------------------------------- #


@dataclass
class Utxo:
    txid: str
    vout: int
    address: str
    amount: int


@dataclass
class Tx:
    txid: str
    moment: datetime
    inputs: list[Utxo]
    outputs: list[tuple[str, int]]
    fee: int
    family: str
    role: str
    scenario: str
    illicit: bool
    net: dict[str, Any]
    script_type: str


@dataclass
class Injector:
    rng: random.Random
    seed: str
    start: datetime
    end: datetime
    txs: list[Tx] = field(default_factory=list)
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    entity_truth: dict[str, str] = field(default_factory=dict)
    counter: int = 0
    exchange: dict[str, Any] | None = None

    # ---- identifiers -----------------------------------------------------
    def _token(self, tag: str) -> str:
        self.counter += 1
        return f"{self.seed}:{tag}:{self.counter}"

    def address(self, tag: str) -> str:
        return "bcrt1q" + hashlib.blake2b(self._token(tag).encode(), digest_size=20).hexdigest()

    def txid(self, tag: str) -> str:
        return hashlib.sha256(self._token(tag).encode()).hexdigest()

    def when(self, low: float = 0.05, high: float = 0.85) -> datetime:
        span = (self.end - self.start).total_seconds()
        return self.start + timedelta(seconds=span * self.rng.uniform(low, high))

    def script(self) -> str:
        roll, total = self.rng.random(), 0.0
        for name, weight in SCRIPT_TYPES:
            total += weight
            if roll <= total:
                return name
        return SCRIPT_TYPES[0][0]

    # ---- network context -------------------------------------------------
    def benign_net(self) -> dict[str, Any]:
        rng = self.rng
        octet = 1 + rng.randrange(254)
        source = f"198.51.100.{octet}" if rng.random() < 0.53 else f"203.0.113.{octet}"
        return {
            "src_ip": source, "src_port": 8333 if rng.random() < 0.78 else 18_333,
            "dst_ip": f"198.51.100.{1 + rng.randrange(254)}", "dst_port": 8333,
            "geo_country": DOC_COUNTRIES[rng.randrange(len(DOC_COUNTRIES))], "asn": f"AS{64512 + rng.randrange(180)}",
        }

    def operator_relay(self) -> dict[str, Any]:
        prefix, country, asn = PUBLIC_RELAYS[self.rng.randrange(len(PUBLIC_RELAYS))]
        relay = {"ip": f"{prefix}.{2 + self.rng.randrange(250)}", "db_country": country, "db_asn": asn,
                 "spoofed": self.rng.random() < 0.35}
        if relay["spoofed"]:
            relay["reported_country"] = self.rng.choice([c for c in SPOOF_COUNTRIES if c != country])
            relay["reported_asn"] = 64_512 + self.rng.randrange(1, 900)
        else:
            relay["reported_country"], relay["reported_asn"] = country, asn
        return relay

    @staticmethod
    def relay_net(relay: dict[str, Any], rng: random.Random) -> dict[str, Any]:
        return {
            "src_ip": relay["ip"], "src_port": 8333, "dst_ip": f"203.0.113.{1 + rng.randrange(254)}", "dst_port": 8333,
            "geo_country": relay["reported_country"], "asn": f"AS{relay['reported_asn']}",
        }

    # ---- transactions ----------------------------------------------------
    def fund(self, moment: datetime, outputs: list[tuple[str, int]], scenario: str, role: str = "funding") -> list[Utxo]:
        tx = Tx(self.txid("fund"), moment, [], outputs, 0, "scenario_funding", role, scenario, False,
                self.benign_net(), self.script())
        self.txs.append(tx)
        return [Utxo(tx.txid, vout, address, amount) for vout, (address, amount) in enumerate(outputs)]

    def spend(
        self, moment: datetime, inputs: list[Utxo], outputs: list[tuple[str, int]], *, family: str, role: str,
        scenario: str, illicit: bool, net: dict[str, Any], fee: int | None = None,
    ) -> list[Utxo]:
        fee = fee if fee is not None else self.rng.randrange(300, 2_500)
        total = sum(item.amount for item in inputs)
        assigned = sum(amount for _, amount in outputs)
        if assigned + fee > total:
            raise ValueError("scenario transaction would create value")
        tx = Tx(self.txid(role), moment, inputs, outputs, total - assigned, family, role, scenario, illicit, net,
                self.script())
        self.txs.append(tx)
        return [Utxo(tx.txid, vout, address, amount) for vout, (address, amount) in enumerate(outputs)]

    # ---- shared exchange -------------------------------------------------
    def exchange_deposit(self) -> str:
        if self.exchange is None:
            self.exchange = {"id": f"{self.seed}:exchange", "deposits": [], "utxos": []}
        address = self.address("exchange-deposit")
        self.exchange["deposits"].append(address)
        self.entity_truth[address] = self.exchange["id"]
        return address

    def receive_at_exchange(self, utxos: list[Utxo]) -> None:
        if self.exchange is None:
            self.exchange_deposit()
        self.exchange["utxos"].extend(utxos)

    def finish_exchange(self) -> None:
        """The exchange sweeps its deposit addresses daily (benign consolidation)."""
        if self.exchange is None:
            return
        rng = self.rng
        scenario = f"{self.seed}:exchange"
        # Benign customer deposits so cash-outs are not the only exchange inflow.
        for _ in range(rng.randrange(40, 90)):
            address = self.exchange_deposit()
            moment = self.when(0.02, 0.9)
            self.receive_at_exchange(self.fund(moment, [(address, rng.randrange(200_000, 40_000_000))], scenario,
                                               role="customer_deposit"))
        hot = self.address("exchange-hot")
        self.entity_truth[hot] = self.exchange["id"]
        # One sweep per day of every deposit received that day (>= 2 inputs);
        # a lone deposit waits for the next day's sweep.
        by_day: dict[int, list[Utxo]] = defaultdict(list)
        for utxo in self.exchange["utxos"]:
            by_day[int((self._time_of(utxo.txid) - self.start).total_seconds() // 86_400)].append(utxo)
        waiting: list[Utxo] = []
        for day in sorted(by_day):
            batch = waiting + by_day[day]
            if len(batch) < 2:
                waiting = batch
                continue
            waiting = []
            moment = max(self._time_of(u.txid) for u in batch) + timedelta(minutes=rng.randrange(5, 180))
            total = sum(u.amount for u in batch)
            self.spend(moment, batch, [(hot, total - 3_000)], family="exchange_sweep", role="exchange_sweep",
                       scenario=scenario, illicit=False, net=self.benign_net(), fee=3_000)
        self.scenarios.append({"scenario_id": scenario, "kind": "exchange", "illicit": False,
                               "wallets": {"exchange": self.exchange["id"]}})

    def _time_of(self, txid: str) -> datetime:
        if not hasattr(self, "_times"):
            self._times = {}
        if txid not in self._times:
            self._times.update({tx.txid: tx.moment for tx in self.txs})
        return self._times[txid]

    # ---- scenarios -------------------------------------------------------
    def ransomware(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:ransomware-{index}"
        relay = self.operator_relay()
        operator = f"{sid}:operator"
        collection = [self.address("ransom-collect") for _ in range(rng.randrange(1, 4))]
        for address in collection:
            self.entity_truth[address] = operator
        start = self.when(0.03, 0.55)
        victims: list[Utxo] = []
        txids: list[str] = []
        for _ in range(rng.randrange(12, 41)):
            moment = start + timedelta(seconds=rng.uniform(0, 3 * 86_400))
            ransom = rng.randrange(5_000_000, 150_000_000)
            victim = self.address("victim")
            funded = self.fund(moment - timedelta(hours=rng.uniform(1, 30)), [(victim, ransom + rng.randrange(40_000, 2_000_000))], sid, role="victim_funding")
            paid = self.spend(moment, funded, [(rng.choice(collection), ransom), (self.address("victim-change"), funded[0].amount - ransom - 1_500)],
                              family="ransom_payment", role="victim_payment", scenario=sid, illicit=True,
                              net=self.benign_net(), fee=1_500)
            victims.append(paid[0])
            txids.append(self.txs[-1].txid)
        victims.sort(key=lambda u: self._time_of(u.txid))
        # Consolidation rounds: each round also spends the previous round's
        # output, so common-input-ownership links every operator address.
        rounds = rng.randrange(2, 5)
        size = max(2, math.ceil(len(victims) / rounds))
        carry: Utxo | None = None
        moment = self._time_of(victims[-1].txid)
        for round_index in range(rounds):
            batch = victims[round_index * size:(round_index + 1) * size]
            if carry is not None:
                batch = [carry, *batch]
            if not batch:
                continue
            moment = max(self._time_of(u.txid) for u in batch) + timedelta(hours=rng.uniform(1, 18))
            target = self.address("ransom-consolidated")
            self.entity_truth[target] = operator
            total = sum(u.amount for u in batch)
            carry = self.spend(moment, batch, [(target, total - 4_000)], family="illicit_consolidation",
                               role="consolidation", scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=4_000)[0]
            txids.append(self.txs[-1].txid)
        tainted: dict[str, int] = {}
        current, hop = carry, 0
        for hop in range(1, rng.randrange(4, 9) + 1):
            moment += timedelta(minutes=rng.uniform(4, 600))
            peel = int(current.amount * rng.uniform(0.02, 0.09))
            destination = self.exchange_deposit() if rng.random() < 0.4 else self.address("mule")
            change = self.address("peel-change")
            self.entity_truth[change] = operator
            fee = rng.randrange(400, 2_000)
            outputs = self.spend(moment, [current], [(destination, peel), (change, current.amount - peel - fee)],
                                 family="peel_step", role="peel_hop", scenario=sid, illicit=True,
                                 net=self.relay_net(relay, rng), fee=fee)
            txids.append(self.txs[-1].txid)
            tainted[destination] = hop
            if destination in self.entity_truth:
                self.receive_at_exchange([outputs[0]])
            current = outputs[1]
        # CoinJoin with unrelated participants; the operator keeps one equal output.
        moment += timedelta(minutes=rng.uniform(10, 240))
        participants = rng.randrange(3, 7)
        denomination = max(100_000, (current.amount // 2) // 100_000 * 100_000)
        others = [self.fund(moment - timedelta(hours=rng.uniform(1, 24)),
                            [(self.address("cj-participant"), denomination + rng.randrange(20_000, 3_000_000))], sid,
                            role="coinjoin_participant_funding")[0] for _ in range(participants)]
        inputs = [current, *others]
        fee = 600 * len(inputs)
        mixed = self.address("mixed")
        self.entity_truth[mixed] = operator
        outputs = [(mixed, denomination)] + [(self.address("cj-out"), denomination) for _ in others]
        remainder = sum(u.amount for u in inputs) - denomination * len(inputs) - fee
        outputs.append((self.address("cj-change"), remainder))
        cj = self.spend(moment, inputs, outputs, family="coinjoin_like", role="coinjoin", scenario=sid, illicit=True,
                        net=self.benign_net(), fee=fee)
        txids.append(self.txs[-1].txid)
        tainted[mixed] = hop + 1
        moment += timedelta(hours=rng.uniform(2, 48))
        deposit = self.exchange_deposit()
        cash = self.spend(moment, [cj[0]], [(deposit, cj[0].amount - 1_200)], family="cashout", role="cashout",
                          scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=1_200)
        txids.append(self.txs[-1].txid)
        tainted[deposit] = hop + 2
        self.receive_at_exchange(cash)
        self.scenarios.append({
            "scenario_id": sid, "kind": "ransomware", "illicit": True, "transaction_ids": txids,
            "seed_addresses": collection, "tainted_addresses": tainted, "wallets": {"operator": operator},
            "relay": relay,
        })

    def darknet_market(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:darknet-{index}"
        relay = self.operator_relay()
        market = f"{sid}:market"
        deposits = [self.address("market-deposit") for _ in range(rng.randrange(6, 16))]
        for address in deposits:
            self.entity_truth[address] = market
        start = self.when(0.03, 0.4)
        span = timedelta(days=rng.uniform(6, 12))
        txids: list[str] = []
        received: list[Utxo] = []
        for _ in range(rng.randrange(40, 121)):
            moment = start + span * rng.random()
            price = rng.randrange(200_000, 5_000_000)
            buyer = self.address("buyer")
            funded = self.fund(moment - timedelta(hours=rng.uniform(0.5, 48)), [(buyer, price + rng.randrange(20_000, 900_000))], sid, role="buyer_funding")
            paid = self.spend(moment, funded, [(rng.choice(deposits), price), (self.address("buyer-change"), funded[0].amount - price - 900)],
                              family="darknet_payment", role="buyer_payment", scenario=sid, illicit=True,
                              net=self.benign_net(), fee=900)
            received.append(paid[0])
            txids.append(self.txs[-1].txid)
        received.sort(key=lambda u: self._time_of(u.txid))
        sweeps = rng.randrange(6, 11)
        hot: Utxo | None = None
        cursor = start
        step = span / sweeps
        for _ in range(sweeps):
            cursor += step
            batch = [u for u in received if self._time_of(u.txid) < cursor]
            received = [u for u in received if self._time_of(u.txid) >= cursor]
            inputs = ([hot] if hot else []) + batch
            if len(inputs) < 2:
                continue
            target = self.address("market-hot")
            self.entity_truth[target] = market
            total = sum(u.amount for u in inputs)
            hot = self.spend(cursor + timedelta(minutes=rng.uniform(5, 90)), inputs, [(target, total - 3_500)],
                             family="darknet_sweep", role="hot_wallet_sweep", scenario=sid, illicit=True,
                             net=self.relay_net(relay, rng), fee=3_500)[0]
            txids.append(self.txs[-1].txid)
        tainted: dict[str, int] = {}
        if hot is not None:
            vendors = rng.randrange(5, 16)
            share = hot.amount // (vendors + 2)
            payouts = [(self.address("vendor"), int(share * rng.uniform(0.5, 1.4))) for _ in range(vendors)]
            change = self.address("market-hot")
            self.entity_truth[change] = market
            payouts.append((change, hot.amount - sum(a for _, a in payouts) - 5_000))
            self.spend(self._time_of(hot.txid) + timedelta(hours=rng.uniform(1, 12)), [hot], payouts, family="darknet_payout",
                       role="vendor_payout", scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=5_000)
            txids.append(self.txs[-1].txid)
            tainted = {address: 1 for address, _ in payouts[:-1]}
        self.scenarios.append({
            "scenario_id": sid, "kind": "darknet_market", "illicit": True, "transaction_ids": txids,
            "seed_addresses": deposits[:3], "tainted_addresses": tainted, "wallets": {"market": market}, "relay": relay,
        })

    def layering(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:layering-{index}"
        relay = self.operator_relay()
        operator = f"{sid}:operator"
        moment = self.when(0.05, 0.8)
        source = self.address("layering-source")
        self.entity_truth[source] = operator
        funded = self.fund(moment - timedelta(hours=rng.uniform(1, 72)), [(source, rng.randrange(200_000_000, 2_000_000_000))], sid, role="layering_funding")[0]
        branches = rng.randrange(10, 26)
        piece = (funded.amount - 10_000) // branches
        first = [(self.address("layer-1"), piece) for _ in range(branches)]
        for address, _ in first:
            self.entity_truth[address] = operator
        layer1 = self.spend(moment, [funded], first, family="layering_fanout", role="split", scenario=sid, illicit=True,
                            net=self.relay_net(relay, rng), fee=funded.amount - piece * branches)
        txids = [self.txs[-1].txid]
        tainted: dict[str, int] = {}
        for utxo in layer1:
            hop_time = moment + timedelta(minutes=rng.uniform(1, 30))
            nxt = self.address("layer-2")
            self.entity_truth[nxt] = operator
            second = self.spend(hop_time, [utxo], [(nxt, utxo.amount - 800)], family="rapid_redistribution",
                                role="rapid_respend", scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=800)[0]
            txids.append(self.txs[-1].txid)
            deposit = self.exchange_deposit()
            cash = self.spend(hop_time + timedelta(minutes=rng.uniform(5, 240)), [second], [(deposit, second.amount - 900)],
                              family="cashout", role="cashout", scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=900)
            txids.append(self.txs[-1].txid)
            tainted[deposit] = 3
            self.receive_at_exchange(cash)
        self.scenarios.append({
            "scenario_id": sid, "kind": "layering", "illicit": True, "transaction_ids": txids,
            "seed_addresses": [source], "tainted_addresses": tainted, "wallets": {"operator": operator}, "relay": relay,
        })

    def extortion(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:extortion-{index}"
        relay = self.operator_relay()
        operator = f"{sid}:operator"
        target = self.address("extortion")
        self.entity_truth[target] = operator
        start = self.when(0.05, 0.85)
        paid: list[Utxo] = []
        txids: list[str] = []
        for _ in range(rng.randrange(5, 13)):
            moment = start + timedelta(minutes=rng.uniform(0, 600))
            amount = rng.randrange(1_000_000, 10_000_000)
            payer = self.address("extortion-payer")
            funded = self.fund(moment - timedelta(hours=rng.uniform(1, 12)), [(payer, amount + 50_000)], sid, role="payer_funding")
            paid.append(self.spend(moment, funded, [(target, amount), (self.address("payer-change"), 50_000 - 700)],
                                   family="extortion_payment", role="payment", scenario=sid, illicit=True,
                                   net=self.benign_net(), fee=700)[0])
            txids.append(self.txs[-1].txid)
        last = max(self._time_of(u.txid) for u in paid)
        swept_to = self.address("extortion-sweep")
        self.entity_truth[swept_to] = operator
        swept = self.spend(last + timedelta(minutes=rng.uniform(10, 60)), paid, [(swept_to, sum(u.amount for u in paid) - 2_000)],
                           family="extortion_sweep", role="sweep", scenario=sid, illicit=True,
                           net=self.relay_net(relay, rng), fee=2_000)
        txids.append(self.txs[-1].txid)
        deposit = self.exchange_deposit()
        cash = self.spend(last + timedelta(hours=rng.uniform(2, 30)), swept, [(deposit, swept[0].amount - 1_000)],
                          family="cashout", role="cashout", scenario=sid, illicit=True, net=self.relay_net(relay, rng), fee=1_000)
        txids.append(self.txs[-1].txid)
        self.receive_at_exchange(cash)
        self.scenarios.append({
            "scenario_id": sid, "kind": "extortion", "illicit": True, "transaction_ids": txids,
            "seed_addresses": [target], "tainted_addresses": {swept_to: 1, deposit: 2},
            "wallets": {"operator": operator}, "relay": relay,
        })

    def benign_merchant(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:merchant-{index}"
        merchant = f"{sid}:merchant"
        addresses = [self.address("merchant") for _ in range(rng.randrange(3, 7))]
        for address in addresses:
            self.entity_truth[address] = merchant
        start = self.when(0.03, 0.5)
        received: list[Utxo] = []
        txids: list[str] = []
        for _ in range(rng.randrange(30, 81)):
            moment = start + timedelta(days=rng.uniform(0, 14))
            price = rng.randrange(100_000, 20_000_000)
            customer = self.address("customer")
            funded = self.fund(moment - timedelta(hours=rng.uniform(1, 72)), [(customer, price + rng.randrange(30_000, 900_000))], sid, role="customer_funding")
            received.append(self.spend(moment, funded, [(rng.choice(addresses), price), (self.address("customer-change"), funded[0].amount - price - 800)],
                                       family="benign_purchase", role="customer_payment", scenario=sid, illicit=False,
                                       net=self.benign_net(), fee=800)[0])
            txids.append(self.txs[-1].txid)
        received.sort(key=lambda u: self._time_of(u.txid))
        treasury = self.address("merchant-treasury")
        self.entity_truth[treasury] = merchant
        for week in range(1, 4):
            cursor = start + timedelta(days=7 * week)
            batch = [u for u in received if self._time_of(u.txid) < cursor]
            received = [u for u in received if self._time_of(u.txid) >= cursor]
            if len(batch) >= 2:
                self.spend(cursor + timedelta(days=rng.uniform(0.5, 2)), batch, [(treasury, sum(u.amount for u in batch) - 3_000)],
                           family="benign_merchant_consolidation", role="weekly_consolidation", scenario=sid,
                           illicit=False, net=self.benign_net(), fee=3_000)
                txids.append(self.txs[-1].txid)
        self.scenarios.append({"scenario_id": sid, "kind": "benign_merchant", "illicit": False, "transaction_ids": txids,
                               "wallets": {"merchant": merchant}})

    def benign_payroll(self, index: int) -> None:
        rng, sid = self.rng, f"{self.seed}:payroll-{index}"
        employer = f"{sid}:employer"
        source = self.address("employer")
        self.entity_truth[source] = employer
        txids: list[str] = []
        for month in range(2):
            moment = self.when(0.05, 0.9)
            staff = rng.randrange(15, 41)
            salaries = [rng.randrange(20, 200) * 100_000 for _ in range(staff)]
            funded = self.fund(moment - timedelta(days=1), [(source, sum(salaries) + 50_000)], sid, role="employer_funding")
            self.spend(moment, funded, [(self.address("employee"), salary) for salary in salaries],
                       family="benign_payroll", role="payroll", scenario=sid, illicit=False, net=self.benign_net(),
                       fee=50_000)
            txids.append(self.txs[-1].txid)
        self.scenarios.append({"scenario_id": sid, "kind": "benign_payroll", "illicit": False, "transaction_ids": txids,
                               "wallets": {"employer": employer}})


# --------------------------------------------------------------------------- #
# One part: base generator + injected scenarios, merged in time order
# --------------------------------------------------------------------------- #


def _raw_row(tx: Tx, locator: str) -> dict[str, Any]:
    if tx.inputs:
        structured = [{"prev_txid": u.txid, "prev_vout": u.vout, "address": u.address, "amount_sats": u.amount,
                       "sequence": SEQUENCE} for u in tx.inputs]
    else:
        structured = [{"prev_txid": None, "prev_vout": None, "address": None, "amount_sats": None, "sequence": SEQUENCE}]
    return {
        "source_record_id": f"r-{locator}", "source_locator": f"record:{locator}", "timestamp": _iso(tx.moment),
        "network": "bitcoin-regtest", "txid": tx.txid,
        "input_addresses": [u.address for u in tx.inputs], "output_addresses": [a for a, _ in tx.outputs],
        "input_amounts": [_btc(u.amount) for u in tx.inputs], "output_amounts": [_btc(v) for _, v in tx.outputs],
        "fee": _btc(tx.fee), "script_type": tx.script_type, "inputs": structured,
        "outputs": [{"address": a, "amount_sats": v, "script_type": tx.script_type} for a, v in tx.outputs],
        **tx.net,
    }


class _Peek:
    def __init__(self, path: Path) -> None:
        self.handle = path.open(encoding="utf-8")
        self.line = self.handle.readline()

    def take_while(self, predicate) -> list[str]:
        out = []
        while self.line and predicate(self.line):
            out.append(self.line)
            self.line = self.handle.readline()
        return out

    def close(self) -> None:
        self.handle.close()


def _txid_of(line: str) -> str:
    start = line.index('"txid":"') + 8
    return line[start:start + 64]


def build_part(args: tuple[int, int, str, str, float]) -> dict[str, Any]:
    """Generate part `index` into `work/part-NNN/` and return its summary."""
    index, _parts, seed, work, scale = args
    base = _load_base()
    part_seed = f"{seed}-part-{index:03d}"
    config, config_hash = base.load_config()
    config = {**config, "seed": part_seed}
    base.load_config = lambda: (config, config_hash)
    directory = Path(work) / f"part-{index:03d}"
    raw_dir = directory / "base"
    shutil.rmtree(directory, ignore_errors=True)
    base.generate(raw_dir, ("ndjson",), validate_output=False)
    offset = PART_SPAN * index
    truth = json.loads((raw_dir / "evaluation_truth.json").read_text(encoding="utf-8"))

    times = [datetime.fromisoformat(json.loads(line)["block_time"])
             for line in (raw_dir / "transactions.ndjson").open(encoding="utf-8") if line.strip()]
    injector = Injector(random.Random(f"{part_seed}:scenarios"), part_seed, min(times), max(times))
    for kind, count in SCENARIOS_PER_PART.items():
        for number in range(max(1, round(count * scale))):
            getattr(injector, kind)(number)
    injector.finish_exchange()
    injected = sorted(injector.txs, key=lambda tx: (tx.moment, tx.txid))

    out = {name: (directory / name).open("w", encoding="utf-8") for name in (
        "ingestion_rows.ndjson", "transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson")}
    readers = {name: _Peek(raw_dir / name) for name in ("ingestion_rows.ndjson", "transactions.ndjson", "inputs.ndjson",
                                                          "outputs.ndjson")}
    labelled_base = truth["labelled_transactions"]
    position = 0
    positions: dict[str, int] = {}
    base_position: dict[int, int] = {}
    injected_cursor = 0

    def write_injected(tx: Tx) -> None:
        nonlocal position
        moment = tx.moment + offset
        shifted = Tx(tx.txid, moment, tx.inputs, tx.outputs, tx.fee, tx.family, tx.role, tx.scenario, tx.illicit,
                     tx.net, tx.script_type)
        out["ingestion_rows.ndjson"].write(_compact(_raw_row(shifted, f"{index}-inj-{position}")) + "\n")
        out["transactions.ndjson"].write(_compact({"block_time": _iso(moment), "fee_sats": tx.fee, "network": "bitcoin-regtest",
                                                   "source_locator": f"transaction:{index}-inj-{position}", "txid": tx.txid}) + "\n")
        if tx.inputs:
            for vin, utxo in enumerate(tx.inputs):
                out["inputs.ndjson"].write(_compact({"address": utxo.address, "amount_sats": utxo.amount, "prev_txid": utxo.txid,
                                                     "prev_vout": utxo.vout, "sequence": SEQUENCE, "source_locator": f"input:{tx.txid[:12]}:{vin}",
                                                     "txid": tx.txid, "vin": vin}) + "\n")
        else:
            out["inputs.ndjson"].write(_compact({"address": None, "amount_sats": None, "prev_txid": None, "prev_vout": None,
                                                 "sequence": SEQUENCE, "source_locator": f"input:{tx.txid[:12]}:0",
                                                 "txid": tx.txid, "vin": 0}) + "\n")
        for vout, (address, amount) in enumerate(tx.outputs):
            out["outputs.ndjson"].write(_compact({"address": address, "amount_sats": amount, "script_type": tx.script_type,
                                                  "source_locator": f"output:{tx.txid[:12]}:{vout}", "txid": tx.txid,
                                                  "vout": vout}) + "\n")
        positions[tx.txid] = position
        position += 1

    for base_index, line in enumerate(iter(lambda: readers["transactions.ndjson"].line, "")):
        record = json.loads(line)
        readers["transactions.ndjson"].line = readers["transactions.ndjson"].handle.readline()
        moment = datetime.fromisoformat(record["block_time"])
        while injected_cursor < len(injected) and injected[injected_cursor].moment < moment:
            write_injected(injected[injected_cursor])
            injected_cursor += 1
        txid = record["txid"]
        record["block_time"] = _iso(moment + offset)
        out["transactions.ndjson"].write(_compact(record) + "\n")
        for name in ("inputs.ndjson", "outputs.ndjson"):
            for item in readers[name].take_while(lambda value, txid=txid: _txid_of(value) == txid):
                out[name].write(item)
        rows = readers["ingestion_rows.ndjson"].take_while(lambda value, txid=txid: _txid_of(value) == txid)
        for row_line in rows:
            row = json.loads(row_line)
            row["timestamp"] = _shift(row.get("timestamp"), offset)
            out["ingestion_rows.ndjson"].write(_compact(row) + "\n")
        positions[txid] = position
        base_position[base_index] = position
        position += 1
    while injected_cursor < len(injected):
        write_injected(injected[injected_cursor])
        injected_cursor += 1
    # The base generator appends its duplicate source rows (same txid, new
    # record id) after every canonical row; keep them, time-shifted, at the end.
    for row_line in readers["ingestion_rows.ndjson"].take_while(lambda value: True):
        row = json.loads(row_line)
        row["timestamp"] = _shift(row.get("timestamp"), offset)
        row["source_record_id"] = f"{row['source_record_id']}-p{index:03d}"
        out["ingestion_rows.ndjson"].write(_compact(row) + "\n")
    for reader in readers.values():
        reader.close()
    for line in (raw_dir / "network_observations.ndjson").open(encoding="utf-8"):
        row = json.loads(line)
        row["observer_received_at"] = _shift(row.get("observer_received_at"), offset)
        row["observer_time_original"] = _shift(row.get("observer_time_original"), offset)
        row["observation_id"] = f"p{index:03d}-{row['observation_id']}"
        out["network_observations.ndjson"].write(_compact(row) + "\n")
    for handle in out.values():
        handle.close()

    labelled = {
        txid: {**record, "transaction_index": positions[txid], "illicit_flow": False, "source": "base_fixture",
               "episode_id": f"p{index:03d}-{record['episode_id']}" if record.get("episode_id") else None}
        for txid, record in labelled_base.items()
    }
    for tx in injector.txs:
        if tx.family == "scenario_funding":
            continue
        labelled[tx.txid] = {"family": tx.family, "role": tx.role, "episode_id": tx.scenario, "in_surge": False,
                             "illicit_flow": tx.illicit, "source": "injected_scenario",
                             "transaction_index": positions[tx.txid]}
    episodes = []
    for episode in truth["motif_episodes"]:
        start = base_position.get(episode["transaction_index_start"], position)
        end = base_position.get(episode["transaction_index_end_exclusive"] - 1, position - 1) + 1
        episodes.append({**episode, "episode_id": f"p{index:03d}-{episode['episode_id']}",
                         "transaction_index_start": start, "transaction_index_end_exclusive": end})
    groups = [{**group, "transaction_index_start": base_position[group["transaction_index_start"]],
               "transaction_index_end_exclusive": (base_position.get(group["transaction_index_end_exclusive"])
                                                   if group["transaction_index_end_exclusive"] in base_position else position)}
              for group in truth["split_groups"]]
    part_truth = {
        "labelled_transactions": labelled, "motif_episodes": episodes, "split_groups": groups,
        "scenarios": injector.scenarios, "entity_truth": injector.entity_truth,
        "anchors": {key: truth[key] for key in (
            "expected_peeling_chain_transaction_ids", "expected_coinjoin_like_transaction_ids",
            "synthetic_review_seed_address", "expected_two_hop_target_address", "disconnected_address",
            "expected_benign_controls") if key in truth},
    }
    (directory / "part_truth.json").write_text(_compact(part_truth), encoding="utf-8")
    shutil.rmtree(raw_dir, ignore_errors=True)
    return {"index": index, "transactions": position, "injected": len(injector.txs),
            "scenarios": Counter(s["kind"] for s in injector.scenarios)}


# --------------------------------------------------------------------------- #
# Merge, truth, manifest, verification
# --------------------------------------------------------------------------- #


def _split_of(index: int, parts: int) -> str:
    """Time-ordered splits by part: ~80% train, ~10% validation, ~10% holdout,
    always at least one validation and one holdout part once there are 3+."""
    if parts < 3:
        return "train_reference"
    held = max(1, round(parts * 0.1))
    train = parts - 2 * held
    if index < train:
        return "train_reference"
    return "validation" if index < train + held else "final_holdout"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge(output: Path, work: Path, summaries: list[dict[str, Any]], *, formats: tuple[str, ...], seed: str) -> dict:
    parts = len(summaries)
    names = ("ingestion_rows.ndjson", "transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson")
    handles = {name: (output / name).open("w", encoding="utf-8") for name in names}
    csv_handle = None
    if "csv" in formats:
        import csv

        base = _load_base()
        csv_handle = (output / "ingestion_rows.csv").open("w", encoding="utf-8", newline="")
        csv_writer = csv.DictWriter(csv_handle, fieldnames=base.INGESTION_COLUMNS, lineterminator="\n")
        csv_writer.writeheader()
    labelled: dict[str, Any] = {}
    episodes: list[dict] = []
    split_groups: list[dict] = []
    scenarios: list[dict] = []
    entity_truth: dict[str, str] = {}
    anchors: dict[str, Any] = {}
    offset = 0
    rows = 0
    for summary in sorted(summaries, key=lambda item: item["index"]):
        directory = work / f"part-{summary['index']:03d}"
        for name in names:
            with (directory / name).open(encoding="utf-8") as source:
                if name == "ingestion_rows.ndjson":
                    for line in source:
                        handles[name].write(line)
                        rows += 1
                        if csv_handle is not None:
                            csv_writer.writerow(base._csv_row(json.loads(line)))
                else:
                    shutil.copyfileobj(source, handles[name], 1 << 22)
        part = json.loads((directory / "part_truth.json").read_text(encoding="utf-8"))
        split = _split_of(summary["index"], parts)
        for txid, record in part["labelled_transactions"].items():
            labelled[txid] = {**record, "transaction_index": record["transaction_index"] + offset}
        for episode in part["motif_episodes"]:
            episodes.append({**episode, "split": split if parts >= 3 else episode["split"],
                             "transaction_index_start": episode["transaction_index_start"] + offset,
                             "transaction_index_end_exclusive": episode["transaction_index_end_exclusive"] + offset})
        if parts >= 3:
            split_groups.append({"group_id": f"part-{summary['index']:03d}", "split": split, "scenario": "mixed",
                                 "review_pattern": True, "transaction_index_start": offset,
                                 "transaction_index_end_exclusive": offset + summary["transactions"]})
        else:
            split_groups.extend({**group, "transaction_index_start": group["transaction_index_start"] + offset,
                                 "transaction_index_end_exclusive": group["transaction_index_end_exclusive"] + offset}
                                for group in part["split_groups"])
        for scenario in part["scenarios"]:
            scenarios.append({**scenario, "split": split})
        entity_truth.update(part["entity_truth"])
        if not anchors:
            anchors = part["anchors"]
        offset += summary["transactions"]
        shutil.rmtree(directory, ignore_errors=True)
    for handle in handles.values():
        handle.close()
    if csv_handle is not None:
        csv_handle.close()

    illicit = [s for s in scenarios if s.get("illicit")]
    positives = sorted(txid for txid, record in labelled.items() if record["family"] in ("coinjoin_like", "peel_step"))
    truth = {
        "evaluation_only": True, "do_not_ingest": True, "generator_version": GENERATOR_VERSION, "seed": seed,
        "parts": parts, "split_groups": split_groups, "labelled_transactions": labelled,
        "labelled_positive_transaction_ids": positives, "motif_episodes": episodes,
        "family_counts": dict(sorted(Counter(record["family"] for record in labelled.values()).items())),
        "illicit_flow_transaction_count": sum(1 for record in labelled.values() if record.get("illicit_flow")),
        "scenarios": scenarios,
        "entity_truth": entity_truth,
        "risk_truth": {
            "seed_addresses": sorted({address for s in illicit for address in s.get("seed_addresses", [])}),
            "tainted_addresses": {address: hop for s in illicit for address, hop in s.get("tainted_addresses", {}).items()},
        },
        "network_truth": {
            "relay_concentration": [{"scenario_id": s["scenario_id"], "wallet": next(iter(s["wallets"].values())),
                                     "ip": s["relay"]["ip"]} for s in illicit],
            "geo_mismatch_ips": sorted({s["relay"]["ip"] for s in illicit if s["relay"]["spoofed"]}),
        },
        **anchors,
    }
    (output / "evaluation_truth.json").write_text(_compact(truth) + "\n", encoding="utf-8")
    files = [*names, "evaluation_truth.json"] + (["ingestion_rows.csv"] if csv_handle is not None else [])
    manifest = {
        "generator": "generator.py", "generator_version": GENERATOR_VERSION, "seed": seed, "parts": parts,
        "base_generator_sha256": _file_hash(BASE_GENERATOR), "generator_sha256": _file_hash(Path(__file__)),
        "counts": {
            "transactions": offset, "ingestion_rows": rows, "labelled_transactions": len(labelled),
            "labelled_motif_positives": len(positives), "illicit_flow_transactions": truth["illicit_flow_transaction_count"],
            "scenarios": dict(Counter(s["kind"] for s in scenarios)), "entity_truth_addresses": len(entity_truth),
            "seed_addresses": len(truth["risk_truth"]["seed_addresses"]),
            "tainted_addresses": len(truth["risk_truth"]["tainted_addresses"]),
            "relay_concentrations": len(truth["network_truth"]["relay_concentration"]),
            "geo_mismatch_ips": len(truth["network_truth"]["geo_mismatch_ips"]),
            "families": truth["family_counts"],
        },
        "splits": {split: sum(1 for group in split_groups if group["split"] == split)
                   for split in ("train_reference", "validation", "final_holdout")},
        "files": {name: {"bytes": (output / name).stat().st_size, "sha256": _file_hash(output / name)} for name in files},
    }
    (output / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify(output: Path) -> dict[str, int]:
    """Stream-check UTXO invariants over the merged ingestion file."""
    created: dict[tuple[str, int], int] = {}
    spent: set[tuple[str, int]] = set()
    txids: set[str] = set()
    checks = Counter()
    with (output / "ingestion_rows.ndjson").open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if str(row.get("source_record_id", "")).startswith("r-duplicate"):
                checks["duplicate_rows"] += 1
                continue
            txid = row["txid"]
            if txid in txids:
                raise ValueError(f"duplicate canonical txid {txid}")
            txids.add(txid)
            value_in, known = 0, True
            for item in row["inputs"]:
                if item.get("prev_txid") is None:
                    known = False
                    continue
                key = (item["prev_txid"], item["prev_vout"])
                if key in spent:
                    raise ValueError(f"double spend of {key} in {txid}")
                spent.add(key)
                if key not in created:
                    known = False
                    checks["unresolved_prevouts"] += 1
                    continue
                if created[key] != item["amount_sats"]:
                    raise ValueError(f"input amount mismatch for {key} in {txid}")
                value_in += item["amount_sats"]
            value_out = 0
            for vout, item in enumerate(row["outputs"]):
                created[(txid, vout)] = item["amount_sats"]
                value_out += item["amount_sats"]
            if known and row["inputs"] and value_in < value_out:
                raise ValueError(f"{txid} creates value")
            checks["transactions"] += 1
    return dict(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--rows", type=int, default=3_000_000, help="target transactions (rounded up to 100K parts)")
    parser.add_argument("--output", type=Path, default=ROOT / "datasets" / "synthetic_3m")
    parser.add_argument("--seed", default="tracex-sih-26146-v3")
    parser.add_argument("--formats", default="ndjson", help="ndjson and/or csv")
    parser.add_argument("--workers", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    parser.add_argument("--scenario-scale", type=float, default=1.0, help="multiply the injected scenarios per part")
    parser.add_argument("--verify", action="store_true", help="check every UTXO invariant of the merged file")
    args = parser.parse_args(argv)
    formats = tuple(part.strip() for part in args.formats.split(",") if part.strip())
    if not set(formats) <= {"ndjson", "csv"}:
        parser.error("--formats supports ndjson and csv")
    parts = max(1, math.ceil(args.rows / PART_TRANSACTIONS))
    args.output.mkdir(parents=True, exist_ok=True)
    work = args.output / ".parts"
    work.mkdir(exist_ok=True)
    started = time.time()
    print(f"generating {parts} part(s) of ~{PART_TRANSACTIONS:,} transactions with {args.workers} worker(s) -> {args.output}")
    jobs = [(index, parts, args.seed, str(work), args.scenario_scale) for index in range(parts)]
    summaries = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for summary in pool.map(build_part, jobs):
            summaries.append(summary)
            print(f"  part {summary['index'] + 1}/{parts}: {summary['transactions']:,} txs "
                  f"({summary['injected']:,} injected scenario txs)  [{time.time() - started:.0f}s]", flush=True)
    manifest = merge(args.output, work, summaries, formats=formats, seed=args.seed)
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(manifest["counts"], indent=2))
    if args.verify:
        print("verify:", verify(args.output))
    print(f"done in {time.time() - started:.0f}s; import {args.output / 'ingestion_rows.ndjson'} "
          f"(never import evaluation_truth.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
