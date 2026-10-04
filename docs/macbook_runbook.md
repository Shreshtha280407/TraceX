# TraceX owner-run MacBook acceptance

Status: **NOT RUN on the MacBook**. These commands implement preparation, resource
screening, fresh acceptance and separately registered quality evaluation. The
target remains **at least 3,000,000 canonical transactions through every mandatory
stage in less than 1,800 seconds**, including authenticated final findings
retrieval. Nothing below guarantees a time, AP or accuracy result. Do not execute
large commands on the implementation machine.

## 1. Owner review, commit and push (implementation machine)

No commit or push was made by the agent. Review the diff and compact results first.
The original integrated implementation started at `d88af37c2377f6aed72604b72b2f116a87ae39b2`
and was subsequently committed by the owner as `c70fcb4`. The current scoring/reporting
follow-up was subsequently committed as `84dd826`. The current grouped-review
work builds on that actual HEAD; see `docs/laptop_1m_runbook.md` for its owner-only
review/staging commands and measured resource blockers. Recheck upstream
before your commit if others have since pushed. Stage only the changes you reviewed;
the commands below assume the displayed changes are all intended.

```sh
git status --short
git diff --check
git diff --stat
git diff
git add -u
git add -- app/engine/evidence.py app/ml/candidate.py app/ml/candidate_findings.py app/ml/recipient_history.py app/ml/promotion.py app/telemetry.py
git add -- frontend/src/components/StructuredEvidence.tsx frontend/src/lib/graphWindow.ts frontend/e2e/graph-window.test.mjs
git add -- scripts/candidate_lifecycle.py scripts/quality_matrix.py scripts/case_export.py scripts/export_review_package.py scripts/macbook_benchmark.py scripts/runtime_probe.py scripts/small_browser_acceptance.py
git add -- tests/unit/test_integrated_improvements.py tests/unit/test_integrated_boundaries.py tests/unit/test_macbook_orchestration.py tests/unit/test_quality_wiring.py tests/unit/test_quality_matrix_reporting.py tests/unit/test_recipient_history_release.py
git add -- docs/macbook_runbook.md docs/integrated_implementation_2026-10-04.md docs/quality_matrix_reporting_2026-10-04.md docs/recipient_history_and_deployment_2026-10-04.md experiments/integrated_20261004 experiments/recipient_history_20261004
git diff --cached --check
git diff --cached
git commit -m "Add approved scorer routing, causal recipient context and fail-closed quality gates"
git push origin HEAD
git rev-parse HEAD
```

Do not add `.env`, `var/`, datasets, vaults, private case exports, browser tokens or
model weight files. Record the printed commit as `TRACEX_REVIEW_COMMIT` below.

## 2. Pull the exact reviewed commit without discarding Mac changes

Run inside the Mac checkout. If `git status --short` is nonempty, **stop** and
resolve/save your changes yourself; these instructions do not reset or auto-stash.

```sh
git status --short
git fetch origin
git switch main
git pull --ff-only origin main
export TRACEX_REVIEW_COMMIT=PASTE_THE_FULL_COMMIT_YOU_REVIEWED
git switch --detach "$TRACEX_REVIEW_COMMIT"
git rev-parse HEAD
```

If fast-forward fails, resolve divergence before continuing. Detached checkout
pins this experiment without rewriting your branch. Never substitute a newer
unreviewed commit silently.

## 3. Architecture and pinned dependency preparation (ONLINE)

