# Phase 4 — deterministic, reviewable findings

Phase 4 operates only on receipt-approved snapshots and their immutable Phase 3 graph. It has no ML dependency, no network call, and makes no ownership, origin, or wrongdoing assertion. A finding is an address/script plus a bounded time window prioritized for a reviewer.

## Frozen feature and rule contract

`deterministic-v1` computes the same address-window feature vector at 15-minute, one-hour, and 24-hour windows:

- inbound and verified outbound event counts, received and outgoing output counts, and distinct observed counterparties;
- received/outgoing value distributions and inter-event gap statistics;
- committed-prevout rapid-spend timing, only where the supplied outpoint resolves in the snapshot;
- a deliberately bounded one-hop graph delta. It is not a wallet/entity component or ownership claim.

The transparent versioned thresholds are: three distinct inbound source transactions for `concentrated_collection`, four for `emerging_hub`, and three inbound transactions plus at least one committed-prevout spend within 3,600 seconds for `rapid_redistribution`. Scores are deterministic priority scores, not probabilities or accusations.

Every feature record includes graph coverage, time-source counts, and `window_boundary_incomplete: true`: absence outside the committed source scope is unknown. When block time is unavailable, source timestamps are used only as source-observed times and this is recorded in coverage.

## Evidence, review, and export

- `GET /v1/cases/{case_id}/findings` lists case-scoped deterministic findings.
- `GET /v1/findings/{finding_id}/evidence` returns the feature vector, source locators, coverage, alternative explanations, opposing-evidence limitation, reviewer chain, and audit trail. Use `GET /v1/evidence/{source_id}/records?locator=...` to reopen the exact raw source record.
- `POST /v1/findings/{finding_id}/reviews` requires a `case_lead` or `reviewer` role and an `expected_finding_version`. The database conditional update prevents stale concurrent decisions; stale versions return `409`. A review may record source-linked `counterevidence_refs`.
- `GET /v1/cases/{case_id}/findings/export` produces a case-scoped JSON evidence bundle containing rule/version, features, raw-source locators, coverage, reviews, audit entries, and limitations.

The Phase 4 test uses a synthetic high-volume treasury-sweep lookalike. It first produces the same transparent lead, reopens the exact source row containing the authorized control context, and records a source-linked dismissal. This demonstrates why a lead is a review queue item, not a conclusion.

## Gate

```bash
make phase-four
```

This runs Phase 1–4 unit/integration coverage. `make phase-two-100k` remains the synthetic 100,000-transaction ingestion and graph scale check; it can also be run after Phase 4 because findings are derived in the same background completion path.

## Phase 4.1 graph-intelligence extension

Phase 4.1 adds three bounded deterministic review signals to the same receipt-approved snapshot:

- `peeling_chain_candidate` follows only `output -> SPENT_BY -> transaction -> CREATES_OUTPUT` chains. A continuation must be a real output that is later uniquely spent; default maximum depth is eight and default minimum chain is three verified spends. Its normalized score averages bounded verified-hop extent, strict value reduction regularity, timestamp continuity, and evidence coverage.
- `coinjoin_like_structure` is an observable shape only: by default at least three inputs, three outputs, and three equal-valued output candidates, with configurable satoshi tolerance. It does not call a transaction CoinJoin or mixing activity.
- `synthetic_seed_proximity` is available only from an explicit synthetic-case seed and traverses verified UTXO relationships downstream at most two transaction hops with `0.60` decay per hop. It never traverses IP/endpoint observation edges, never creates an ownership link, and distinguishes the direct seed from propagated context.

Each result exposes `finding_id`, `finding_type`, `entity_or_transaction_id`, `snapshot_id`, `graph_snapshot_id`, `score`, coverage, uncertainty, reason codes, explanation, evidence references, and a graph path where applicable. The feature dataset is exported through `GET /v1/cases/{case_id}/features/export`; its Phase 5A contract is [phase5a_handoff.md](phase5a_handoff.md). No dashboard exists in this backend-only repository, so the API returns an empty `findings`/`rows` list when no result is present.
