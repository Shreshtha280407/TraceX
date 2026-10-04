# TraceX integrated implementation — 2026-10-04

Historical scope: the initial integrated implementation subsequently committed by
the owner as `c70fcb4`. See the [recipient-history/deployment follow-up](recipient_history_and_deployment_2026-10-04.md)
for the newer candidate procedure, default selection policy and fresh small checks.
The initial measurements below have not been rewritten as follow-up results.

This is the implementation-machine report, not MacBook acceptance. The owner
must review, commit and push manually. No commit, push, merge, publication or
destructive data/image/volume cleanup was performed. No 100K/300K/1M/3M dataset
was generated, imported, processed or benchmarked here; no existing large study
or previously evaluated final holdout was reopened. New transaction fixtures
were hand-authored and small (mostly 4–64 transactions); a 9,000-row, two-column
embedding projection tested chunk boundaries without constructing transactions.

Starting checkout was clean at the reviewed commit
`d88af37c2377f6aed72604b72b2f116a87ae39b2`. Read-only inspection found no later
local commits; initial `git fetch origin` showed no subsequent `origin/main`
commits. There were no applicable AGENTS.md files. The retained worktree is the
new implementation, not an automatically committed release. Existing appliance
containers/images, datasets, reports and case vaults were left intact.

## Delivered behavior

The active checkout has **no local LLM/chat integration**: Ollama requests,
assistant module, finding-chat route/schemas, configuration/environment readers,
frontend panel/buttons/client and chat-only tests are removed. Current setup
instructions no longer download a model or require a cloud/tunnel. Historical
reports explicitly retain their former release scope; old unavailable-LLM checks
were not rerun or relabelled as post-removal measurements. Existing historical
images were not altered; use the reviewed rebuilt image for this release.

`structured-finding-evidence-v1` presents the exact proposition, responsible
version, supporting records, actual stored/source-backed opposing observations,
separately labelled reviewer assertions and plausible benign alternatives,
missing coverage, supported graph paths, features/baselines, decisions/reasons,
and immutable score/calibration provenance. Empty checked counter-evidence uses:

> No counter-evidence was identified within the supplied data and checked coverage.

Supporting/opposing references are separately replayable. Reference validation
checks case, SHA and receipt locator coverage; raw replay verifies immutable
bytes and returns 409 on corruption. Review citations pin their source hash,
including legacy locator-only citations. Stale/unverified opposition is not
presented as observed counter-evidence. Missing prevouts do not prove unrelated
funds; shared relays do not prove ownership. ECOD is unusual-feature context;
IF/candidate perturbations are descriptive score sensitivity, not causal proof,
criminality or a valid alternative transaction. Separate Stouffer structure and
burst contributions are exposed. Candidate probabilities are not passed through
the v2 Gaussian anomaly-tail calculation.

Finding ordering uses `family-context-round-robin-v2`: independent ranks within
snapshot/rule/version; analyst-escalated first, dismissed last; round-robin
family ranks inside each workflow band with stable ties. Raw 0–100 and 0–1 rule
scores are never compared across families by the review UI. This is workflow
prioritization, **not calibrated cross-family risk**. All findings remain
available through paginated lists; the separate case-authorized distinct-TX ML
top-K queue retains ties, capacity, zero capacity and outside-queue access.

The prior 11/12 benign merchant episode diagnostic remains a historical warning,
not a newly measured false-positive rate. Inspection of the report, merchant
constructor and small controls identifies overlapping mechanisms: repeated
customer receipts, variable values, consolidation and high-degree activity
legitimately trigger structural rules/anomaly triage. An episode being "touched"
does not itself establish a wrongly detected motif or suspicious interpretation.
We retained those observations, exposed benign alternatives/coverage and separated
observed patterns, anomaly triage, escalation and confirmed propositions. No
generator-ID allowlist, blanket benign classification or suppressed structural
match was introduced. **A reduced merchant false-positive rate is not claimed**;
the registered later validation must measure it.