Prerequisites: Docker Desktop with Compose, `uv`, Python 3.11, Node 22/npm, and
Chrome for browser verification. On a Homebrew-equipped Mac you can prepare tools
with `brew install uv python@3.11 node@22 libomp`; ensure the selected Node binary
is actually Node 22. Python 3.11 matches the appliance; frozen model loading also
requires exact recorded library versions. XGBoost/LightGBM are optional research
backends, not analysis network services. See their official
[XGBoost installation](https://xgboost.readthedocs.io/en/stable/install.html) and
[LightGBM installation](https://lightgbm.readthedocs.io/en/stable/Installation-Guide.html)
instructions if native OpenMP prerequisites are missing.

```sh
uname -m
node --version
uv --version
uv sync --python 3.11 --frozen --extra ml --extra research --extra diagnostics
npm --prefix frontend ci --no-audit --no-fund
export TRACEX_DOCKER_CONTEXT=$(docker context show)
docker --context "$TRACEX_DOCKER_CONTEXT" info
uv run python -m scripts.macbook_benchmark --help
```

Architecture is inspected at runtime: Apple Silicon uses native `linux/arm64`;
Intel uses native `linux/amd64`. No silent amd64 emulation is accepted. Lockfiles
pin Python and web packages; actual base-image IDs, PostgreSQL patch version and
runtime hashes are recorded rather than pretending floating base tags are fixed.

## 4–5. Offline Geo-IP resources, native image and PostgreSQL appliance

ONLINE preparation is separate from the later upload clock. Obtain country/ASN
resources once, preserving DB-IP/IPtoASN licences and checksum provenance.
If the build cache already exists, use verification, **not an overwrite**:

```sh
uv run python -m app.engine.geoip --dir var/geoip download
uv run python -m scripts.prepare_geoip_cache --source var/geoip/compiled
uv run python -m scripts.prepare_geoip_cache --verify-existing --source deploy/appliance/geoip-cache/compiled
export TRACEX_MAC_RUN=$(date -u +%Y%m%dT%H%M%SZ)
export TRACEX_IMAGE="tracex:mac-$TRACEX_MAC_RUN"
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" build --image "$TRACEX_IMAGE"
```

Skip the first two commands if restoring an existing checksummed build cache;
verify it with the third. The compiler verifies upstream package integrity and
the build verifies the compiled inventory. Runtime preflight rechecks that
inventory. Default image supports HGB/hybrid. To evaluate a frozen XGBoost or
LightGBM candidate inside the product, build a **new** tag with `build --image NEW_TAG
--candidate-backends` during online preparation; do not overwrite a reviewed image.

## 6. Host/container memory and disk admission

Set Docker Desktop's VM allocation manually using actual host availability and
macOS pressure, leaving OS, API and PostgreSQL headroom. Do not allocate almost all
32 GB to Docker or infer the worker budget from physical RAM. The current global
algorithms still need substantial joint memory; admission may correctly reject
3M. The code estimates compact arrays **plus** dictionaries, feature/scoring
scratch, native SQL and parent/child allocations. It cannot prove measured peak
usage before a run. Disk includes retained sources, generator outputs, vaults,
scratch/spill and WAL. All data remains retained; there is no automatic cleanup.

Choose `TRACEX_WORKER_MB` yourself from the actual VM/container availability. The
script returns estimates, current ceilings and time-varying recommendations;
revise the setting safely and use a new install if admission fails. It does not
force a particular Docker allocation. Begin with one worker; screening later can
compare additional admitted configurations.

```sh
export TRACEX_WORKER_MB=ENTER_A_SAFE_INTEGER_MIB_BUDGET
export TRACEX_INSTALL="var/mac-install-$TRACEX_MAC_RUN"
export TRACEX_PROJECT="tracex-benchmark-$TRACEX_MAC_RUN"
export TRACEX_BASE=http://127.0.0.1:8000
mkdir -p var/mac-reports
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" init --install "$TRACEX_INSTALL" --image "$TRACEX_IMAGE" --memory-mb "$TRACEX_WORKER_MB" --workers 1 --port 8000
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" start --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT"
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" preflight --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --output "var/mac-reports/$TRACEX_MAC_RUN-pre-generation.json"
```

Do not proceed on BLOCKED. `init` makes a fresh dedicated installation with random
0600 secrets and a marker; mutations require that marker and a
`tracex-benchmark-*` project. Preflight checks host CPU/RAM, Docker VM RAM/cores,
actual worker limits/budget, API/worker/PostgreSQL native images, code/library/DB
versions, native thread settings, offline resources, readiness and free space
inside both worker/evidence and PostgreSQL filesystems. Unsupported metrics are
null/explicit, not invented zeroes. A later runtime admission can still reject a
run if availability changes.

Mac loopback routing uses `TRACEX_UI_INTERNAL=false` to make Docker Desktop's
published browser port reachable. The worker and PostgreSQL remain internal;
analysis itself requires no cloud service. This Mac routing configuration is
**NOT** evidence of native-Linux strict API network isolation. For Linux's strict
internal deployment use `deploy/appliance/README.md` and its host-private bridge
instructions; do not reuse historical isolation measurements as Mac evidence.

## 7–8. Generate 3M with seed/manifest; independently count (Mac ONLY)

```sh
export TRACEX_DATASET="datasets/mac-3m-$TRACEX_MAC_RUN"
export TRACEX_COUNTS="var/mac-reports/$TRACEX_MAC_RUN-counts.json"
uv run python -m scripts.macbook_benchmark generate --rows 3000000 --seed "tracex-mac-fresh-$TRACEX_MAC_RUN" --workers 2 --output "$TRACEX_DATASET"
uv run python -m scripts.macbook_benchmark count --source "$TRACEX_DATASET/ingestion_rows.ndjson" --output "$TRACEX_COUNTS"
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" preflight --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_DATASET/ingestion_rows.ndjson" --counts "$TRACEX_COUNTS" --output "var/mac-reports/$TRACEX_MAC_RUN-exact-preflight.json"
```

Nominal `--rows` rounds to 100K parts and adds scenarios. Exact canonical/source
counts can exceed the nominal setting. Independent counting uses a bounded
SQLite disk index over raw rows; checks generator manifest source SHA and canonical
count; writes a separate `.provenance.json`. Truth is not read. Generation and
counting costs are preparation; source verification **inside** ingestion is timed.

## 9. Optional worker screening and profiling (Mac ONLY)

Use a **separate fresh** screen dataset so the acceptance run is not a resume.
Screening generates no data implicitly and never deletes its cases. The smaller
screen's time limit is diagnostic, not the 3M acceptance target.

```sh
export TRACEX_SCREEN="datasets/mac-screen-$TRACEX_MAC_RUN"
uv run python -m scripts.macbook_benchmark generate --rows 100000 --seed "tracex-screen-$TRACEX_MAC_RUN" --workers 2 --output "$TRACEX_SCREEN"
uv run python -m scripts.macbook_benchmark count --source "$TRACEX_SCREEN/ingestion_rows.ndjson" --output "var/mac-reports/$TRACEX_MAC_RUN-screen-counts.json"
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" screen --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_SCREEN/ingestion_rows.ndjson" --counts "var/mac-reports/$TRACEX_MAC_RUN-screen-counts.json" --workers 1 2 4 --minimum 100000 --target 7200 --output "var/mac-reports/$TRACEX_MAC_RUN-screen.json"
```

Select the reported `selected_safe_workers` (fewest workers within 5% of fastest
passing screen). Do not assume the screening optimum transfers to 3M. Apply it
only while the dedicated worker has no active job:

```sh
export TRACEX_SELECTED_WORKERS=ENTER_THE_REPORTED_SAFE_WORKER_COUNT
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" configure --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --workers "$TRACEX_SELECTED_WORKERS"
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" profile --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_SCREEN/ingestion_rows.ndjson" --counts "var/mac-reports/$TRACEX_MAC_RUN-screen-counts.json" --minimum 100000 --target 7200 --output "var/mac-reports/$TRACEX_MAC_RUN-profile.json"
python3 -m pstats "var/mac-reports/$TRACEX_MAC_RUN-profile.pstats"
```

Profile is optional: a distinct fresh run, restores the normal worker, and saves
diagnostics if the profile is unavailable. pstats covers the parent worker;
stage reaped-child CPU and resource samples provide complementary observations,
not native/child flamegraphs. Keep native thread caps at 1 during worker screening.

## 10–11. Fresh all-stage acceptance, then UI/findings

First open the UI at `$TRACEX_BASE`, register your own owner account, then use that
display name. `--user` securely prompts for its password; no token/password enters
argv or reports. This gives your browser account access to the benchmark case.
Without `--user` the harness uses a disposable random account solely for API checks.

```sh
export TRACEX_OWNER=YOUR_REGISTERED_DISPLAY_NAME
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" accept --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_DATASET/ingestion_rows.ndjson" --counts "$TRACEX_COUNTS" --minimum 3000000 --target 1800 --timeout 7200 --user "$TRACEX_OWNER" --output "var/mac-reports/$TRACEX_MAC_RUN-acceptance.json"
```

This starts at upload initiation, includes immutable source verification, receipt
ingestion/quarantine, UTXO graph, deterministic findings/features, ML fitting and
inference, clustering, embeddings and network/Geo-IP analytics. It stops only
after terminal snapshot and authenticated investigation-group/member evidence
retrieval for that snapshot. `investigation_grouping` is mandatory, not optional.
Provisional receipt activity time is recorded separately. A 7,200-second timeout
allows failure diagnostics; it does **not** weaken the strict `<1,800` gate.
No mandatory stage is disabled. A degraded/failed/mismatched scorer, count/hash
error, missing Geo-IP/embedding or sampler failure is not a pass.

Wrapper JSON points to unique `var/appliance-runs/<run_id>/result.json`, plus
`progress.ndjson`, `resources.ndjson`, service logs and per-stage duration/CPU.
It includes source/manifest/count hashes, exact code/dirty-tree/runtime identity,
container settings, stage outcomes, scorer/calibration identity and errors/exit
code. Memory samples run inside Linux containers; cgroup peaks are container
lifetime values (may include preceding screens), not reset per-run measurements.
Polling may miss very early output; null does not mean zero. A failed result must
be returned unchanged, identifying the longest stage and retained diagnostics.

Log into the UI, open the created case, verify the final analysis/scorer banner,
findings, evidence replay, queue capacity and bounded graph continuation. For a
private full streaming export, obtain the case ID from the **private** result:

```sh
uv run python -m scripts.case_export --base "$TRACEX_BASE" --user "$TRACEX_OWNER" --case CASE_ID_FROM_PRIVATE_RESULT --output "var/mac-reports/$TRACEX_MAC_RUN-private-findings.ndjson"
```

Never add this case export to GitHub.

## 12. Separate registered labelled quality study (Mac ONLY for large studies)

Never reopen prior evaluated finals for selection. Reserve new final paths before
selection and record a written distribution/label-applicability protocol. This
example compares equal causal features with bounded 64-tree/iteration candidates;
no truth enters application imports. Use independent seeds/prevalences and
scenario/episode-disjoint populations; audit overlapping entities separately.

```sh
export TRACEX_QUALITY="var/quality-$TRACEX_MAC_RUN"
mkdir -p "$TRACEX_QUALITY"
uv run python -m scripts.candidate_lifecycle register --protocol "$TRACEX_QUALITY/protocol.json" --final "$TRACEX_QUALITY/final-a" --final "$TRACEX_QUALITY/final-b" --final "$TRACEX_QUALITY/final-independent" --final "$TRACEX_QUALITY/final-missing_network" --final "$TRACEX_QUALITY/final-noisy_network" --final "$TRACEX_QUALITY/final-incomplete_prevouts" --final "$TRACEX_QUALITY/final-reuse_degree" --final "$TRACEX_QUALITY/final-timing_value"
uv run python generator.py --rows 100000 --seed "quality-train-$TRACEX_MAC_RUN" --scenario-scale 1 --workers 2 --formats ndjson --verify --output "$TRACEX_QUALITY/train"
uv run python generator.py --rows 100000 --seed "quality-cal-$TRACEX_MAC_RUN" --scenario-scale 0.75 --workers 2 --formats ndjson --verify --output "$TRACEX_QUALITY/cal"
uv run python generator.py --rows 100000 --seed "quality-validation-$TRACEX_MAC_RUN" --scenario-scale 1.25 --workers 2 --formats ndjson --verify --output "$TRACEX_QUALITY/validation"
uv run python -m scripts.candidate_lifecycle compare --protocol "$TRACEX_QUALITY/protocol.json" --training "$TRACEX_QUALITY/train" --training-truth "$TRACEX_QUALITY/train/ground_truth.json" --calibration "$TRACEX_QUALITY/cal" --calibration-truth "$TRACEX_QUALITY/cal/ground_truth.json" --validation "$TRACEX_QUALITY/validation" --validation-truth "$TRACEX_QUALITY/validation/ground_truth.json" --exclude-family peel_step --output "$TRACEX_QUALITY/comparison"
```

Use `uv run python generator.py` if Python is not your activated 3.11 environment.
`--exclude-family peel_step` registers an unseen positive family by excluding it
from all fitted/selection populations, leaving it in finals. Do not choose the
excluded family after seeing finals. Missing optional backends are UNAVAILABLE,
not claimed compared. Research admission screens dictionaries and retained
training/calibration/validation matrices; if rejected, use smaller permitted
preparation populations without substituting their quality for representative
large finals. Never tune a generator to favor the candidate.

Choose a candidate using **validation only**, accounting for all three tasks and
benign controls, not just best motif AP. Record the selection before final data
generation; generate registered finals without opening labels, then freeze
their source/truth fingerprints before evaluation. Set `TRACEX_MODEL` to one actual compared artifact
directory (`hist`, `xgboost`, `lightgbm`, `hybrid`).

```sh
export TRACEX_MODEL="$TRACEX_QUALITY/comparison/hist"
export TRACEX_MODEL_SHA=$(shasum -a 256 "$TRACEX_MODEL/manifest.json" | awk '{print $1}')
uv run python generator.py --rows 100000 --seed "quality-final-a-$TRACEX_MAC_RUN" --scenario-scale 0.5 --workers 2 --formats ndjson --verify --output "$TRACEX_QUALITY/final-a"
uv run python generator.py --rows 100000 --seed "quality-final-b-$TRACEX_MAC_RUN" --scenario-scale 1.5 --workers 2 --formats ndjson --verify --output "$TRACEX_QUALITY/final-b"
uv run python -m scripts.quality_matrix fixture --count 1000 --seed 41004 --role independent-final --output "$TRACEX_QUALITY/final-independent"
for TRACEX_VARIANT in missing_network noisy_network incomplete_prevouts reuse_degree timing_value; do
  uv run python -m scripts.quality_matrix stress --protocol "$TRACEX_QUALITY/protocol.json" --source "$TRACEX_QUALITY/final-a" --output "$TRACEX_QUALITY/final-$TRACEX_VARIANT" --variant "$TRACEX_VARIANT" --seed 51004
done
uv run python -m scripts.candidate_lifecycle freeze --protocol "$TRACEX_QUALITY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --validation-report "$TRACEX_QUALITY/comparison/comparison.json" --reason "OWNER_RECORDED_VALIDATION_SELECTION_REASON"
for TRACEX_FINAL in final-a final-b final-independent final-missing_network final-noisy_network final-incomplete_prevouts final-reuse_degree final-timing_value; do
  uv run python -m scripts.candidate_lifecycle evaluate --protocol "$TRACEX_QUALITY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --dataset "$TRACEX_QUALITY/$TRACEX_FINAL" --truth "$TRACEX_QUALITY/$TRACEX_FINAL/ground_truth.json" --review-budget 100 --output "$TRACEX_QUALITY/$TRACEX_FINAL-result.json"
done
uv run python -m scripts.quality_matrix aggregate --review-budget 100 --result "$TRACEX_QUALITY/final-a-result.json" --result "$TRACEX_QUALITY/final-b-result.json" --result "$TRACEX_QUALITY/final-independent-result.json" --result "$TRACEX_QUALITY/final-missing_network-result.json" --result "$TRACEX_QUALITY/final-noisy_network-result.json" --result "$TRACEX_QUALITY/final-incomplete_prevouts-result.json" --result "$TRACEX_QUALITY/final-reuse_degree-result.json" --result "$TRACEX_QUALITY/final-timing_value-result.json" --output "$TRACEX_QUALITY/worst-case.json"
```

Aggregation uses `quality-matrix-v2`. Missing/null/invalid required metrics make
the matrix **INCOMPLETE**, the affected worst-case value null, and affected gate
details **NOT EVALUABLE**, with dataset/report-specific reasons in `metric_coverage`
and `gate_details`. Missing benign-control counts are not zero. AP regression is
not evaluable without candidate AP and its reported comparison. P@100 is **NOT
APPLICABLE** only for a known labelled population below 100; unknown population
or missing P@100 in an eligible population is not excused. Mixed populations
retain every explicit exclusion and check all eligible datasets. Gate flags are
booleans; neither NOT EVALUABLE nor NOT APPLICABLE is true.

Product queue comparisons additionally require truth covering **all time-eligible
canonical transactions**, not just labelled pattern rows. The root generator's
truth omits scenario-funding transactions; those datasets can provide labelled-subset
AP but not measured whole-product-queue precision/recall without independent,
applicable labels for the omitted population. Do not invent negative labels or
change the generator to force a pass. `queue_comparison` reports **NOT EVALUABLE**
with exact eligible/labelled counts; aggregation then remains **INCOMPLETE** even
if subset metrics are finite. Such runs are useful diagnostics, not promotion.

`compare`/`evaluate --finding-budget` defaults to `0.01`, matching the product's
default `TRACEX_ML_REVIEW_BUDGET`; `--review-budget` defaults to 100 and means actual
reviewer K. If changing the product fraction, pre-register it and supply the same
`--finding-budget` to comparison/evaluation. Reports check candidate retained count
and the baseline's actual reference-threshold finding count can both exercise K.
Too few materialized findings is NOT EVALUABLE, not a full-population queue pass.

The output report is saved before `aggregate` exits: **2** for incomplete metrics,
**1** for evaluated thresholds that fail or cannot all be exercised, **0** only
when all four automated threshold checks are evaluable and pass. Read
`quality_gates_passed` together with per-task coverage; a 0 exit is not promotion,
representative-label approval, proof of acceptable benign FP/calibration, or a
claim that the default product now deploys that candidate.

Evaluate uses frozen fitted weights; test labels are opened only for metrics.
Preprocessing, classifiers and isotonic calibration are fitted solely on permitted
train/calibration populations. It consumes a one-use final marker. Baseline v2
instead refits label-free on each dataset's reference period and has retrospective
D; it is explicitly **not** frozen transfer or causal adaptation. No candidate
inference-time adaptation is performed. Per-task/per-family results include AP,
prevalence, P@100 where eligible, precision/recall, reviewer-budget recall, confusion,
benign false positives, Brier and conditional cluster-bootstrap uncertainty.
The independent fixture is an independently authored **wiring control**, not
representative real-world accuracy evidence. Stress variants are correlated and
retain source labels; an owner must review their applicability after perturbation.
Future independently authored positive families/representative real labels remain
essential for production generalization. Unlabelled manual uploads cannot measure
AP/accuracy. AP is not accuracy, and no arbitrary dataset guarantee is possible.

Optional product candidate: on a fresh dedicated compatible installation, mount
owner-trusted artifacts using the implemented command:

```sh
uv run python -m scripts.macbook_benchmark --context "$TRACEX_DOCKER_CONTEXT" candidate --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA"
```

Joblib is a code-execution
boundary: never trust user-uploaded model files; pin an independently reviewed
manifest before deserialization. Choose Synthetic/demo candidate only on an
explicitly synthetic case. New `causal-structure-recipient-history-v2` candidates
use `review-contrast-stable-txid-v2`: the separate discrimination/review-interest
target sets queue priority while motif and surge scores and structural findings
remain intact. Old `causal-structure-prior-bucket-v1` artifacts retain their
max(motif,surge) policy. Neither is criminality risk. Fallback retains v2 and records why.
To benchmark that candidate, explicitly pass its manifest `release_id` through
`accept --expected-release ... --scoring-mode synthetic_demo`; fallback is then
an acceptance failure, not a silent pass.

Production promotion is **manual**, not a consequence of label count or synthetic
AP. Supply owner-reviewed approval JSON with `representative_labels`, `domain`,
`decision_reason`, `approved_by`, `label_provenance`, `validation_reports`,
`limitations`; after reviewing that approval, run with every registered transfer
result (not a training table or the tiny unit-test demonstration):

```sh
uv run python -m scripts.candidate_lifecycle promote --protocol "$TRACEX_QUALITY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --approval OWNER_REVIEWED_APPROVAL.json --quality-result "$TRACEX_QUALITY/final-a-result.json" --quality-result "$TRACEX_QUALITY/final-b-result.json" --quality-result "$TRACEX_QUALITY/final-independent-result.json" --quality-result "$TRACEX_QUALITY/final-missing_network-result.json" --quality-result "$TRACEX_QUALITY/final-noisy_network-result.json" --quality-result "$TRACEX_QUALITY/final-incomplete_prevouts-result.json" --quality-result "$TRACEX_QUALITY/final-reuse_degree-result.json" --quality-result "$TRACEX_QUALITY/final-timing_value-result.json" --output "$TRACEX_QUALITY/approved-artifact"
```

Promotion now requires measurable motif/surge AP >=.85, all-task P@100 >=.90 and
no negative AP deltas, matched-capacity product queue precision >=.90 without
precision/recall/benign regression, and observed merchant FP reduction in at least
one dataset without increases in the others. Missing measurements block promotion.
Full eligible-population labels and sufficient materialized findings at the
registered fraction are required. The worker checks this approved fraction against
its actual setting; a mismatch retains v2. Reports must agree on fraction and K.
The unique `approved-artifact.promotion-decision.json` is saved even on a blocked
quantitative gate. These point estimates are not statistical significance or a
representative-label approval. The promoted weights/calibration bytes are copied
unchanged, retaining release identity; metadata adds exact report hashes and the
owner's applicability decision. It does not verify that assertion is true.

Install/pin the approved artifact with the same `candidate` command, using its
new manifest digest. New API/UI cases default to `auto_eligible`; provide the exact
approved `candidate_domain` and this installed candidate is selected automatically.
Blank/unknown domains, missing quality proof and demo-only artifacts retain v2.
No approved real-case artifact was installed in this implementation phase, so this
checkout's effective scorer for ordinary uploads remains v2 until owner validation,
promotion and pinning. Do not use the controlled unit-test artifact as real-case
approval. The acceptance command still defaults explicitly to unsupervised v2
unless the owner supplies candidate mode/domain/exact release.
Existing snapshots/reviews keep their pinned scorer. Explicit `validated_candidate`
and `unsupervised` modes remain available; no finding review becomes a transaction
criminality label. For a later fresh approved-candidate benchmark, use
`accept --scoring-mode auto_eligible --candidate-domain APPROVED_DOMAIN
--expected-release EXACT_ARTIFACT_RELEASE` in addition to the usual required
accept flags. Candidate memory/preflight includes fitted weights and the extra
recipient workspace; mismatched artifact pins/versions block preflight.

## 13. Export compact underlying evidence for review

Set `TRACEX_ACCEPTANCE_RESULT` to the actual unique result path printed by accept,
not the wrapper. Provide quality results and protocol, not just checksum lists:

```sh
export TRACEX_ACCEPTANCE_RESULT=var/appliance-runs/ACTUAL_RUN_ID/result.json
uv run python -m scripts.export_review_package --result "$TRACEX_ACCEPTANCE_RESULT" --result "$TRACEX_QUALITY/worst-case.json" --result "$TRACEX_QUALITY/comparison/comparison.json" --result "$TRACEX_QUALITY/protocol.json" --result "$TRACEX_QUALITY/protocol.json.frozen.json" --output "var/mac-review-package-$TRACEX_MAC_RUN"
```

Also add actual compact browser summaries and JUnit files with repeated `--result`
and `--junit` flags. Each input JSON must be <=8 MiB. The exporter retains sanitized
underlying metrics/stages and integrity inventory; it omits credentials, raw
sources, source references, case exports and weights. Manually review the package
before GitHub publication. Return this package plus failure/service logs privately
if needed: timings/resources, counts/hashes, mandatory-stage outcomes, active
scorer, eligibility/fallback, per-task quality/worst cases, errors and exit code.

## Small implementation-machine reproduction

Allowed without large datasets: subcommand `--help`, mocked preflight tests, the
explicit test modules listed in the implementation report, graph-window Node
test, frontend build and `uv run python -m scripts.small_browser_acceptance
--output var/NEW_SMALL_CHECK --timeout 120`. The latter creates only 64 transactions,
an isolated SQLite case store and same-origin web build; terminates only its own
API/worker processes and retains all diagnostics. It never runs Docker or root
generator large workloads. Browser prerequisites are prepared online separately.
