"""Strictly prior recipient activity; participation is not wallet ownership.

Compact per-address state, with bounded source witnesses only for requested TXs.
Equal-time transactions are scored together before any history updates.
"""
import numpy as np

COLUMNS = ("output_recipient_coverage", "prior_recipient_fraction", "prior_recipient_mean_count",
           "prior_recipient_max_span_seconds")


def working_set_bytes(transactions, outputs):
    """Conservative incremental arrays/copies; addresses are bounded by outputs."""
    return int(transactions) * 64 + int(outputs) * 32


def recipient_history(facts, *, selected=(), feature_values=True):
    starts, outputs = facts.outputs_of()
    count = np.zeros(len(facts.addresses), dtype=np.int32)
    first_time = np.zeros(len(count), dtype=np.int64)
    last_time = np.zeros(len(count), dtype=np.int64)
    first_tx = np.full(len(count), -1, dtype=np.int32)
    last_tx = np.full(len(count), -1, dtype=np.int32)
    values = np.zeros((facts.transaction_count, len(COLUMNS)), dtype=np.float32) if feature_values else None
    selected = set(map(int, selected))
    witnesses = {}
    order = np.argsort(facts.tx_time, kind="stable")

    def recipients(transaction):
        slots = facts.out_addr[outputs[starts[transaction]:starts[transaction + 1]]]
        known = slots[slots >= 0]
        return np.unique(known), len(known) / len(slots) if len(slots) else 0

    position = 0
    while position < len(order):
        end = position + 1
        stamp = int(facts.tx_time[order[position]])
        while end < len(order) and facts.tx_time[order[end]] == stamp:
            end += 1
        # No pending Python list proportional to a whole equal-time group.
        for transaction in order[position:end]:
            if not feature_values and int(transaction) not in selected:
                continue
            addresses, coverage = recipients(transaction)
            if feature_values:
                prior = count[addresses] > 0
                spans = last_time[addresses] - first_time[addresses]
                values[transaction] = (coverage, float(prior.mean()) if len(prior) else 0,
                    float(count[addresses].mean()) if len(addresses) else 0,
                    float(spans.max()) if len(spans) else 0)
            if int(transaction) in selected:
                recurrent = [int(address) for address in addresses if count[address] >= 2]
                recurrent.sort(key=lambda address: (-int(count[address]), facts.addresses[address]))
                witnesses[int(transaction)] = [{"recipient_ref": facts.addresses[address],
                    "prior_transactions": int(count[address]), "first_observed_epoch": int(first_time[address]),
                    "last_observed_epoch": int(last_time[address]),
                    "witness_txids": [facts.txids[first_tx[address]], facts.txids[last_tx[address]]]} for address in recurrent[:3]]
        for transaction in order[position:end]:
            addresses, _ = recipients(transaction)
            unseen = addresses[count[addresses] == 0]
            first_time[unseen], first_tx[unseen] = stamp, transaction
            count[addresses] += 1
            last_time[addresses], last_tx[addresses] = stamp, transaction
        position = end
    return values, witnesses


def opposing_observations(witnesses, references):
    """Observed recurrence weakens novelty, not the structure or crime proposition."""
    rows = []
    for witness in witnesses:
        refs = [ref for txid in witness["witness_txids"] for ref in references.get(txid, [])]
        if not all(references.get(txid) for txid in witness["witness_txids"]):
            continue  # No source-backed opposition if either witness is unavailable.
        rows.append({"kind": "observed_recipient_recurrence",
            "statement": f"Observed recipient identifier {witness['recipient_ref']} appears in {witness['prior_transactions']} strictly earlier supplied transactions. "
                "First/last source witnesses support recurring participation, which weakens a novel-recipient interpretation. "
                "This does not refute the structural match or establish payroll, merchant status, ownership or innocence.",
            "observations": witness, "source_refs": refs,
            "coverage": "Address or canonical script-ID fallback; bounded first/last witness sample. History outside the supplied snapshot is unknown."})
    return rows
