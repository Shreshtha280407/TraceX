"""Load the canonical fixture facts into compact integer-indexed arrays.

Everything downstream works on integer ids, not strings: `txid` and `address`
each get a dense index, and every table is a numpy array over those indices.
That keeps a 100K-transaction run in a few hundred megabytes instead of the
~5.7 GB the dict-of-dicts preparation path needs, which is what has to change
before this scales past the fixture.

Nothing here reads `evaluation_truth.json`.  Labels are loaded separately by
`app.ml.evaluate`, are never joined into a feature table, and never reach a fit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

# Raw fields the PS publishes that only exist on the ingestion rows.  Loading
# them is optional because the 100 MB NDJSON pass costs ~15 s and only layer E
# uses them.
NETWORK_FIELDS = ("src_ip", "dst_ip", "geo_country", "asn")


def _epoch(value: str) -> int:
    # Python 3.11+ parses the trailing "Z" directly.
    return int(datetime.fromisoformat(value).timestamp())


def _ndjson(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


@dataclass(slots=True)
class Facts:
    """Integer-indexed canonical facts for one snapshot of the fixture."""

    # --- transactions, in generation order -----------------------------------
    txids: list[str]
    tx_index: dict[str, int]
    tx_time: np.ndarray          # int64 epoch seconds
    tx_fee: np.ndarray           # int64 satoshis

    # --- outputs -------------------------------------------------------------
    out_tx: np.ndarray           # int32 transaction index
    out_vout: np.ndarray         # int32
    out_value: np.ndarray        # int64 satoshis
    out_addr: np.ndarray         # int32 address index
    out_script: np.ndarray       # int8 script-type index
    out_spent_by: np.ndarray     # int32 spending transaction index, -1 if unspent

    # --- inputs --------------------------------------------------------------
    in_tx: np.ndarray            # int32 spending transaction index
    in_prev: np.ndarray          # int32 output index, -1 when the prevout is unknown

    # --- addresses -----------------------------------------------------------
    addresses: list[str]
    addr_index: dict[str, int]

    # --- optional PS network context, per transaction ------------------------
    tx_asn: np.ndarray | None = None       # int32 ASN index, -1 when unknown
    tx_country: np.ndarray | None = None   # int32 country index
    tx_src_ip: np.ndarray | None = None    # int32 endpoint index
    asns: list[str] | None = None
    countries: list[str] | None = None
    src_ips: list[str] | None = None
    _indexes: dict = field(default_factory=dict, init=False, repr=False)

    def freeze_indexes(self):
        """Cache only immutable grouping arrays. Mutable research facts never cache."""
        self.out_tx.flags.writeable = False
        self.in_tx.flags.writeable = False
        return self

    @property
    def transaction_count(self) -> int:
        return len(self.txids)

    def outputs_of(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR-style index of outputs grouped by transaction: (starts, order).

        `order` lists output indices sorted by transaction; `starts[t]:starts[t+1]`
        slices the outputs of transaction `t`.
        """
        if not self.out_tx.flags.writeable and "outputs" in self._indexes:
            return self._indexes["outputs"]
        order = np.argsort(self.out_tx, kind="stable").astype(np.int32)
        counts = np.bincount(self.out_tx, minlength=self.transaction_count)
        starts = np.zeros(self.transaction_count + 1, dtype=np.int64)
        np.cumsum(counts, out=starts[1:])
        if not self.out_tx.flags.writeable:
            starts.flags.writeable = order.flags.writeable = False
            self._indexes["outputs"] = (starts, order)
        return starts, order

    def inputs_of(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR-style index of inputs grouped by spending transaction."""
        if not self.in_tx.flags.writeable and "inputs" in self._indexes:
            return self._indexes["inputs"]
        order = np.argsort(self.in_tx, kind="stable").astype(np.int32)
        counts = np.bincount(self.in_tx, minlength=self.transaction_count)
        starts = np.zeros(self.transaction_count + 1, dtype=np.int64)
        np.cumsum(counts, out=starts[1:])
        if not self.in_tx.flags.writeable:
            starts.flags.writeable = order.flags.writeable = False
            self._indexes["inputs"] = (starts, order)
        return starts, order


def load_facts(dataset: Path, *, with_network_context: bool = True) -> Facts:
    """Read `transactions/inputs/outputs.ndjson` into `Facts`.

    The three files are read once each, in order.  Outputs are resolved to their
    spending transaction here so no downstream layer has to re-scan inputs.
    """
    dataset = Path(dataset)
    txids: list[str] = []
    tx_index: dict[str, int] = {}
    times: list[int] = []
    fees: list[int] = []
    for row in _ndjson(dataset / "transactions.ndjson"):
        tx_index[row["txid"]] = len(txids)
        txids.append(row["txid"])
        times.append(_epoch(row["block_time"]))
        fees.append(int(row["fee_sats"] or 0))

    addresses: list[str] = []
    addr_index: dict[str, int] = {}
    scripts: list[str] = []
    script_index: dict[str, int] = {}
    out_tx: list[int] = []
    out_vout: list[int] = []
    out_value: list[int] = []
    out_addr: list[int] = []
    out_script: list[int] = []
    outpoint_index: dict[tuple[int, int], int] = {}
    for row in _ndjson(dataset / "outputs.ndjson"):
        transaction = tx_index[row["txid"]]
        address = row.get("address")
        if address is None:
            slot = -1
        else:
            slot = addr_index.get(address, -1)
            if slot < 0:
                slot = len(addresses)
                addr_index[address] = slot
                addresses.append(address)
        script = row.get("script_type") or ""
        script_slot = script_index.get(script, -1)
        if script_slot < 0:
            script_slot = len(scripts)
            script_index[script] = script_slot
            scripts.append(script)
        outpoint_index[(transaction, int(row["vout"]))] = len(out_tx)
        out_tx.append(transaction)
        out_vout.append(int(row["vout"]))
        out_value.append(int(row["amount_sats"]))
        out_addr.append(slot)
        out_script.append(script_slot)

    in_tx: list[int] = []
    in_prev: list[int] = []
    spent_by = np.full(len(out_tx), -1, dtype=np.int32)
    for row in _ndjson(dataset / "inputs.ndjson"):
        transaction = tx_index[row["txid"]]
        in_tx.append(transaction)
        previous_txid, previous_vout = row.get("prev_txid"), row.get("prev_vout")
        if previous_txid is None or previous_vout is None:
            in_prev.append(-1)
            continue
        slot = outpoint_index.get((tx_index.get(previous_txid, -1), int(previous_vout)), -1)
        in_prev.append(slot)
        if slot >= 0:
            spent_by[slot] = transaction

    facts = Facts(
        txids=txids,
        tx_index=tx_index,
        tx_time=np.asarray(times, dtype=np.int64),
        tx_fee=np.asarray(fees, dtype=np.int64),
        out_tx=np.asarray(out_tx, dtype=np.int32),
        out_vout=np.asarray(out_vout, dtype=np.int32),
        out_value=np.asarray(out_value, dtype=np.int64),
        out_addr=np.asarray(out_addr, dtype=np.int32),
        out_script=np.asarray(out_script, dtype=np.int8),
        out_spent_by=spent_by,
        in_tx=np.asarray(in_tx, dtype=np.int32),
        in_prev=np.asarray(in_prev, dtype=np.int32),
        addresses=addresses,
        addr_index=addr_index,
    )
    if with_network_context:
        _attach_network_context(facts, dataset)
    return facts


def _attach_network_context(facts: Facts, dataset: Path) -> None:
    """Attach the PS endpoint/geo fields from the raw ingestion rows.

    These are observations of a relay, never an ownership or origin claim.  They
    are loaded because the current exporter drops them entirely, which wastes
    four of the twelve fields the PS asks for.
    """
    source = dataset / "ingestion_rows.ndjson"
    if not source.is_file():
        return
    count = facts.transaction_count
    asns: list[str] = []
    countries: list[str] = []
    src_ips: list[str] = []
    asn_index: dict[str, int] = {}
    country_index: dict[str, int] = {}
    ip_index: dict[str, int] = {}
    tx_asn = np.full(count, -1, dtype=np.int32)
    tx_country = np.full(count, -1, dtype=np.int32)
    tx_src_ip = np.full(count, -1, dtype=np.int32)

    def intern(value: str | None, table: list[str], index: dict[str, int]) -> int:
        if value is None:
            return -1
        slot = index.get(value, -1)
        if slot < 0:
            slot = len(table)
            index[value] = slot
            table.append(value)
        return slot

    for row in _ndjson(source):
        transaction = facts.tx_index.get(row.get("txid", ""), -1)
        if transaction < 0 or tx_asn[transaction] >= 0:
            continue  # duplicate source records do not overwrite the canonical row
        tx_asn[transaction] = intern(row.get("asn"), asns, asn_index)
        tx_country[transaction] = intern(row.get("geo_country"), countries, country_index)
        tx_src_ip[transaction] = intern(row.get("src_ip"), src_ips, ip_index)
    facts.tx_asn, facts.tx_country, facts.tx_src_ip = tx_asn, tx_country, tx_src_ip
    facts.asns, facts.countries, facts.src_ips = asns, countries, src_ips


def split_boundaries(dataset: Path, truth: dict) -> tuple[int, int]:
    """Two epoch-second boundaries separating train / validation / final holdout.

    Derived from the real timestamps at the split-group index boundaries, exactly
    like `scripts/phase5b_prepare_features.py` does — never by re-deriving indices
    from the feature rows themselves, which would let row ordering define a split.
    """
    groups = truth["split_groups"]
    first_validation = min(g["transaction_index_start"] for g in groups if g["split"] == "validation")
    first_holdout = min(g["transaction_index_start"] for g in groups if g["split"] == "final_holdout")
    times: dict[int, int] = {}
    wanted = {first_validation, first_holdout}
    for position, row in enumerate(_ndjson(Path(dataset) / "transactions.ndjson")):
        if position in wanted:
            times[position] = _epoch(row["block_time"])
            if len(times) == len(wanted):
                break
    validation_start, holdout_start = times[first_validation], times[first_holdout]
    if not validation_start < holdout_start:
        raise ValueError("split boundaries are not time-ordered; refusing to proceed")
    return validation_start, holdout_start


def assign_splits(tx_time: np.ndarray, boundaries: tuple[int, int]) -> np.ndarray:
    """0 = train_reference, 1 = validation, 2 = final_holdout."""
    validation_start, holdout_start = boundaries
    splits = np.zeros(tx_time.shape[0], dtype=np.int8)
    splits[tx_time >= validation_start] = 1
    splits[tx_time >= holdout_start] = 2
    return splits


SPLIT_NAMES = ("train_reference", "validation", "final_holdout")


def facts_from_records(records: dict[str, list[dict]]) -> Facts:
    """Build `Facts` from committed canonical fragments held in memory."""
    return facts_from_streams(
        records.get("transactions", []), records.get("outputs", []), records.get("inputs", [])
    )


def facts_from_streams(transactions_in: Iterable[dict], outputs_in: Iterable[dict], inputs_in: Iterable[dict]) -> Facts:
    """Build `Facts` from committed canonical facts, each kind read once in order.

    The three iterables may be lazy (the bounded-memory path streams them from
    disk in chunks), so only the compact integer arrays below are ever held.

    `records` is exactly what `app.engine.graph.builder._facts` returns for a
    receipt-approved snapshot, so the stack can run on the same evidence the
    graph and the deterministic findings run on — case-scoped, source-located and
    coverage-bounded — instead of on files read off disk.

    Transactions are ordered by `(block_time, txid)` so the ordering is a property
    of the evidence rather than of fragment read order.
    """
    transactions = []
    for fact in transactions_in:
        stamp = fact.get("block_time") or fact.get("source_timestamp")
        if not stamp:
            continue  # no usable time: it cannot be placed in any causal window
        transactions.append((_epoch(stamp), str(fact["txid"]), int(fact.get("fee_sats") or 0)))
    transactions.sort()

    txids = [txid for _, txid, _ in transactions]
    tx_index = {txid: slot for slot, txid in enumerate(txids)}
    tx_time = np.asarray([stamp for stamp, _, _ in transactions], dtype=np.int64)
    tx_fee = np.asarray([fee for _, _, fee in transactions], dtype=np.int64)

    addresses: list[str] = []
    addr_index: dict[str, int] = {}
    script_index: dict[str, int] = {}
    out_tx: list[int] = []
    out_vout: list[int] = []
    out_value: list[int] = []
    out_addr: list[int] = []
    out_script: list[int] = []
    outpoint_index: dict[tuple[int, int], int] = {}
    for fact in outputs_in:
        transaction = tx_index.get(str(fact["txid"]), -1)
        if transaction < 0:
            continue
        # `script_id` is the fallback the deterministic engine already uses when an
        # output carries no address; keeping the same precedence keeps the two
        # views of the snapshot consistent.
        label = fact.get("address") or fact.get("script_id")
        if label is None:
            slot = -1
        else:
            slot = addr_index.setdefault(str(label), len(addresses))
            if slot == len(addresses):
                addresses.append(str(label))
        script = str(fact.get("script_type") or "")
        script_slot = script_index.setdefault(script, len(script_index))
        outpoint_index[(transaction, int(fact["vout"]))] = len(out_tx)
        out_tx.append(transaction)
        out_vout.append(int(fact["vout"]))
        out_value.append(int(fact["amount_sats"]))
        out_addr.append(slot)
        out_script.append(script_slot)

    in_tx: list[int] = []
    in_prev: list[int] = []
    spent_by = np.full(len(out_tx), -1, dtype=np.int32)
    for fact in inputs_in:
        transaction = tx_index.get(str(fact["txid"]), -1)
        if transaction < 0:
            continue
        in_tx.append(transaction)
        previous_txid, previous_vout = fact.get("prev_txid"), fact.get("prev_vout")
        previous = tx_index.get(str(previous_txid), -1) if previous_txid is not None else -1
        if previous < 0 or previous_vout is None:
            in_prev.append(-1)
            continue
        slot = outpoint_index.get((previous, int(previous_vout)), -1)
        in_prev.append(slot)
        if slot >= 0:
            spent_by[slot] = transaction

    return Facts(
        txids=txids, tx_index=tx_index, tx_time=tx_time, tx_fee=tx_fee,
        out_tx=np.asarray(out_tx, dtype=np.int32), out_vout=np.asarray(out_vout, dtype=np.int32),
        out_value=np.asarray(out_value, dtype=np.int64), out_addr=np.asarray(out_addr, dtype=np.int32),
        out_script=np.asarray(out_script, dtype=np.int8), out_spent_by=spent_by,
        in_tx=np.asarray(in_tx, dtype=np.int32), in_prev=np.asarray(in_prev, dtype=np.int32),
        addresses=addresses, addr_index=addr_index,
    )


def attach_network_observations(facts: Facts, observations: Iterable[dict]) -> None:
    """Attach the PS network fields (relay endpoint, ASN, reported country) from
    committed canonical `network_observation` facts, first observation per
    transaction. Observations of a relay only -- never an origin or owner."""
    count = facts.transaction_count
    tx_asn = np.full(count, -1, dtype=np.int32)
    tx_country = np.full(count, -1, dtype=np.int32)
    tx_src_ip = np.full(count, -1, dtype=np.int32)
    tables: dict[str, tuple[list[str], dict[str, int]]] = {"asn": ([], {}), "country": ([], {}), "ip": ([], {})}

    def intern(value, name: str) -> int:
        if value in (None, ""):
            return -1
        table, index = tables[name]
        value = str(value)
        slot = index.get(value, -1)
        if slot < 0:
            slot = len(table)
            index[value] = slot
            table.append(value)
        return slot

    seen = np.zeros(count, dtype=bool)
    for fact in observations:
        transaction = facts.tx_index.get(str(fact.get("txid") or ""), -1)
        if transaction < 0 or seen[transaction]:
            continue
        seen[transaction] = True
        tx_asn[transaction] = intern(fact.get("asn"), "asn")
        tx_country[transaction] = intern(fact.get("geo_country"), "country")
        tx_src_ip[transaction] = intern(fact.get("src_ip"), "ip")
    facts.tx_asn, facts.tx_country, facts.tx_src_ip = tx_asn, tx_country, tx_src_ip
    facts.asns, facts.countries, facts.src_ips = tables["asn"][0], tables["country"][0], tables["ip"][0]


def load_facts_from_snapshot(session, evidence_root, snapshot_id: str) -> Facts:
    """Read one receipt-approved snapshot's committed facts through the real path."""
    from app.engine.graph.builder import _facts

    return facts_from_records(_facts(session, evidence_root, snapshot_id))


def truncate_facts(facts: Facts, as_of: int) -> Facts:
    """A view of the snapshot as it looked at `as_of`, used to prove causality.

    Transactions after `as_of` are removed, and every spend edge they carried is
    removed with them — so an output spent only in the future becomes unspent,
    exactly as it would have looked at the time.  Recomputing features on this
    view and comparing against the full run is a property test that catches any
    future dependence by construction, rather than by reading the code.
    """
    keep = facts.tx_time <= as_of
    old_to_new = np.full(facts.transaction_count, -1, dtype=np.int32)
    old_to_new[keep] = np.arange(int(keep.sum()), dtype=np.int32)

    out_keep = keep[facts.out_tx]
    out_old_to_new = np.full(facts.out_tx.shape[0], -1, dtype=np.int32)
    out_old_to_new[out_keep] = np.arange(int(out_keep.sum()), dtype=np.int32)

    spent = facts.out_spent_by[out_keep]
    spent = np.where(spent >= 0, old_to_new[np.maximum(spent, 0)], -1).astype(np.int32)
    spent[facts.out_spent_by[out_keep] < 0] = -1

    in_keep = keep[facts.in_tx]
    previous = facts.in_prev[in_keep]
    previous = np.where(previous >= 0, out_old_to_new[np.maximum(previous, 0)], -1).astype(np.int32)

    kept_txids = [txid for txid, flag in zip(facts.txids, keep, strict=True) if flag]
    truncated = Facts(
        txids=kept_txids,
        tx_index={txid: slot for slot, txid in enumerate(kept_txids)},
        tx_time=facts.tx_time[keep], tx_fee=facts.tx_fee[keep],
        out_tx=old_to_new[facts.out_tx[out_keep]], out_vout=facts.out_vout[out_keep],
        out_value=facts.out_value[out_keep], out_addr=facts.out_addr[out_keep],
        out_script=facts.out_script[out_keep], out_spent_by=spent,
        in_tx=old_to_new[facts.in_tx[in_keep]], in_prev=previous,
        addresses=facts.addresses, addr_index=facts.addr_index,
    )
    for name in ("tx_asn", "tx_country", "tx_src_ip"):
        value = getattr(facts, name)
        if value is not None:
            setattr(truncated, name, value[keep])
    truncated.asns, truncated.countries, truncated.src_ips = facts.asns, facts.countries, facts.src_ips
    return truncated
