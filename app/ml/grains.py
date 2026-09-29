"""Feature tables at the four grains the problem actually has.

The measured failure of the frozen Phase 4.1 table is that it keys everything on
`(address, 15-minute window)`.  Equal-output shape is a property of a
*transaction*, spend latency of an *outpoint*, surge of an *entity over time*,
and motif bursts of the *population over time*.  Collapsing four questions onto
one key is why a 4-output CoinJoin becomes four rows that each look like an
ordinary single receipt.

Every table below is built strictly causally: a row may only use facts committed
at or before its own timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.ml.facts import Facts

WINDOW_SECONDS = 900


@dataclass(slots=True)
class Table:
    """A feature matrix plus the column names that explain it."""

    columns: tuple[str, ...]
    matrix: np.ndarray

    def __post_init__(self) -> None:
        if self.matrix.shape[1] != len(self.columns):
            raise ValueError(f"matrix has {self.matrix.shape[1]} columns, {len(self.columns)} names given")


def _gini(values: np.ndarray) -> float:
    if values.size < 2:
        return 0.0
    ordered = np.sort(values)
    n = ordered.size
    total = ordered.sum()
    if total <= 0:
        return 0.0
    index = np.arange(1, n + 1)
    return float((2.0 * (index * ordered).sum()) / (n * total) - (n + 1.0) / n)


def _entropy(values: np.ndarray) -> float:
    total = values.sum()
    if total <= 0 or values.size < 2:
        return 0.0
    share = values / total
    share = share[share > 0]
    return float(-(share * np.log(share)).sum())


def _largest_equal_group(values: np.ndarray, tolerance: int) -> int:
    """Biggest set of outputs equal within `tolerance` satoshis.

    Graded tolerance is the point: a hard `tolerance == 0` predicate makes
    equal-output structure a yes/no rule, so a model can add nothing.  Scanning a
    sorted window lets a near-miss (three outputs within 500 sats) rank *below* a
    true equal set instead of vanishing.
    """
    if values.size == 0:
        return 0
    ordered = np.sort(values)
    best = 1
    left = 0
    for right in range(ordered.size):
        while ordered[right] - ordered[left] > tolerance:
            left += 1
        best = max(best, right - left + 1)
    return best


# --------------------------------------------------------------------------- #
# Grain A — transaction shape
# --------------------------------------------------------------------------- #
TRANSACTION_COLUMNS = (
    "n_inputs", "n_outputs", "log_n_inputs", "log_n_outputs",
    "equal_group_exact", "equal_group_tol_10", "equal_group_tol_100", "equal_group_tol_1000",
    "equal_group_fraction", "distinct_value_classes", "distinct_value_fraction",
    "output_value_entropy", "output_value_gini", "max_output_share", "second_to_max_ratio",
    "log_output_total", "log_output_max", "log_output_min", "log_output_median",
    "log_fee", "fee_share", "peel_ratio", "one_big_one_small",
    "round_output_count", "round_output_share", "script_type_count",
    "input_value_concentration", "log_input_total", "value_throughput_ratio",
    "spent_output_share", "input_age_min_log", "input_age_median_log", "rapid_input_share",
)


def build_transaction_table(facts: Facts) -> Table:
    """Shape features for every transaction.

    This is the grain the current pipeline has no table for at all, and it is the
    only one where equal-output structure and peel geometry are representable.
    """
    count = facts.transaction_count
    out_starts, out_order = facts.outputs_of()
    in_starts, in_order = facts.inputs_of()
    matrix = np.zeros((count, len(TRANSACTION_COLUMNS)), dtype=np.float32)

    value = facts.out_value
    script = facts.out_script
    spent = facts.out_spent_by >= 0
    tx_time = facts.tx_time
    prev_of_input = facts.in_prev

    for transaction in range(count):
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        ins = in_order[in_starts[transaction]:in_starts[transaction + 1]]
        values = value[outs].astype(np.float64)
        n_out = values.size
        resolved = prev_of_input[ins]
        resolved = resolved[resolved >= 0]
        n_in = resolved.size

        total = float(values.sum()) if n_out else 0.0
        ordered = np.sort(values)[::-1] if n_out else np.zeros(1)
        largest = float(ordered[0]) if n_out else 0.0
        second = float(ordered[1]) if n_out > 1 else 0.0
        classes = np.unique(values).size if n_out else 0

        input_values = value[resolved].astype(np.float64) if n_in else np.zeros(0)
        input_total = float(input_values.sum())
        fee = float(facts.tx_fee[transaction])

        if n_in:
            ages = (tx_time[transaction] - tx_time[facts.out_tx[resolved]]).astype(np.float64)
            ages = np.maximum(ages, 0.0)
        else:
            ages = np.zeros(0)

        row = matrix[transaction]
        row[0] = n_in
        row[1] = n_out
        row[2] = np.log1p(n_in)
        row[3] = np.log1p(n_out)
        row[4] = _largest_equal_group(values, 0)
        row[5] = _largest_equal_group(values, 10)
        row[6] = _largest_equal_group(values, 100)
        row[7] = _largest_equal_group(values, 1000)
        row[8] = row[4] / n_out if n_out else 0.0
        row[9] = classes
        row[10] = classes / n_out if n_out else 0.0
        row[11] = _entropy(values)
        row[12] = _gini(values)
        row[13] = largest / total if total > 0 else 0.0
        row[14] = second / largest if largest > 0 else 0.0
        row[15] = np.log1p(total)
        row[16] = np.log1p(largest)
        row[17] = np.log1p(float(ordered[-1])) if n_out else 0.0
        row[18] = np.log1p(float(np.median(values))) if n_out else 0.0
        row[19] = np.log1p(fee)
        row[20] = fee / input_total if input_total > 0 else 0.0
        # Peel geometry: one dominant continuation plus a small payment.
        row[21] = largest / input_total if input_total > 0 else 0.0
        row[22] = 1.0 if (n_out == 2 and largest > 0 and second / largest < 0.25) else 0.0
        rounds = int(np.sum([(v % 100_000 == 0) or (v % 1_000_000 == 0) for v in values])) if n_out else 0
        row[23] = rounds
        row[24] = rounds / n_out if n_out else 0.0
        row[25] = np.unique(script[outs]).size if n_out else 0
        row[26] = float(input_values.max() / input_total) if input_total > 0 else 0.0
        row[27] = np.log1p(input_total)
        row[28] = total / input_total if input_total > 0 else 0.0
        row[29] = float(spent[outs].mean()) if n_out else 0.0
        row[30] = np.log1p(float(ages.min())) if ages.size else 0.0
        row[31] = np.log1p(float(np.median(ages))) if ages.size else 0.0
        row[32] = float((ages <= 3600).mean()) if ages.size else 0.0
    return Table(TRANSACTION_COLUMNS, matrix)


# --------------------------------------------------------------------------- #
# Grain B — outpoint spend latency
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LatencyTable:
    """One row per output: how long it survived, and whether that was observed.

    `observed == False` means the output was still unspent at the end of the
    snapshot — right-censored, not slow.  Treating censored rows as slow is the
    mistake that makes a fixed 3,600-second predicate the only option.
    """

    values: np.ndarray        # output value in satoshis
    duration: np.ndarray      # seconds from creation to spend, or to snapshot end
    observed: np.ndarray      # bool: an actual spend was seen
    created_tx: np.ndarray    # int32 creating transaction
    spending_tx: np.ndarray   # int32 spending transaction, -1 when censored


def build_latency_table(facts: Facts) -> LatencyTable:
    created_at = facts.tx_time[facts.out_tx]
    spending = facts.out_spent_by
    observed = spending >= 0
    horizon = int(facts.tx_time.max())
    spent_at = np.where(observed, facts.tx_time[np.maximum(spending, 0)], horizon)
    duration = np.maximum(spent_at - created_at, 0).astype(np.float64)
    return LatencyTable(
        values=facts.out_value.astype(np.float64),
        duration=duration,
        observed=observed,
        created_tx=facts.out_tx,
        spending_tx=spending,
    )


# --------------------------------------------------------------------------- #
# Grain C — entity x window history
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class HistoryTable:
    """Per (address, window) rows with strictly-earlier baselines attached."""

    address: np.ndarray        # int32
    window: np.ndarray         # int64 window-start epoch
    event_count: np.ndarray    # distinct inbound transactions this window
    value_total: np.ndarray    # satoshis received this window
    prior_windows: np.ndarray  # how many earlier windows this address had
    prior_events: np.ndarray   # sum of earlier event counts
    prior_value: np.ndarray    # sum of earlier received value
    gap_seconds: np.ndarray    # seconds since this address's previous window, -1 if none
    prior_motif: np.ndarray    # count of earlier motif-flagged windows for this address
    transactions: list[np.ndarray]  # transaction indices contributing to each row


def build_history_table(facts: Facts, motif_flag: np.ndarray | None = None) -> HistoryTable:
    """Aggregate outputs into (address, window) rows and attach causal baselines.

    `motif_flag` is a per-transaction 0/1 vector from layer A.  Carrying it here
    is what produces *prior motif frequency* — a feature the current exporter has
    no way to express, because motif identity lives at a grain it never sees.
    """
    valid = facts.out_addr >= 0
    address = facts.out_addr[valid]
    transaction = facts.out_tx[valid]
    value = facts.out_value[valid]
    window = (facts.tx_time[transaction] // WINDOW_SECONDS) * WINDOW_SECONDS

    order = np.lexsort((window, address))
    address, transaction, value, window = address[order], transaction[order], value[order], window[order]

    # Row boundaries: a new row starts whenever (address, window) changes.
    new_row = np.empty(address.shape[0], dtype=bool)
    new_row[0] = True
    np.not_equal(address[1:], address[:-1], out=new_row[1:])
    new_row[1:] |= window[1:] != window[:-1]
    starts = np.flatnonzero(new_row)
    ends = np.append(starts[1:], address.shape[0])

    row_address = address[starts]
    row_window = window[starts]
    row_value = np.add.reduceat(value, starts)
    row_transactions = [np.unique(transaction[start:end]) for start, end in zip(starts, ends, strict=True)]
    row_events = np.array([block.size for block in row_transactions], dtype=np.float64)
    row_motif = np.array(
        [float(motif_flag[block].sum()) if motif_flag is not None else 0.0 for block in row_transactions],
        dtype=np.float64,
    )

    # Rows are already ordered by (address, window), so the running totals below
    # only ever see strictly-earlier windows of the same address.
    rows = row_address.shape[0]
    prior_windows = np.zeros(rows, dtype=np.float64)
    prior_events = np.zeros(rows, dtype=np.float64)
    prior_value = np.zeros(rows, dtype=np.float64)
    gap_seconds = np.full(rows, -1.0, dtype=np.float64)
    prior_motif = np.zeros(rows, dtype=np.float64)

    windows_seen = 0.0
    events_seen = 0.0
    value_seen = 0.0
    motif_seen = 0.0
    previous_window = -1
    for slot in range(rows):
        if slot > 0 and row_address[slot] != row_address[slot - 1]:
            windows_seen = events_seen = value_seen = motif_seen = 0.0
            previous_window = -1
        prior_windows[slot] = windows_seen
        prior_events[slot] = events_seen
        prior_value[slot] = value_seen
        prior_motif[slot] = motif_seen
        if previous_window >= 0:
            gap_seconds[slot] = float(row_window[slot] - previous_window)
        windows_seen += 1.0
        events_seen += row_events[slot]
        value_seen += float(row_value[slot])
        motif_seen += row_motif[slot]
        previous_window = int(row_window[slot])

    return HistoryTable(
        address=row_address,
        window=row_window,
        event_count=row_events,
        value_total=row_value.astype(np.float64),
        prior_windows=prior_windows,
        prior_events=prior_events,
        prior_value=prior_value,
        gap_seconds=gap_seconds,
        prior_motif=prior_motif,
        transactions=row_transactions,
    )


# --------------------------------------------------------------------------- #
# Grain D — population motif count series
# --------------------------------------------------------------------------- #
def build_motif_series(
    facts: Facts, family_flag: np.ndarray, *, bucket_seconds: int = WINDOW_SECONDS
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Count of flagged transactions per time bucket, plus each bucket's start.

    Returns `(bucket_start, counts, bucket_of_transaction)`.  This is the series
    a burst detector consumes: the literal reading of "sudden surge in CoinJoin
    and peeling" is a change point on this curve, not a per-address anomaly.
    """
    origin = int(facts.tx_time.min())
    bucket = ((facts.tx_time - origin) // bucket_seconds).astype(np.int64)
    span = int(bucket.max()) + 1
    counts = np.bincount(bucket[family_flag > 0], minlength=span).astype(np.float64)
    bucket_start = origin + np.arange(span, dtype=np.int64) * bucket_seconds
    return bucket_start, counts, bucket


# --------------------------------------------------------------------------- #
# Grain E — bounded graph neighbourhood
# --------------------------------------------------------------------------- #
GRAPH_COLUMNS = (
    "in_degree", "out_degree", "degree_asymmetry", "distinct_prev_tx", "distinct_next_tx",
    "chain_depth_back", "chain_depth_forward", "neighbour_value_in", "neighbour_value_out",
    "recipient_reuse_share", "endpoint_cooccurrence", "asn_cooccurrence", "country_rarity",
)


def build_graph_table(facts: Facts, *, window_seconds: int = 3600) -> Table:
    """Bounded k<=2 UTXO-graph features plus the PS network-context aggregates.

    The exporter's `bounded_component_change` proxy for this is perfectly
    collinear with `received_output_count`, so it adds nothing.  Real degree,
    chain depth and endpoint co-occurrence are all cheap and all currently unused.

    Endpoint and ASN co-occurrence stay *observations*: how many distinct
    transactions shared a relay endpoint in the same hour.  They never create an
    ownership edge and never assert origin.
    """
    count = facts.transaction_count
    out_starts, out_order = facts.outputs_of()
    in_starts, in_order = facts.inputs_of()
    matrix = np.zeros((count, len(GRAPH_COLUMNS)), dtype=np.float32)

    prev_tx_of_output = facts.out_tx
    # Backward depth: longest verified spend chain ending at this transaction,
    # capped at 2 hops so this stays a bounded neighbourhood, not a traversal.
    back_depth = np.zeros(count, dtype=np.float32)
    forward_depth = np.zeros(count, dtype=np.float32)

    address_use = np.bincount(facts.out_addr[facts.out_addr >= 0], minlength=len(facts.addresses))

    for transaction in range(count):
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        ins = in_order[in_starts[transaction]:in_starts[transaction + 1]]
        resolved = facts.in_prev[ins]
        resolved = resolved[resolved >= 0]
        parents = np.unique(prev_tx_of_output[resolved]) if resolved.size else np.zeros(0, dtype=np.int32)
        children = facts.out_spent_by[outs]
        children = np.unique(children[children >= 0])

        row = matrix[transaction]
        row[0] = resolved.size
        row[1] = outs.size
        row[2] = (outs.size - resolved.size) / max(outs.size + resolved.size, 1)
        row[3] = parents.size
        row[4] = children.size
        if parents.size:
            back_depth[transaction] = min(2.0, 1.0 + float(back_depth[parents].max()))
        row[5] = back_depth[transaction]
        row[7] = np.log1p(float(facts.out_value[resolved].sum())) if resolved.size else 0.0
        row[8] = np.log1p(float(facts.out_value[outs].sum())) if outs.size else 0.0
        addresses = facts.out_addr[outs]
        addresses = addresses[addresses >= 0]
        row[9] = float((address_use[addresses] > 1).mean()) if addresses.size else 0.0

    # Forward depth needs a reverse pass, so it is filled after back_depth exists.
    for transaction in range(count - 1, -1, -1):
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        children = facts.out_spent_by[outs]
        children = children[children >= 0]
        if children.size:
            forward_depth[transaction] = min(2.0, 1.0 + float(forward_depth[np.unique(children)].max()))
        matrix[transaction, 6] = forward_depth[transaction]

    if facts.tx_asn is not None and facts.tx_src_ip is not None and facts.tx_country is not None:
        bucket = (facts.tx_time // window_seconds).astype(np.int64)
        matrix[:, 10] = _cooccurrence(facts.tx_src_ip, bucket)
        matrix[:, 11] = _cooccurrence(facts.tx_asn, bucket)
        frequency = np.bincount(facts.tx_country[facts.tx_country >= 0])
        share = frequency / max(frequency.sum(), 1)
        rarity = np.zeros(count, dtype=np.float32)
        known = facts.tx_country >= 0
        rarity[known] = -np.log(np.clip(share[facts.tx_country[known]], 1e-9, 1.0))
        matrix[:, 12] = rarity
    return Table(GRAPH_COLUMNS, matrix)


def _cooccurrence(key: np.ndarray, bucket: np.ndarray) -> np.ndarray:
    """How many transactions shared this key inside this time bucket."""
    valid = key >= 0
    pair = np.where(valid, key.astype(np.int64) * (bucket.max() + 1) + bucket, -1)
    unique, inverse, counts = np.unique(pair, return_inverse=True, return_counts=True)
    result = counts[inverse].astype(np.float32)
    result[~valid] = 0.0
    if unique.size and unique[0] == -1:
        result[pair == -1] = 0.0
    return np.log1p(result)
