#!/usr/bin/env python3
"""Score an imported generator dataset against its ground truth.

    uv run --extra ml python scripts/evaluate_scenarios.py \
        --database var/scale-run/judge_b/control.db --evidence var/scale-run/judge_b/evidence \
        --truth datasets/judge_b/evaluation_truth.json

Measures what the PS asks TraceX to find, using the generator's
`evaluation_truth.json` (never imported):

* scenario coverage -- share of each injected scenario kind (ransomware,
  darknet market, layering, extortion) that at least one finding touches, and
  how often benign controls (merchant, payroll) are touched;
* entity clustering -- pairwise precision / recall of common-input-ownership
  clusters against the true wallet of every scenario address;
* risk propagation -- seeding only the scenarios' reported seed addresses, the
  share of truly tainted addresses that receive risk, and the ROC AUC of risk
  for tainted vs. untainted wallets;
* network layer -- precision / recall of `reported_geo_mismatch` findings and
  recall of relay-concentration findings against the truth relays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import make_engine
from app.engine import analytics
from app.models import Case, FindingRecord, RiskSeed


def _auc(positive: np.ndarray, negative: np.ndarray) -> float:
    if positive.size == 0 or negative.size == 0:
        return float("nan")
    values = np.concatenate([positive, negative])
    order = values.argsort(kind="stable")
    ranks = np.empty(values.size)
    ranks[order] = np.arange(1, values.size + 1)
    # average ranks for ties
    for value in np.unique(values):
        tied = values == value
        if tied.sum() > 1:
            ranks[tied] = ranks[tied].mean()
    return float((ranks[: positive.size].sum() - positive.size * (positive.size + 1) / 2) / (positive.size * negative.size))


def evaluate(args):
    if args.database.resolve().parent.parent != (REPO / "var/scale-run").resolve():
        raise ValueError("Truth-seeded evaluation is restricted to disposable benchmark databases")
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest"):
        raise ValueError("Truth lacks evaluation-only isolation markers")
    sessions = sessionmaker(bind=make_engine(f"sqlite:///{args.database}"), expire_on_commit=False)
    report: dict = {"truth_sha256": hashlib.sha256(args.truth.read_bytes()).hexdigest(),
                   "scope": "synthetic disposable benchmark only; all temporary truth-seeded changes rolled back"}
    with sessions() as session:
        case = session.scalar(select(Case))
        if case is None or not case.synthetic or not case.name.startswith("scale "):
            raise ValueError("Not a disposable synthetic scale case")
        record = analytics.latest_analytics(session, case.id)
        cursor = analytics.cursor_for(args.evidence, record)

        # ---- which transactions / addresses do findings touch? -------------
        touched_tx: set[str] = set()
        touched_address: set[str] = set()
        geo_flagged: set[str] = set()
        relay_flagged: set[str] = set()
        for entity_ref, rule_id, vector in session.execute(
            select(FindingRecord.entity_ref, FindingRecord.rule_id, FindingRecord.feature_vector)
            .where(FindingRecord.case_id == case.id)
        ):
            kind, _, value = entity_ref.partition(":")
            if kind == "tx":
                touched_tx.add(value)
            elif kind == "address":
                touched_address.add(value)
            elif kind == "endpoint" and rule_id == "reported_geo_mismatch":
                geo_flagged.add(value)
            elif kind == "endpoint" and rule_id == "relay_flow_coherence":
                relay_flagged.add(value)
                touched_tx.update((vector or {}).get("transactions") or [])
            if rule_id in {"relay_endpoint_concentration", "relay_asn_concentration"} and vector:
                relay_flagged.add(vector.get("wallet"))
                if vector.get("dimension") == "endpoint":
                    relay_flagged.add(vector.get("key"))
                touched_tx.update(vector.get("transactions") or [])
            detector = (vector or {}).get("detector_result") or {}
            pattern = detector.get("pattern") or {}
            touched_tx.update(t.removeprefix("tx:") for t in pattern.get("transaction_ids") or [])
            for node in (detector.get("graph_path") or {}).get("nodes") or []:
                if node.startswith("tx:"):
                    touched_tx.add(node[3:])
                elif node.startswith("address:"):
                    touched_address.add(node[8:])

        # ---- scenario coverage ---------------------------------------------
        coverage: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for scenario in truth["scenarios"]:
            if scenario["kind"] == "exchange":
                continue
            txids = set(scenario.get("transaction_ids") or [])
            addresses = {a for a, wallet in truth["entity_truth"].items()
                         if wallet in set((scenario.get("wallets") or {}).values())}
            hit = bool(txids & touched_tx) or bool(addresses & touched_address)
            coverage[scenario["kind"]][0] += int(hit)
            coverage[scenario["kind"]][1] += 1
        report["scenario_coverage"] = {kind: {"touched": hit, "total": total, "rate": round(hit / total, 3)}
                                       for kind, (hit, total) in sorted(coverage.items())}

        # ---- entity clustering ---------------------------------------------
        truth_wallet = truth["entity_truth"]
        predicted = analytics.wallet_for_addresses(cursor, list(truth_wallet)) if truth_wallet else {}
        by_truth: dict[str, list[str]] = defaultdict(list)
        for address, wallet in truth_wallet.items():
            by_truth[wallet].append(address)
        true_pairs = predicted_pairs = both = 0
        for addresses in by_truth.values():
            for left, right in combinations(sorted(addresses), 2):
                true_pairs += 1
                if predicted.get(left) and predicted.get(left) == predicted.get(right):
                    both += 1
        by_predicted: dict[str, list[str]] = defaultdict(list)
        for address, entity in predicted.items():
            by_predicted[entity].append(address)
        for addresses in by_predicted.values():
            predicted_pairs += len(addresses) * (len(addresses) - 1) // 2
        precision_pairs = sum(
            1 for addresses in by_predicted.values() for left, right in combinations(sorted(addresses), 2)
            if truth_wallet.get(left) == truth_wallet.get(right)
        )
        report["entity_clustering"] = {
            "truth_addresses": len(truth_wallet), "truth_wallets": len(by_truth),
            "pairwise_recall": round(both / true_pairs, 3) if true_pairs else None,
            "pairwise_precision_within_truth_addresses": round(precision_pairs / predicted_pairs, 3) if predicted_pairs else None,
            "note": "recall is bounded by design: peel-chain change and layering addresses are never co-spent, "
                    "so common-input-ownership cannot link them",
        }

        # ---- risk propagation from the true seeds ---------------------------
        for seed in session.scalars(select(RiskSeed).where(RiskSeed.case_id == case.id)):
            session.delete(seed)
        for address in truth["risk_truth"]["seed_addresses"]:
            session.add(RiskSeed(case_id=case.id, wallet_ref=address, label="illicit", reason="generator truth seed",
                                 weight=1.0, created_by=case.created_by))
        session.flush()
        run = analytics.propagate_risk(session, evidence_root=args.evidence, record=record, case_id=case.id,
                                       created_by=None)
        session.rollback()
        risk = {item["wallet"]: item["risk"] for item in run.scores}
        tainted = truth["risk_truth"]["tainted_addresses"]
        tainted_wallets = {predicted.get(address) or address for address in tainted}
        seeds = {predicted.get(address) or address for address in truth["risk_truth"]["seed_addresses"]}
        tainted_wallets -= seeds
        reached = sum(1 for wallet in tainted_wallets if risk.get(wallet, 0) > 0)
        sample = [row[0] for row in cursor.execute("SELECT wallet FROM wallets ORDER BY hash(wallet),wallet LIMIT 5000").fetchall()]
        negatives = np.array([risk.get(w, 0.0) for w in sample if w not in tainted_wallets and w not in seeds])
        positives = np.array([risk.get(w, 0.0) for w in tainted_wallets])
        report["risk_propagation"] = {
            "seed_addresses": len(seeds), "tainted_wallets": len(tainted_wallets),
            "tainted_reached": reached, "recall": round(reached / len(tainted_wallets), 3) if tainted_wallets else None,
            "auc_tainted_vs_random": round(_auc(positives, negatives), 3),
            "wallets_with_risk": run.summary.get("wallets_with_risk"),
            "note": "risk is capped to the top 2000 wallets per run; tainted wallets beyond that count as unreached",
            "negative_sampling": "deterministic hash-order sample capped at 5000; not full-population AUC",
        }

        # ---- network layer -------------------------------------------------
        truth_geo = set(truth["network_truth"]["geo_mismatch_ips"])
        truth_relays = truth["network_truth"]["relay_concentration"]
        relay_hits = sum(1 for item in truth_relays if item["ip"] in relay_flagged)
        report["network"] = {
            "geo_mismatch": {"truth": len(truth_geo), "flagged": len(geo_flagged),
                             "true_positives": len(truth_geo & geo_flagged),
                             "precision": round(len(truth_geo & geo_flagged) / len(geo_flagged), 3) if geo_flagged else None,
                             "recall": round(len(truth_geo & geo_flagged) / len(truth_geo), 3) if truth_geo else None},
            "relay_concentration": {"truth": len(truth_relays), "detected": relay_hits,
                                    "recall": round(relay_hits / len(truth_relays), 3) if truth_relays else None,
                                    "note": "detected by relay_endpoint_concentration (per wallet, >= 5 observed spends) "
                                            "or relay_flow_coherence (per endpoint, chained spends)"},
        }
        cursor.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite scenario evaluation evidence")
    report = {"status": "failed"}
    try:
        report.update(evaluate(args), status="pass")
    except Exception as error:  # noqa: BLE001 - retain failed research evidence
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
