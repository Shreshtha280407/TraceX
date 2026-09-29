#!/usr/bin/env python3
"""Run the TraceX anomaly stack and compare every layer combination.

    python3 scripts/run_anomaly_stack.py --dataset datasets/phase5a_100k

Prints a ranked table of the deterministic rule baseline, each layer on its own,
and every combination of layers, all on the same transactions, the same split and
the same review budget.  Writes the full result to `--output` as JSON.

This answers the question directly: which combination actually predicts best.
Nothing here declares a winner in advance.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.ml import evaluate as ev
from app.ml.stack import StackConfig, explain, run_stack


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=REPO / "datasets" / "phase5a_100k")
    parser.add_argument("--layers", default="A,B,C,D,E", help="which layers to build (default: all)")
    parser.add_argument("--budget", type=float, default=0.01, help="review budget as a fraction of rows")
    parser.add_argument("--split", default="validation", choices=("validation", "final_holdout"),
                        help="split to report on; use final_holdout exactly once, after settings are frozen")
    parser.add_argument("--fusion", default="stouffer", choices=("stouffer", "rank_average"))
    parser.add_argument("--max-combination", type=int, default=None, help="cap the combination size")
    parser.add_argument("--no-stratify", action="store_true", help="score layer A globally instead of per shape family")
    parser.add_argument("--with-hbos", action="store_true", help="add the HBOS third opinion on grain A")
    parser.add_argument("--with-supervised", action="store_true",
                        help="add the supervised gradient-boosted ranker (layer S). On this fixture its "
                             "labels are generator truth, which is close to circular for the motif task "
                             "and genuinely informative for discrimination -- read the per-task tables, "
                             "not the headline")
    parser.add_argument("--no-bocpd", action="store_true", help="skip change-point posteriors in layer D")
    parser.add_argument("--rank-by", default="average_precision",
                        choices=("average_precision", "roc_auc", "precision_at_budget", "recall_at_budget"))
    parser.add_argument("--tasks", default="motif,surge,discrimination",
                        help="evaluation tasks to report (default: all three)")
    parser.add_argument("--limit", type=int, default=25, help="rows to print per task")
    parser.add_argument("--output", type=Path, default=None, help="write the full JSON result here")
    parser.add_argument("--explain-top", type=int, default=3, help="explain this many top-ranked transactions")
    args = parser.parse_args()

    if not (args.dataset / "transactions.ndjson").is_file():
        parser.error(
            f"no fixture at {args.dataset}. Generate it first:\n"
            "  python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k "
            "--formats csv,ndjson,xml,json --verify"
        )

    config = StackConfig(
        dataset=args.dataset,
        layers=tuple(item.strip().upper() for item in args.layers.split(",") if item.strip()),
        stratified=not args.no_stratify,
        with_hbos=args.with_hbos,
        with_bocpd=not args.no_bocpd,
        budget=args.budget,
    )
    print(f"TraceX anomaly stack — dataset={args.dataset}  layers={','.join(config.layers)}  "
          f"budget={args.budget:.3%}  threads={config.threads}")
    print(f"host: {platform.platform()} | python {platform.python_version()}")
    if args.with_supervised:
        print(
            "\n" + "!" * 100 + "\n"
            "  DEMO / RESEARCH MODE: layer S is trained on the fixture's generator truth.\n"
            "  Those labels are the generator's own shape families, and the deterministic rule's\n"
            "  predicate is nearly the same predicate, so the `motif` result is close to circular\n"
            "  and is NOT evidence of real-world accuracy. Read `discrimination` instead: its\n"
            "  negatives satisfy the rule exactly, so a gain there is one the rule cannot have.\n"
            "  What ships is the unsupervised ranking. Layer S becomes legitimate only once it\n"
            "  trains on analyst review decisions (app/ml/findings.py::review_decision_labels).\n"
            + "!" * 100
        )
    else:
        print("mode: unsupervised only — this is the configuration intended for deployment.")
    print()

    config.with_supervised = args.with_supervised
    started = time.perf_counter()
    if args.with_supervised:
        # Two passes: the first builds the grains, the second refits with layer S
        # once the labels it needs are available.  Kept explicit so it is obvious
        # that no unsupervised layer ever saw a label.
        probe = run_stack(StackConfig(**{**config.__dict__, "with_supervised": False}))
        labels = ev.load_labels(probe.facts, args.dataset / "evaluation_truth.json")
        result = run_stack(config, supervised_labels=labels.positive)
    else:
        result = run_stack(config)
        labels = ev.load_labels(result.facts, args.dataset / "evaluation_truth.json")

    print("grain / layer timings (seconds):")
    for name, seconds in result.timings.items():
        print(f"  {name:22s} {seconds:8.2f}")
    print(f"  {'total':22s} {time.perf_counter() - started:8.2f}")
    print(f"peak RSS: {result.peak_rss_mb:,.0f} MB")
    print(f"splits  : {result.notes['split_counts']}")
    print(f"labels  : {int(labels.positive.sum()):,} positive, {int(labels.near_miss.sum()):,} near-miss, "
          f"{int(labels.in_surge.sum()):,} in a labelled surge episode\n")

    tasks = tuple(item.strip() for item in args.tasks.split(",") if item.strip())
    by_task = ev.run_ablation(
        result.layer_scores, result.baseline, labels, result.splits,
        budget=args.budget, fusion=args.fusion, max_size=args.max_combination,
        split=args.split, tasks=tasks,
    )

    winners: dict[str, tuple[ev.Metrics, ev.Metrics]] = {}
    for task in tasks:
        rows = by_task[task]
        ranked = ev.rank_results(rows, key=args.rank_by)
        baseline = next(item for item in rows if item.candidate == "rule_baseline")
        winners[task] = (ranked[0], baseline)
        print("=" * 116)
        print(f"TASK: {task}  —  {ev.TASKS[task]}")
        print(f"  population {ranked[0].rows:,} transactions on the {args.split} split, "
              f"{ranked[0].positives:,} positive; ranked by {args.rank_by}")
        print("=" * 116)
        print(ev.format_table(ranked, limit=args.limit))
        print()

    print("=" * 116)
    print("VERDICT PER TASK")
    print("=" * 116)
    for task, (best, baseline) in winners.items():
        if best.candidate == "rule_baseline":
            print(f"  {task:16s} the deterministic rule wins (AP {baseline.average_precision:.4f}); "
                  f"no layer combination beat it")
            continue
        lift = (best.average_precision / baseline.average_precision) if baseline.average_precision > 0 else float("inf")
        print(f"  {task:16s} {best.candidate}")
        print(f"  {'':16s}   AP {best.average_precision:.4f} vs rule {baseline.average_precision:.4f} ({lift:.2f}x), "
              f"AUC {best.roc_auc:.4f}")
        print(f"  {'':16s}   at a {args.budget:.2%} budget: flags {best.flagged_at_budget:,}/{best.rows:,}, "
              f"recall {best.recall_at_budget:.1%}, precision {best.precision_at_budget:.1%}, "
              f"near-miss share {best.near_miss_rate_at_budget:.1%}")
    best = ev.rank_results(by_task[tasks[0]], key=args.rank_by)[0]

    if args.explain_top:
        import numpy as np

        print(f"\ntop {args.explain_top} ranked transactions under the best candidate, with drivers:")
        best_layers = best.notes.get("layers") or []
        if best_layers:
            from app.ml.fusion import rank_average_fuse, stouffer_fuse
            fuse = stouffer_fuse if args.fusion == "stouffer" else rank_average_fuse
            subset = {name: result.layer_scores[name] for name in best_layers}
            score = (next(iter(subset.values())) if len(subset) == 1
                     else fuse(subset, result.splits == 0, budget=args.budget).score)
        else:
            score = result.baseline
        mask = result.splits == ev.SPLIT_NAMES.index(args.split)
        candidates = np.flatnonzero(mask)
        top = candidates[np.argsort(-score[candidates])[: args.explain_top]]
        for transaction in top:
            payload = explain(result, int(transaction))
            family = labels.families[labels.family[transaction]] if labels.family[transaction] >= 0 else "unlabelled"
            print(f"\n  {payload['txid'][:16]}…  truth={family}  shape_family={payload.get('shape_family')}")
            for layer_name, value in sorted(payload["layers"].items()):
                print(f"      {layer_name:14s} {value:.4f}")
            for layer_name, drivers in payload["drivers"].items():
                pretty = ", ".join(f"{column}={value:.2f}" for column, value in drivers[:4])
                print(f"      drivers[{layer_name}]: {pretty}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "dataset": str(args.dataset),
            "layers": list(config.layers),
            "budget": args.budget,
            "split": args.split,
            "fusion": args.fusion,
            "ranked_by": args.rank_by,
            "threads": config.threads,
            "timings_seconds": result.timings,
            "peak_rss_mb": result.peak_rss_mb,
            "notes": {k: v for k, v in result.notes.items() if isinstance(v, (int, float, str, list, dict))},
            "supervised_enabled": args.with_supervised,
            "label_provenance": (
                "fixture evaluation truth (generator shape families) — DEMO/RESEARCH ONLY; "
                "not analyst review decisions, not evidence of real-world accuracy"
                if args.with_supervised else
                "none — unsupervised ranking only; no label reached any fit()"
            ),
            "deployable_configuration": not args.with_supervised,
            "label_counts": {
                "positive": int(labels.positive.sum()),
                "near_miss": int(labels.near_miss.sum()),
                "in_surge": int(labels.in_surge.sum()),
            },
            "results_by_task": {
                task: ev.to_json(ev.rank_results(rows, key=args.rank_by)) for task, rows in by_task.items()
            },
            "winners": {
                task: {"candidate": best.candidate, "average_precision": best.average_precision,
                       "roc_auc": best.roc_auc, "baseline_average_precision": base.average_precision}
                for task, (best, base) in winners.items()
            },
            "limitations": (
                "Synthetic-fixture evaluation only. These numbers do not prove real-world "
                "detection accuracy; they show which layer combination separates the "
                "fixture's labelled motifs from its labelled near-miss negatives. "
                "Every feature is strictly causal (only facts at or before its own "
                "transaction's timestamp), proved by the truncation property tests in "
                "tests/unit/test_anomaly_stack.py. The `motif` task's labels are close to "
                "the deterministic rule's own predicate, so its margin overstates what a "
                "model adds; `discrimination` is the honest comparison."
            ),
        }, indent=2), encoding="utf-8")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
