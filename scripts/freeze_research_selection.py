"""Freeze validation-only selection and reserve finals before their generation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_candidate(reports, tolerance=.01):
    candidates = set.intersection(*(set(r["results"]["motif"]) for r in reports))
    ranked = []
    for candidate in sorted(candidates):
        if not candidate.startswith("E"):
            continue
        if all(r["results"]["discrimination"].get(candidate, {}).get("ap", -1) >=
               r["results"]["discrimination"]["v2_deployed_procedure"]["ap"] - tolerance for r in reports):
            scores = [r["results"][task][candidate].get("ap") for r in reports for task in ("motif", "surge")]
            if all(v is not None for v in scores):
                ranked.append((min(scores), candidate))
    if not ranked:
        raise ValueError("No candidate meets the validation regression gate; finals must not be evaluated")
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1], ranked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registration = json.loads(args.protocol.read_text())
    reports = [r for path in args.report for r in json.loads(path.read_text())["reports"]]
    if any(r["evaluation_split"] != "validation" for r in reports):
        raise ValueError("Final outcomes cannot participate in candidate selection")
    candidate, ranked = select_candidate(reports)
    protocol_sha = sha(args.protocol)
    selection = {"candidate": candidate, "protocol_sha256": protocol_sha,
                 "validation_report_sha256": {str(p): sha(p) for p in args.report},
                 "ranking": ranked, "production_adoption": "forbidden; synthetic research only",
                 "development_transactions": [r["transactions"] for r in reports]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(selection, handle, indent=2)
    for definition in registration["final_datasets"]:
        dataset = Path(definition["directory"])
        if (dataset / "evaluation_truth.json").exists():
            raise ValueError("Final dataset was generated before reservation")
        dataset.mkdir(parents=True, exist_ok=True)
        with (dataset / ".final_reservation.json").open("x") as handle:
            json.dump({"protocol_sha256": protocol_sha, "selection_sha256": sha(args.output),
                       "candidate": candidate, "definition": definition}, handle, indent=2)
    print(json.dumps(selection, indent=2))


if __name__ == "__main__":
    main()
