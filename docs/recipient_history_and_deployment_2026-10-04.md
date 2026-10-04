# Recipient context, deployed routing and quality gates — 2026-10-04

This is an uncommitted implementation follow-up to owner-committed
`c70fcb4f0b94982c005069c8b3208fb6e15b7550`. The previous reporting correction
is retained alongside these changes. No applicable AGENTS.md was found.
The earlier [integrated report](integrated_implementation_2026-10-04.md) and
[reporting report](quality_matrix_reporting_2026-10-04.md) retain their historical
scope; their measurements are not post-follow-up acceptance.

## Outcome and remaining external evidence

The product no longer requires a per-case candidate opt-in when an **installed,
quality-approved** artifact matches a declared approved domain. New API/UI cases
default to `auto_eligible`. The actual worker applies that policy, records its
eligibility/fallback decision and feeds the ordinary authorized review queue.
Synthetic artifacts are never automatically selected. Unknown applicability,
missing approval, bad integrity/library pins or a mismatched approved finding
fraction retain the eligible unsupervised `anomaly-stack-v2` procedure.

**No representative real-case model was promoted or installed here. Ordinary
uploads in this checkout therefore still fall back to v2.** Existing cases,
successful snapshots, reviews and confidence pins are not automatically migrated.
Explicit unsupervised and explicit candidate modes remain available. This is a
deployment-path improvement, not a claim that new candidate quality is already
approved for arbitrary real cases.

There is now a measured **controlled unit** reduction in merchant-control queue
entries, not just better explanation text. There is still **no demonstrated
real-case/default-v2 merchant false-positive or precision improvement**. The
external blocker is representative, independently applicable labels and a
reviewed frozen-model validation/promotion decision. Synthetic data alone cannot
honestly remove that limitation. Arbitrary unlabelled uploads cannot supply
measured AP/accuracy. MacBook 3M/<1800s acceptance and fresh representative
generalization remain **NOT RUN**.

## Versioned scoring implementation

`app/ml/recipient_history.py` supplies four features: recipient coverage, fraction
seen previously, mean prior transaction count and maximum prior observed span.
Recipient identifiers are supplied addresses or the existing canonical script-ID
fallback, not wallet identities. The computation uses strictly earlier timestamps:
equal-time groups are scored before any of their history is updated. Repeated
outputs to a recipient count once per transaction. Missing identifiers have
explicit incomplete coverage; missing history is not a benign or innocence flag.
No generator IDs, truth, reviewer decisions or future observations enter features.

New fitted candidates use `causal-structure-recipient-history-v2` (38 columns) and
`review-contrast-stable-txid-v2`. Queue priority is the separately trained
discrimination/review-interest target, not max(motif,surge), criminality or an
arbitrary cross-family risk rescaling. Motif/surge scores and all deterministic
structural findings remain accessible. A legitimate equal-output shape is still
a correct structural observation. Old 34-column
`causal-structure-prior-bucket-v1` artifacts retain their features, calibrated
weights, sensitivity columns and max(motif,surge) queue policy. Explicit previously
approved v1 artifacts remain supported; automatic activation requires the new
measured promotion proof. The v2 unsupervised fallback's scores/fitting/fusion
procedure remain unchanged, including its **retrospective** burst layer.

Candidate findings store the feature contract, artifact/manifest hashes,
calibration applicability, three separate task scores, actual queue policy,
retention fraction, bounded descriptive sensitivity and frozen training baselines.
They include source-backed first/last witnesses for up to three recurrent
recipients. Both witness references must be present. Observed recurrence weakens
a novel-recipient interpretation; it does not refute a structural match or
establish merchant/payroll status, ownership, innocence or criminality.
Supporting/opposing raw-source replay still passes case authorization,
immutable-hash and receipt checks. These observations use the existing structured
evidence UI, not generated language; the release contains no LLM/chat.

## Promotion cannot substitute research tables for product precision

`scripts.candidate_lifecycle` trains/calibrates/compares all four backends on the
frozen contract, freezes validation-selected fitted weights, then evaluates
untouched finals without label fitting/adaptation. New reports compare the actual
candidate queue policy against the v2 procedure at the **same reviewer capacity**.
V2 is a label-free per-dataset reference refit with retrospective burst, not
frozen transfer. Truth family flags are used only in offline evaluation.

`product-queue-quality-gate-v1` requires exact release/payload/feature identity,
frozen-transfer provenance, measurable motif/surge AP >=0.85, all-task P@100 >=0.90,
nonnegative AP deltas, queue precision >=0.90 and no precision/recall/benign/merchant
regression. At least one measured merchant queue FP reduction is required.
Every registered report must agree on the retained-finding fraction and K.
The quantitative gate is necessary, not a representative-label or significance
certification. Owner domain/label-applicability approval remains separately required.

Missing/invalid metrics cannot pass. Full time-eligible population truth coverage
is required for a **product** queue comparison; unlabelled rows are not negatives.
The generator omits scenario-funding rows from truth. Its labelled-subset AP can
remain useful, but whole-queue precision/recall is NOT EVALUABLE until applicable
independent labels cover omitted rows. Do not manufacture negatives to pass.
Reports independently check that candidate retained findings and the v2 reference
threshold produce enough findings to exercise K. A score-only top-K larger than
the available product findings is not evidence of product precision.

