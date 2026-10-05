# Case dashboard pending group count — 2026-10-05

Base HEAD: `74a82a1`. No applicable AGENTS.md was found; the checkout was clean
before this change. No commit or push was performed.

## Actual cause

Read-only checks of the development API's PostgreSQL database found three
completed older 100K imports, each with **23,099 stored findings and zero
published investigation groups**. There were no active imports. Zero stored
groups did not establish that no human reviews were pending. Two separate
128-transaction test cases already had 42 groups each.

The older cases belong to different accounts. They were not automatically
modified by impersonating an account or bypassing case authorization.

## Implementation

- The dashboard displays the actual published unresolved **group queue**, with
  default capacity 100: `min(100, unresolved_groups)`. One investigation group
  is one proposition/episode review, not one task for every member transaction.
- An ungrouped older case now shows **Not ready**, rather than a misleading
  zero. An active import with no published queued groups shows **Preparing**.
  A completely grouped and genuinely resolved case still correctly shows zero.
- **Generate review groups** submits an explicit, authenticated, case-authorized
  POST to `/v1/cases/{case_id}/investigation-queue/build`. Queue GETs do not mutate
  cases. A request queues at most 20 completed snapshots and is idempotent while
  work is already queued/running.
- Group-only intent is durable in `AnalysisRequest.grouping_only`, with an
  additive default-false schema upgrade. Old analytics/full-analysis intents
  retain their meaning. A newer full-analysis request supersedes group-only
  recovery; retries and worker reclamation retain unfulfilled intent.
- The worker verifies immutable source and canonical-graph hashes, then runs
  the existing bounded grouping procedure. It does **not** reparse transactions,
  rebuild the graph, fit/score ML or rerun analytics for group-only recovery.
- Group publication, request fulfillment, stage completion and job completion
  commit together. Failure retains intent and records an actionable failed job.
  Source receipts, prior stage outcomes, confidence pins and individual reviews
  are preserved. Identical generations retain membership and group decisions.
- Counts refresh on completion/review events and focus, with bounded polling
  only while analysis is active. Evidence Intake identifies the review-group
  stage accurately. No unrelated frontend styling was changed.

The default 100 is **queue capacity**, not a fabricated total pattern count.
Additional unresolved groups remain available through the backlog view. Fewer
actual unresolved groups yield a smaller queue. Grouping rules, scoring release,
family ordering, detector outputs and model eligibility were not changed; no
quality/precision improvement is claimed from this count correction.

## Verified now

- **77 audited small backend tests passed in 23.53 s**, including recovery,
  failure/crash reclamation, authorization, 0/1/100 capacities, accessible
  backlog, immutable replay, preserved reviews/pins/stages and schema upgrade.
  XML: `var/group-queue-recovery-20261005-tests.xml`.
- **13 mocked browser checks passed**, including 100, 42 and truly zero pending
  groups, unknown counts on missing grouping, an explicit recovery POST and
  automatic count refresh. Latest report:
  `var/browser-runs/review-counts-1791180814163/result.json`.
- **14 live API/worker checks and 3 live browser checks passed** on a new,
  isolated **60-transaction synthetic case**, including an explicit authorized
  dashboard recovery POST, automatic counter refresh, group-detail navigation,
  preserved group decision and unchanged receipt/pin/graph hashes. Report:
  `var/group-queue-recovery-live-20261005T062334-04539f94/result.json`;
  browser summary: `browser.json` in that same directory. Only this new
  synthetic fixture's publication flag was temporarily made inactive to
  simulate an older ungrouped case; existing owner cases were not altered.
- Node count/graph tests, TypeScript/production build, targeted Ruff and
  `git diff --check` passed. Frontend lint succeeded with existing React warnings;
  backend tests retain the existing Starlette/httpx deprecation warning.
- The exact idle local API and worker were reloaded with their original process
  environment, PostgreSQL, evidence vault and session configuration. The live
  new route, schema column and recent worker heartbeat were verified. All
  Docker services, images and volumes were untouched. Reload diagnostics:
  `var/local-service-reloads/20261005T061041950762Z/`.

The first recovery fixture had only three transactions and produced no findings;
it was corrected to 60 transactions. Final verification uses small temporary
fixtures, not large dataset generation or a 100K/1M benchmark. The old 100K
cases have **not** been automatically reprocessed or backfilled. No datasets,
reports or case evidence were deleted. The current grouping procedure hash is
unchanged; reserved model finals were not reopened.

Two live test-harness attempts failed before final success: the first omitted
the upload's required idempotency header; the second recovered the real queue
but expected the wrong group-detail heading. Those reports and synthetic
fixtures were retained, not overwritten or deleted:
`var/group-queue-recovery-live-20261005T062124-d57c332d/` and
`var/group-queue-recovery-live-20261005T062202-6748604d/`.

## Using the fix for an older upload

1. Refresh the case dashboard while logged into an authorized case account.
2. Click **Generate review groups** once. No reupload or ML rerun is needed.
3. The counter updates after the worker publishes the groups. Review up to 100
   queued groups; use the findings page's backlog view for additional groups.
4. If generation fails, inspect the job in Evidence Intake. Fix the reported
   integrity/worker issue before retrying; do not invent a count of 100.

Review and commit the changed files manually. No automatic push, publish or
large-data acceptance claim accompanies this UI/workflow correction.
