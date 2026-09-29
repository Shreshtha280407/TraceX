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
from dataclasses import dataclass
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

    @property
    def transaction_count(self) -> int:
        return len(self.txids)

    def outputs_of(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR-style index of outputs grouped by transaction: (starts, order).

        `order` lists output indices sorted by transaction; `starts[t]:starts[t+1]`
        slices the outputs of transaction `t`.
        """
        order = np.argsort(self.out_tx, kind="stable").astype(np.int32)
        counts = np.bincount(self.out_tx, minlength=self.transaction_count)
        starts = np.zeros(self.transaction_count + 1, dtype=np.int64)
        np.cumsum(counts, out=starts[1:])
        return starts, order

    def inputs_of(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR-style index of inputs grouped by spending transaction."""
        order = np.argsort(self.in_tx, kind="stable").astype(np.int32)
        counts = np.bincount(self.in_tx, minlength=self.transaction_count)
        starts = np.zeros(self.transaction_count + 1, dtype=np.int64)
        np.cumsum(counts, out=starts[1:])
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
        slot = outpoint_index.get((tx_index[previous_txid], int(previous_vout)), -1)
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