The corrected matrix now includes `queue_coverage` and is INCOMPLETE if any product
queue comparison is not evaluable, even when labelled-subset AP is finite.
Dataset reasons survive compact export. Export also reports unknown dirty-tree
identity as null, not falsely clean. `promote --quality-result` writes an exclusive
decision report even on a blocked quantitative gate. Successful promotion copies
the exact fitted/calibration bytes and retains release identity; hashes/pins must
be updated for the approved metadata. The worker rejects a different actual
retained-finding fraction rather than silently reusing that approval.

## Controlled measurement — not generalization

The 256-transaction unit intervention uses three **isomorphic** role fixtures
(training, calibration, inference; 768 transactions total), deliberately recurring
merchant controls and a predeclared synthetic review-interest proposition.
It serializes/reloads frozen HGB weights; inference does not read final labels.
The comparison changes queue policy while retaining those same fitted weights.
It is **not** a default-v2 baseline, independent distribution transfer, actual
materialized-product acceptance or representative merchant-label study.

| At reviewer K=20 | New contrast policy | Same weights, legacy max(motif,surge) |
| --- | --- | --- |
| Synthetic review-interest precision | 0.95 | 0.50 |
| Merchant-control entries | 1 | 10 |

There are 86 merchant controls. Their preserved mean motif score is 1.0: correct
structural matches were not suppressed. This establishes wiring/mechanism behavior
on the constructed unit fixture, not statistical significance or real-case AP.
The artifact is explicitly synthetic/demo and is not installed in the product.
Its release/payload/manifest hashes and underlying compact measurements are in
[the review package](../experiments/recipient_history_20261004/README.md).

## Resource and reliability safeguards

History uses compact typed per-recipient state, not a Python dictionary of lists.
The witness-only pass allocates no global feature matrix, and references are
retrieved only for selected findings/witnesses. Joint admission/preflight adds
conservative history workspace (64 bytes/transaction + 32 bytes/output) and
expanded fitted weights multiplied for concurrent/native/deserialization costs.
The inside-worker preflight checks the externally pinned bounded manifest,
payload integrity, supported feature contract and exact library versions without
deserializing the model. Failed candidate admission is explicit, not a zero-byte
estimate. Rejected/ineligible fitted objects are released before fitting fallback.

Some global arrays, sorting and chronological passes remain necessary. Metadata-only
3.05M estimates and small tests are not measured million-row peak-memory/runtime
parity; later Mac profiling remains mandatory. Prior successful stage outcomes,
zero-finding scorer identity, immutable pins, raw sources and review history remain.

## Checks actually completed

The final audited small suite is recorded at
`var/recipient-history-20261004-08/review-ready-small.xml`: **202 passed in 30.29 s**,
with no failed/skipped tests. Small fixtures only; no large-suite discovery was executed
blindly. Coverage includes stage recovery, receipt/provisional reconciliation,
rules, queue ties/pagination/case authorization, source replay/stale opposition,
confidence mismatch, graph cursors, bounded/serial/parallel parity, admission,
portable resource fallbacks, failure diagnostics and candidate lifecycle.
The four backend CLI wiring checks use independent 64-row authored fixtures.
Additional controlled context/release tests cover cutoff/equal-time/order
invariance, exact serialization/backward compatibility, contextual queue behavior,
missing/partial labels, insufficient retained findings, promotion/fallback and
the actual worker's approved routing plus opposing evidence.

Frontend TypeScript/Vite build passed. Node graph-window check passed. Scoped
Ruff passed. Frontend lint exited 0 with 21 existing warnings; one existing
Starlette/httpx deprecation warning remains in pytest. The isolated 64-TX browser
run passed **42/42 checks**: auth/upload, mandatory stages, raw replay/review/pins,
bounded visual continuation, queue/export, analytics and removed-chat 404.
That browser run preceded the final quantitative-retention gate tightening;
the final worker/unit tests cover that tightening. It is SQLite/local-browser
functional verification, not PostgreSQL/Mac performance or native-Linux isolation
recertification. Private logs remain in `var/recipient-browser-20261004-01/`.

Intermediate test failures were corrected (new fail-closed policy assertions
required corresponding fixture metadata/finding-fraction updates); diagnostics
remain in var. Old measurements and datasets were not overwritten/deleted.

Reproduce the focused change checks with unique report paths:

```sh
uv run pytest tests/unit/test_recipient_history_release.py tests/unit/test_quality_matrix_reporting.py tests/unit/test_quality_wiring.py tests/unit/test_integrated_improvements.py tests/unit/test_integrated_boundaries.py -q -o junit_logging=system-out --junitxml=var/NEW_RECIPIENT_CHECKS.xml
uv run ruff check app/ml scripts/candidate_lifecycle.py scripts/quality_matrix.py scripts/export_review_package.py scripts/runtime_probe.py tests/unit/test_recipient_history_release.py tests/unit/test_quality_matrix_reporting.py
npm --prefix frontend run build
npm --prefix frontend run lint
node --test frontend/e2e/graph-window.test.mjs
uv run python -m scripts.candidate_lifecycle evaluate --help
uv run python -m scripts.candidate_lifecycle promote --help
uv run python -m scripts.macbook_benchmark accept --help
git diff --check
```

Use the [ordered owner Git/MacBook runbook](macbook_runbook.md) for fresh registered
quality studies, review/commit/push, architecture/dependencies, actual PostgreSQL
appliance, offline Geo-IP, container admission, exact counting, worker screening,
fresh full acceptance, UI retrieval and compact export. The new flags are
implemented and CLI help/small execution tested. Quality study preparation is
separate from benchmark upload/inference time.

No commit/push/merge/publication, .env changes, Docker image/volume/container
deletion, existing dataset/report/vault deletion, large workload/background job
or previous-final re-selection occurred. The existing appliance was untouched.