Durable stages, complete/degraded/failed status, receipt recovery/idempotency,
confidence anti-join, version/applicability calibration and immutable pins remain.
Retries preserve prior successful scorer rows/reviews/pins and now also preserve
successful **zero-finding** scorer identity. Analytics recomputation still creates
its versioned revision rather than changing old evidence/confidence. UI shows
stage failures, coverage, eligibility/fallback and provisional-versus-final
activity. Signed graph continuation feeds an actual SVG investigation view in
both Graph Explorer and Evidence Package: <=500 loaded nodes/1,000 edges and
<=100 drawn nodes per visual window, with reset/window/cap controls. It does not
reconstruct the whole case or assume cross-window edges are absent.

## Procedure and performance boundaries

Default scoring remains **`anomaly-stack-v2`**, with the same scoring procedure,
reference fraction, seed, numeric fusion and eligible unsupervised fallback.
Its burst layer is **retrospective**. New descriptive explanation fields may
change newly written evidence hashes; old rows/pins are not rewritten. The
legacy release identity's ECOD "attribution" text is retained for manifest/hash
compatibility; current explanations explicitly identify it as feature-tail
context, not an IF/fusion attribution.

Justified optimizations implemented:

- FactStore constructs final typed integer arrays from bounded Arrow projections,
  avoiding transient int64/cast copies; immutable CSR grouping indexes are reused.
- Default v2 no longer fits eight unused stratified IF/ECOD models just to expose
  shape labels. The same scaler/KMeans labels remain; explicitly requested
  stratified procedures still execute. Tiny label and memory/bounded/parallel
  parity checks passed; large-scale parity is **not** inferred from them.
- Network concentration obtains global time bounds with SQL and retains only the
  existing bounded transaction-reference sample, avoiding a full Python list.
- Similar-wallet memmaps score in 8,192-row chunks with native per-chunk top-K and
  a compact global heap. `embedding-row-ascending-v1` explicitly versions tie
  ordering; bounded matrix products can differ in last-bit floating-point
  rounding from one large native product. This is not a claim of bitwise
  cross-platform embedding parity. Cursor handles are closed after normal queries.
- Path-signal SQL is limited to path entities; JSON export is paginated and full
  NDJSON export is streaming. Raw replay digest caching keys file identity/stat
  metadata and invalidates on changes. Rapid-spend cache and confidence anti-join
  fixes are retained with inclusive-boundary/order checks.

This is **not constant-memory ML/analytics**. Remaining global allocations include
TX/address dictionaries, feature/scoring arrays, network endpoint dictionaries,
clustering components, sparse embedding/SVD native scratch and propagation arrays.
Joint admission includes native SQL and reserve, parent/child worker budgets,
expanded candidate weights and disk-backed scratch. It checks configured budget
against current available/container RAM instead of simply increasing limits.
Int32 projections require fewer than 2^31 TX/input/output positions; the practical
supported snapshot size is lower and resource-dependent. Explicit failures keep
stages visible rather than disabling them. Estimates are conservative screens,
not measured 3M peaks; concurrent applications/VM pressure can still cause failure.

## Candidate lifecycle and quality eligibility

New `causal-structure-prior-bucket-v1` features use current transaction structure,
strictly earlier resolved parents and the **previous completed** 900-second bucket,
without future initialization, truth, generator IDs or criminality reviews. This
is a versioned new procedure, not a relabelling of v2 D. HGB, XGBoost, LightGBM and
hybrid use the same contract and bounded tuning. Separate permitted labelled
calibration, payload/manifest hashes, exact library versions, frozen fitted
weights, release ID, descriptive sensitivity, queue materialization, fallback
and reproducible transfer evaluation are wired into the actual product.

Artifacts are owner-trusted joblib, a code-execution trust boundary, never ordinary
user uploads. A separately reviewed manifest digest is checked before reading/
deserializing verified payload bytes; compact/expanded support limits and memory
admission apply. Synthetic/demo models require explicit synthetic case opt-in.
Real cases require a recorded representative-label approval and exact approved
domain. The owner approval is a judgement/provenance record, not automatic proof
that labels are representative. **No candidate was promoted to production.**
Untrusted, unavailable or ineligible candidates fall back to v2 with a visible
reason. Existing successful releases are retained on retry.

