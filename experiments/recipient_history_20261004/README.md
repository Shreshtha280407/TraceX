# TraceX compact evidence

Implementation follow-up to owner commit `c70fcb4`; changes are uncommitted.
Underlying sanitized results, not only checksums, are included:

- `result-00.json`: controlled 256-TX queue-policy intervention, three isomorphic
  training/calibration/inference fixtures. Same frozen weights; **not independent
  generalization, not a default-v2 comparison, not product acceptance**.
- `result-01.json`: 42/42 local browser checks on 64 TX, SQLite and actual worker.
  Ordinary unknown-domain case correctly falls back to anomaly-stack-v2. This
  preceded the last quantitative-retention gate tightening, covered by final tests.
- `tests-00.json`: final 202-test audited small suite, no failures/skips.
- `verification.json`: actual command outcomes, current source/lock hashes and
  explicit NOT RUN gates. Source hashes identify the final scoring/reporting code,
  not a retrospective full code pin for the earlier browser run.
- `inventory.json`: result integrity and original private report digests.

The controlled new-policy K=20 result is precision 0.95 and 1 merchant entry;
same-weights legacy queue is 0.50 and 10 entries. Merchant motif mean remains 1.0.
This does not establish real-case merchant precision, statistical significance or
the AP/P@100 promotion targets. No production artifact was promoted/installed by
this work. No 100K/300K/1M/3M job was run, and no old final holdout was reopened.

Review files manually before Git publication. Credentials, raw sources, reviewer
exports and fitted weights are excluded. Null code identity in an input result is
unknown, not clean or measured. MacBook 3M/time and representative quality results
are **NOT RUN**, not passes.

See [the current report](../../docs/recipient_history_and_deployment_2026-10-04.md)
and [ordered Git/Mac runbook](../../docs/macbook_runbook.md) for limits and reproduction.
