"""Feature tables at the four grains the problem actually has.

The measured failure of the frozen Phase 4.1 table is that it keys everything on
`(address, 15-minute window)`.  Equal-output shape is a property of a
*transaction*, spend latency of an *outpoint*, surge of an *entity over time*,
and motif bursts of the *population over time*.  Collapsing four questions onto
one key is why a 4-output CoinJoin becomes four rows that each look like an
ordinary single receipt.

Every table below is built **strictly causally**: a feature for transaction `t` may
only use facts whose timestamp is at or before `tx_time[t]`.  That is not a style
preference — a feature you cannot compute at observation time cannot be deployed,
so a model trained on one is unshippable no matter how well it scores offline.

Three classes of future leak were found and removed here, each of which had
inflated the reported precision:

* **outcome features** — "was this output later spent", "how many transactions
  later spent it", "how deep does the chain continue forward".  There is no causal
  version of these; they are gone.
* **whole-dataset statistics** — address reuse totals and country frequencies
  counted over the entire file, so an early transaction saw the whole future
  distribution.  These are now expanding-window counts over strictly-earlier rows.
* **containing-bucket aggregates** — endpoint/ASN co-occurrence counted inside the
  bucket holding the transaction, so a transaction early in the bucket saw
  transactions later in it.  These are now trailing windows, `[t - W, t)`.

`app.ml.facts.truncate_facts` plus `tests/unit/test_anomaly_stack.py` prove this by
property rather than by inspection: rebuild every table on a view of the snapshot
truncated at time T and assert the rows for transactions at or before T are
bit-identical to the full run.
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
    "input_age_min_log", "input_age_median_log", "rapid_input_share",
)
# NOTE: `spent_output_share` was removed. Whether a transaction's outputs are later
# spent is not knowable when the transaction is observed, and no causal substitute
# exists. Input *ages* below are the causal counterpart: they look backwards.


def _median_of_sorted(ordered: np.ndarray) -> float:
    """`np.median` for an already-sorted (either direction) non-empty 1-D array.

    Same arithmetic -- the middle element, or the float64 mean of the two middle
    elements -- without np.median's partition/reduce machinery, which dominated
    this per-transaction loop on the tiny arrays it is called with.
    """
    size = ordered.size
    middle = size // 2
    if size % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2)


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
        row[18] = np.log1p(_median_of_sorted(ordered)) if n_out else 0.0
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
        row[29] = np.log1p(float(ages.min())) if ages.size else 0.0
        row[30] = np.log1p(_median_of_sorted(np.sort(ages))) if ages.size else 0.0
        row[31] = float((ages <= 3600).mean()) if ages.size else 0.0
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


def build_latency_table(facts: Facts, *, horizon: int | None = None) -> LatencyTable:
    """Spend durations, censored at `horizon` (default: the snapshot's last time).

    Passing an earlier horizon yields the table *as it looked then*: a spend that
    happens after it is censored rather than observed.  Layer B fits on the
    reference period's horizon for exactly that reason — fitting on durations that
    run past the training period means the survival curve is shaped by events the
    model would not have seen yet.
    """
    created_at = facts.tx_time[facts.out_tx]
    spending = facts.out_spent_by
    horizon = int(facts.tx_time.max()) if horizon is None else int(horizon)
    spend_time = np.where(spending >= 0, facts.tx_time[np.maximum(spending, 0)], horizon + 1)
    observed = (spending >= 0) & (spend_time <= horizon)
    spent_at = np.where(observed, spend_time, horizon)
    duration = np.maximum(spent_at - created_at, 0).astype(np.float64)
    return LatencyTable(
        values=facts.out_value.astype(np.float64),
        duration=duration,
        observed=observed,
        created_tx=facts.out_tx,
        spending_tx=np.where(observed, spending, -1).astype(np.int32),
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
    "in_degree", "out_degree", "degree_asymmetry", "distinct_prev_tx",
    "chain_depth_back", "neighbour_value_in", "neighbour_value_out",
    "prior_recipient_reuse_mean", "prior_recipient_reuse_max", "prior_recipient_seen_share",
    "endpoint_cooccurrence_trailing", "asn_cooccurrence_trailing", "country_rarity_prior",
)
# Removed as non-causal: `distinct_next_tx` and `chain_depth_forward` both read
# `out_spent_by`, which records spends that had not happened yet, and
# `recipient_reuse_share`/`country_rarity` were counted over the whole dataset.


def _prior_occurrence_index(key: np.ndarray, order_time: np.ndarray, tiebreak: np.ndarray) -> np.ndarray:
    """For each row, how many earlier rows shared its key.

    Ordering is `(time, tiebreak)` so rows sharing a timestamp still get a total
    order, and the count is strictly *earlier* — a row never counts itself or a
    simultaneous sibling twice.
    """
    order = np.lexsort((tiebreak, order_time, key))
    sorted_key = key[order]
    position = np.arange(key.shape[0], dtype=np.int64)
    # Index of the first row of each key block, broadcast back over the block.
    starts = np.flatnonzero(np.append(True, sorted_key[1:] != sorted_key[:-1]))
    block_start = np.repeat(starts, np.diff(np.append(starts, key.shape[0])))
    prior_sorted = position - block_start
    prior = np.empty(key.shape[0], dtype=np.int64)
    prior[order] = prior_sorted
    return prior


def _trailing_cooccurrence(key: np.ndarray, time: np.ndarray, window: int) -> np.ndarray:
    """Count of strictly-earlier transactions sharing `key` within `[t - window, t)`.

    The previous implementation counted everything inside the fixed bucket holding
    the transaction, which let a transaction see its own bucket's future.
    """
    result = np.zeros(key.shape[0], dtype=np.float64)
    valid = np.flatnonzero(key >= 0)
    if valid.size == 0:
        return result
    order = valid[np.lexsort((time[valid], key[valid]))]
    sorted_key, sorted_time = key[order], time[order]
    starts = np.flatnonzero(np.append(True, sorted_key[1:] != sorted_key[:-1]))
    ends = np.append(starts[1:], order.shape[0])
    for start, stop in zip(starts, ends, strict=True):
        block_time = sorted_time[start:stop]
        # Rows strictly before t, minus rows before (t - window).
        before = np.searchsorted(block_time, block_time, side="left")
        outside = np.searchsorted(block_time, block_time - window, side="left")
        result[order[start:stop]] = before - outside
    return np.log1p(result)


def _expanding_rarity(key: np.ndarray, time: np.ndarray, tiebreak: np.ndarray, classes: int) -> np.ndarray:
    """-log of this key's share among strictly-earlier rows, Laplace-smoothed.

    Smoothing keeps the first rows defined instead of dividing by zero, and keeps
    the statistic comparable as the denominator grows.
    """
    prior_key = _prior_occurrence_index(key, time, tiebreak).astype(np.float64)
    order = np.lexsort((tiebreak, time))
    rank = np.empty(key.shape[0], dtype=np.float64)
    rank[order] = np.arange(key.shape[0], dtype=np.float64)
    share = (prior_key + 1.0) / (rank + max(classes, 1))
    rarity = -np.log(np.clip(share, 1e-9, 1.0))
    rarity[key < 0] = 0.0
    return rarity


def build_graph_table(facts: Facts, *, window_seconds: int = 3600) -> Table:
    """Bounded backward-only UTXO-graph features plus causal network context.

    Everything here looks backwards from the transaction: which prior outputs it
    consumed, how deep the verified chain behind it runs, how often its recipient
    addresses had already been seen, and how busy its relay endpoint had been in
    the preceding hour.  Nothing reads a spend that had not happened yet.

    Endpoint and ASN counts stay *observations* of a relay.  They never create an
    ownership edge and never assert transaction origin.
    """
    count = facts.transaction_count
    out_starts, out_order = facts.outputs_of()
    in_starts, in_order = facts.inputs_of()
    matrix = np.zeros((count, len(GRAPH_COLUMNS)), dtype=np.float32)
    prev_tx_of_output = facts.out_tx

    # Backward chain depth, capped at two hops so this stays a bounded
    # neighbourhood.  Transactions are processed in time order, so a parent's
    # depth is always final before its child reads it.
    back_depth = np.zeros(count, dtype=np.float32)
    time_order = np.argsort(facts.tx_time, kind="stable")

    # How many times each output address had already been seen, strictly earlier.
    out_addr = facts.out_addr
    prior_use = np.zeros(out_addr.shape[0], dtype=np.float64)
    known = out_addr >= 0
    if known.any():
        prior_use[known] = _prior_occurrence_index(
            out_addr[known], facts.tx_time[facts.out_tx[known]], facts.out_tx[known].astype(np.int64)
        )

    for transaction in time_order:
        outs = out_order[out_starts[transaction]:out_starts[transaction + 1]]
        ins = in_order[in_starts[transaction]:in_starts[transaction + 1]]
        resolved = facts.in_prev[ins]
        resolved = resolved[resolved >= 0]
        parents = np.unique(prev_tx_of_output[resolved]) if resolved.size else np.zeros(0, dtype=np.int32)

        row = matrix[transaction]
        row[0] = resolved.size
        row[1] = outs.size
        row[2] = (outs.size - resolved.size) / max(outs.size + resolved.size, 1)
        row[3] = parents.size
        if parents.size:
            back_depth[transaction] = min(2.0, 1.0 + float(back_depth[parents].max()))
        row[4] = back_depth[transaction]
        row[5] = np.log1p(float(facts.out_value[resolved].sum())) if resolved.size else 0.0
        row[6] = np.log1p(float(facts.out_value[outs].sum())) if outs.size else 0.0

        recipients = prior_use[outs] if outs.size else np.zeros(0)
        row[7] = float(recipients.mean()) if recipients.size else 0.0
        row[8] = float(recipients.max()) if recipients.size else 0.0
        row[9] = float((recipients > 0).mean()) if recipients.size else 0.0

    if facts.tx_asn is not None and facts.tx_src_ip is not None and facts.tx_country is not None:
        tiebreak = np.arange(count, dtype=np.int64)
        matrix[:, 10] = _trailing_cooccurrence(facts.tx_src_ip, facts.tx_time, window_seconds)
        matrix[:, 11] = _trailing_cooccurrence(facts.tx_asn, facts.tx_time, window_seconds)
        matrix[:, 12] = _expanding_rarity(
            facts.tx_country, facts.tx_time, tiebreak, len(facts.countries or []) or 1
        )
    return Table(GRAPH_COLUMNS, matrix)




NETWORK_CONTEXT_COLUMNS = ("endpoint_cooccurrence_trailing", "asn_cooccurrence_trailing", "country_rarity_prior")


def build_network_context(facts: Facts, *, window_seconds: int = 3600) -> Table | None:
    """The PS network-layer columns of grain E alone, fully vectorised.

    Used by the deployed stack to corroborate (not re-rank) a flagged
    transaction: how busy its relay endpoint and ASN had been in the preceding
    hour, and how rare its reported country had been so far. None when the
    snapshot carries no network observations.
    """
    if facts.tx_src_ip is None or not (facts.tx_src_ip >= 0).any():
        return None
    count = facts.transaction_count
    matrix = np.zeros((count, len(NETWORK_CONTEXT_COLUMNS)), dtype=np.float32)
    matrix[:, 0] = _trailing_cooccurrence(facts.tx_src_ip, facts.tx_time, window_seconds)
    matrix[:, 1] = _trailing_cooccurrence(facts.tx_asn, facts.tx_time, window_seconds)
    matrix[:, 2] = _expanding_rarity(
        facts.tx_country, facts.tx_time, np.arange(count, dtype=np.int64), len(facts.countries or []) or 1
    )
    return Table(NETWORK_CONTEXT_COLUMNS, matrix)