Later study registration reserves fresh finals before selection; validation-only
selection is frozen and hashed before final evaluation. Each final is one-use;
changing its truth hash does not allow a second selection attempt. Optional
pre-registered excluded families test unseen positives. Stress preparation is
label-free (missing/noisy IP/ASN, incomplete prevouts, reuse/degree, timing/value);
copied labels require an applicability review after transformation. Protocol
includes varied seeds/prevalence and benign payroll/merchant/exchange/batching/
consolidation. Episode/scenario overlap is recorded; entity disjointness is
explicitly NOT VERIFIED by scenario IDs alone. Independent small fixtures verify
wiring, not representative real-world quality.

Evaluation reports motif, surge and discrimination separately: AP (not accuracy),
prevalence, precision/recall, P@100 when population supports it, reviewer-budget
precision/recall, confusion counts, benign false positives, calibration/Brier,
per-family results, conditional cluster-bootstrap intervals and worst datasets.
The candidate uses frozen transfer; the comparator v2 fits label-free on each
dataset's reference and has retrospective D. No inference adaptation occurs.
No guarantee of AP/accuracy on arbitrary unlabelled uploads is possible. The
AP >=.85 preferred/.90 stretch, P@100 >=.90 and no unjustified discrimination
regression objectives remain **NOT RUN at representative scale**.

## Verification and reproducible evidence

Test discovery was audited before execution. The explicit small module list
below covers evidence/hash authorization, counter-evidence/coverage, independent
family ranking, queue/cursor/provisional activity, retries/pins, serial/parallel
and memory/bounded parity, causal cutoffs, candidate inference/serialization/
fallback/eligibility, metadata-only 3M admission, independent 64-row counts,
timeout/failure retention, portable collector fallbacks and all four CLI backends.
Final counts and durations are in the compact package and JUnit, rather than an
unqualified claim about the entire suite.

Final explicit-module pytest run: **146 passed, 0 failed, 0 skipped in 32.18 s**,
with one Starlette/httpx dependency deprecation warning. JUnit:
`var/implementation-20261004/review-ready-small.xml`.

```sh
uv run pytest tests/unit/test_analysis_acceptance.py tests/unit/test_research_contract.py tests/unit/test_entities_network_risk.py tests/unit/test_graph_flow_and_counts.py tests/unit/test_pattern_grouping_and_resources.py tests/unit/test_process_pool.py tests/unit/test_phase_six_crash_retry.py tests/unit/test_phase_four.py tests/unit/test_phase_four_one.py tests/unit/test_ml_pipeline_integration.py tests/unit/test_integrated_improvements.py tests/unit/test_integrated_boundaries.py tests/unit/test_macbook_orchestration.py tests/unit/test_quality_wiring.py tests/unit/test_appliance_acceptance.py -q --junitxml=var/NEW_SMALL_TESTS.xml
npm --prefix frontend run build
npm --prefix frontend run lint
node --experimental-strip-types --test frontend/e2e/graph-window.test.mjs
uv run python -m scripts.small_browser_acceptance --output var/NEW_SMALL_BROWSER --timeout 120
uv run python -m scripts.quality_matrix smoke --output var/NEW_64_ROW_QUALITY_WIRING
```

The authenticated Chrome check passed **42/42** on an isolated 64-TX SQLite store:
upload/all mandatory stages, finalized receipt view, original SHA/raw replay,
queue capacity 2/0, review history, signed graph cursor traversal and actual SVG
continuation, embeddings, synthetic risk seed/revision, network pages/export,
removed API 404 and no external requests/runtime JS errors. This is **not** copied
PostgreSQL/Mac/Docker isolation acceptance. Frontend build/typecheck and graph
window Node test pass. Frontend lint exits 0 with 21 pre-existing hook/fast-refresh
warnings; no new FindingsFeed dependency warnings remain. Python targeted Ruff
passes; pytest retains a dependency deprecation warning. SQLite additive schema
probe passes, preserving fixture rows and idempotency.

