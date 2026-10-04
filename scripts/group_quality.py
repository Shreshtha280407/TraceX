"""Offline evaluation of an actual frozen investigation queue against episode truth.

Episode truth is evaluation-only. Transaction criminality/one positive member
is NEVER converted to a group label. Unknown/mixed groups fail closed at K.
"""
import argparse
import json
import os
from pathlib import Path

from app.engine.investigations import POLICY, PROCEDURE_SHA256, QUEUE_POLICY
from app.ml.candidate import sha
from scripts.appliance_acceptance import request


def evaluate(groups, subjects, truth, *, capacity=100):
    reasons = []
    if not truth.get("evaluation_only") or not truth.get("do_not_ingest") or truth.get("grouping_sha256") != PROCEDURE_SHA256 or truth.get("proposition") != POLICY["group_target"]:
        reasons.append("Applicable independently declared episode proposition truth/frozen grouping identity is absent.")
    episodes = truth.get("episodes", [])
    index = {}
    for episode in episodes:
        if type(episode.get("requires_review")) is not bool or not episode.get("episode_id") or not episode.get("transactions") or not episode.get("rule_id"):
            reasons.append("Each episode needs an explicit rule proposition, canonical TX set and boolean review-interest judgement.")
            continue
        for tx in episode["transactions"]:
            index.setdefault(tx.removeprefix("tx:"), []).append(episode)
    results, fragments, detected = [], {}, set()
    for group in groups:
        txs = {tx.removeprefix("tx:") for tx in subjects.get(group["group_id"], [])}
        matched = [episode for tx in txs for episode in index.get(tx, []) if episode["rule_id"] == group["family"].split("/")[0]]
        episode_ids = {e["episode_id"] for e in matched}
        values = {e["requires_review"] for e in matched}
        unknown = not txs or any(not any(e["rule_id"] == group["family"].split("/")[0] for e in index.get(tx, [])) for tx in txs)
        mixed = len(values) > 1
        overmerged = len(episode_ids) > 1
        evaluable = not unknown and not mixed and not overmerged and len(values) == 1
        positive = next(iter(values)) if evaluable else None
        if positive:
            detected.update(episode_ids)
        for episode in episode_ids:
            fragments[episode] = fragments.get(episode, 0) + 1
        results.append({"group_id": group["group_id"], "label": positive, "evaluable": evaluable,
                        "unknown": unknown, "mixed": mixed, "overmerged": overmerged,
                        "benign_control": sorted({e.get("benign_control") for e in matched if e.get("benign_control")}),
                        "episode_ids": sorted(episode_ids)})
    queued = results[:capacity]
    if len(queued) < capacity:
        reasons.append(f"Actual queue contains only {len(queued)} groups, below registered K={capacity}.")
    if any(not r["evaluable"] for r in queued):
        reasons.append("Required queue includes unknown, mixed or over-merged group propositions; no passing precision inferred from labelled subset.")
    if not episodes:
        reasons.append("No applicable group episode truth; transaction labels are insufficient.")
    positives = {e["episode_id"] for e in episodes if e.get("requires_review") is True}
    queued_detected = {episode for row in queued if row["label"] is True for episode in row["episode_ids"]}
    return {"status": "NOT EVALUABLE" if reasons else "EVALUATED", "reasons": reasons,
            "proposition": POLICY["group_target"], "grouping_sha256": PROCEDURE_SHA256, "queue_policy": QUEUE_POLICY,
            "capacity": capacity, "population_groups": len(groups), "applicable_labelled_groups": sum(r["evaluable"] for r in results),
            "p_at_100": sum(r["label"] is True for r in queued)/capacity if not reasons and capacity==100 else None,
            "precision_at_capacity": sum(r["label"] is True for r in queued)/capacity if not reasons and capacity else None,
            "episode_recall_at_capacity": len(queued_detected & positives)/len(positives) if positives and not reasons else None,
            "mixed_groups": sum(r["mixed"] for r in results), "overmerged_groups": sum(r["overmerged"] for r in results),
            "fragmented_episodes": {key: n for key,n in fragments.items() if n>1},
            "benign_in_queue": sum(bool(r["benign_control"]) for r in queued),
            "unknown_groups": sum(r["unknown"] for r in results), "per_group": results,
            "limitations": "Episode detection recall, not transaction recall. Missing/mixed truth is never negative; one-positive-member is not a group proposition."}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-env", default="TRACEX_REVIEW_TOKEN")
    parser.add_argument("--capacity", type=int, default=100)
    parser.add_argument("--max-groups", type=int, default=10000, help="Strict bounded offline evaluation limit; does not truncate a queue into a pass")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True, help="Registered canonical sources; must be the actual case upload")
    args=parser.parse_args(argv)
    if not 1<=args.capacity<=10000:
        parser.error("quality evaluation requires positive capacity 1..10000")
    token=os.environ.get(args.token_env)
    if not token:
        parser.error("authenticated case owner token must be supplied via the named environment variable, never an argument")
    groups, subjects = [], {}
    from app.ml.evaluation_identity import final_id, fingerprint
    identity=final_id(args.dataset)
    protocol=json.loads(args.protocol.read_text())
    if identity not in protocol.get("registered_final_ids", []) or protocol.get("grouping_sha256") != PROCEDURE_SHA256:
        parser.error("group evaluation requires this exact registered final and frozen grouping procedure")
    sources=request(args.base,f"/cases/{args.case}/sources",token=token)["sources"]
    raw=sha(args.dataset/"ingestion_rows.ndjson")
    if len(sources)!=1 or sources[0]["sha256"]!=raw:
        parser.error("group evaluation case must contain exactly this registered source; aggregate/mixed-source cases are inapplicable")
    analysis=request(args.base,f"/cases/{args.case}/analysis",token=token)
    stages={s["name"]:s for s in analysis.get("analysis",{}).get("stages",[])}
    if analysis.get("analysis",{}).get("state")!="complete" or stages.get("investigation_grouping",{}).get("details",{}).get("procedure_sha256")!=PROCEDURE_SHA256:
        parser.error("final worker grouping/scoring is incomplete or mismatched")
    queue=request(args.base,f"/cases/{args.case}/investigation-queue?capacity={args.capacity}&limit=200",token=token)
    if queue["unresolved_groups"] > args.max_groups:
        parser.error("Offline group-quality population exceeds the declared bounded limit; use a separately registered bounded study, never silently truncate")
    offset=0
    while offset < queue["queued_groups"]:
        page=request(args.base,f"/cases/{args.case}/investigation-queue?capacity={args.capacity}&limit=200&offset={offset}",token=token)
        groups.extend(page["items"])
        offset+=200
    offset=0
    while offset < queue["backlog_groups"]:
        page=request(args.base,f"/cases/{args.case}/investigation-queue?capacity={args.capacity}&scope=backlog&limit=200&offset={offset}",token=token)
        groups.extend(page["items"])
        offset+=200
    for group in groups:
        values, offset = [], 0
        while True:
            page=request(args.base,f"/investigation-groups/{group['group_id']}/subjects?offset={offset}&limit=200",token=token)
            values.extend(r["ref"] for r in page["items"] if r["kind"]=="transaction")
            if len(page["items"])<200:
                break
            offset+=200
        subjects[group["group_id"]]=values
    report=evaluate(groups,subjects,json.loads(args.truth.read_text()),capacity=args.capacity)
    report.update(truth_sha256=sha(args.truth),queue_counts={k:queue[k] for k in ("underlying_findings","investigation_groups","unresolved_groups","queued_groups","backlog_groups")})
    report.update(protocol_sha256=sha(args.protocol),final_id=identity,source_sha256=fingerprint(args.dataset)["source_sha256"],
                  release_id=stages["ml_scoring"]["details"].get("release_id"))
    with args.output.open("x") as stream:
        json.dump(report,stream,indent=2)
    return 0 if report["status"]=="EVALUATED" else 2


if __name__=="__main__":
    raise SystemExit(main())