Raw retained diagnostics: `var/implementation-20261004/`. Successful final
browser: `browser-small-final/browser/result.json`; candidate CLI:
`quality-smoke-final-02/summary.json`; schema: `schema-tiny-final.json`.
Failed intermediate checks were retained and corrected (browser base URL/worker
budget/missing entity fixture/unknown POST route; candidate tail type). They are
not hidden or treated as passes. Compact sanitized underlying results, comparisons,
model/feature/calibration hashes, test summaries, protocol and reproduction
commands are in [`experiments/integrated_20261004/`](../experiments/integrated_20261004/).
Weights, credentials, browser session tokens and case evidence are not included.
All four serialized candidates were also reloaded with their trusted pins and
reproduced the retained tiny validation AP/Brier/confusion; see
`candidate-reload-parity.json`. This checks serialization wiring, not cross-platform
bitwise or large-population parity.

Intentionally deferred: `tests/test_phase_zero.py` (100K preparation),
`test_phase5a_100k_dataset.py`, `test_phase5a_fixture_v2.py`, full-suite execution,
large research studies/old finals, root generator, 1M/3M processing, actual
Mac/Docker resource screening, rebuilt appliance/PostgreSQL upgrade and large
parity/AP/30-minute gates. `test_phase_five_a_smoke.py`'s 10K generator was also
not needed; disposable-cleanup tests were not invoked. Deferred checks are not
passes and no existing data/image was deleted to make this phase fit.

## Requirement-to-code/test/evidence map

| Requirement | Code and small verification | Evidence/remaining execution |
|---|---|---|
| No LLM; preserved evidence/review | API/config/UI removal, structured evidence, main missing-API route; integrated tests/browser | Removed endpoint 404; historical report labels |
| Supporting/opposing/coverage/source authorization | evidence.py, storage/raw.py, review citations, StructuredEvidence | Empty/real/stale/corrupt/unauthorized fixtures and browser replay |
| Benign controls and family ranking | evidence interpretation, SQL family windows, FindingsFeed/Overview | Rank 100/1 test; merchant rate improvement NOT MEASURED |
| Recovery/status/pins/cursor/activity | pipeline/candidate retention, existing analysis/confidence/graph contracts | Retry/recompute/calibration/activity/cursor tests; bounded SVG browser |
| All-stage 3M resource path | bounded facts, ML/analytics optimizations, resource admission | Synthetic count estimates and tiny parity; 3M NOT RUN |
| Portable native appliance acceptance | telemetry, runtime_probe, macbook_benchmark, appliance_acceptance | Mac fallbacks/mock preflight/help, failed-result test; actual Mac NOT RUN |
| Candidate release lifecycle | candidate.py/candidate_findings.py, lifecycle CLI, case-mode UI | Serialization/inference parity, all-backend 64-row smoke, eligibility/fallback |
| Generalization protocol/results | lifecycle freeze/transfer/strata, quality_matrix stress/aggregate | Small wiring only; representative final AP/FP/calibration NOT RUN |
| Compact reproducibility | export_review_package, case_export, runbook | Underlying sanitized results + hashes, no private case bundle |
| Official PS provenance/compliance | docs/ps26146_compliance.md, existing observational requirement map | Official wording/source remains unresolved; no official-compliance certification |

Historical Oct3 1M acceptance remains documented as **1,014,581 canonical /
1,014,831 source rows / 250 quarantined**, **1,289.993 s** for that former
release/clock contract. It was not rerun after these changes and is not a pass
of the new upload-through-authenticated-findings gate. The future fresh 3M gate
is **NOT RUN**, not a promised extension of the historical 1M measurement.

The exact ordered Git/Mac preparation, generation/counting, screening/profiling,
fresh acceptance, UI/export, separate fresh-final quality study and compact return
package commands are in [macbook_runbook.md](macbook_runbook.md).
