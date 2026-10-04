# Laptop grouped-review acceptance: exact ordered commands

Current fresh 1M result: **BLOCKED / NOT RUN**, not a benchmark pass. Native
Intel i5-13420H/16,366,317,568-byte RAM Linux laptop; existing SSD free space is
below the mandatory gate. Never force admission, delete data, skip a mandatory
stage or call a resumed job fresh. 3M is deferred; its existing tooling remains.

Commands below are implemented. `--help`, tiny wiring, metadata admission,
native PostgreSQL small acceptance and browser execution were tested. **Large
commands below have not been executed in this phase.** Execute them only after
every applicable preflight admits the actual resources. Keep all failures and use
new run/output/source paths for fresh attempts. Analysis requires no cloud/LLM;
online package/image/GeoIP preparation is separate from the upload clock.

## 1. Owner review and Git (not executed by agent)

Run in the current checkout. Existing unrelated styling belongs to the owner;
stage only reviewed files. Do not blindly add `var/`, datasets, credentials,
weights, vaults or private exports. Review the new file list as well as the diff.

```sh
git status --short
git diff --check
git diff --stat
git diff
git add -p -- app frontend scripts tests README.md deploy/appliance/README.md docs/macbook_runbook.md
git add -- app/api/group_routes.py app/engine/investigations.py app/ml/evaluation_identity.py
git add -- frontend/src/components/InvestigationQueue.tsx frontend/src/pages/InvestigationDetail.tsx
git add -- scripts/bounded_study_fixture.py scripts/group_quality.py scripts/laptop_admission.py scripts/grouped_release_report.py
git add -- tests/unit/test_investigations.py tests/unit/test_group_quality.py tests/unit/test_promotion_registry.py
git add -- docs/grouped_review_2026-10-04.md docs/laptop_1m_runbook.md experiments/grouped_review_20261004
git diff --cached --check
git diff --cached
git commit -m "Add durable grouped review and fail-closed model promotion; record laptop admission"
git push origin HEAD
git rev-parse HEAD
```

`git add -p` deliberately leaves unrelated changes unstaged unless you approve
them. On another host, stop on a dirty worktree; do not reset/stash automatically:

```sh
git status --short
git fetch origin
git pull --ff-only
export TRACEX_REVIEW_COMMIT=PASTE_THE_REVIEWED_FULL_COMMIT
git switch --detach "$TRACEX_REVIEW_COMMIT"
git rev-parse HEAD
```

## 2. Online dependency/resource preparation

Requires Docker Engine/Compose, uv, Python 3.11 (appliance compatibility), Node22
and Chrome. This host currently uses Python3.12 for research; model artifacts
require exact recorded runtime/library versions, so those fitted weights must
not silently load in the Python3.11 appliance. The deployed v2 refits locally.

```sh
uname -a
uname -m
docker context ls
docker --context default info
df -h . /var/lib/docker
uv sync --python 3.11 --frozen --all-groups --extra ml --extra research --extra diagnostics --extra dev
npm --prefix frontend ci --no-audit --no-fund
.venv/bin/python -m scripts.macbook_benchmark --help
.venv/bin/python -m scripts.laptop_admission --help
export TRACEX_RUN=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p var/laptop-reports
.venv/bin/python -m scripts.laptop_admission --root . --minimum 1000000 --output "var/laptop-reports/$TRACEX_RUN-disk-floor.json"
```

The disk-floor command allocates no transactions and exits **2** on BLOCKED.
Stop there if rejected. The strict conservative floor is 33,663,676,416 bytes
**even with zero source/input/output bytes**; exact source/scratch/WAL checks
need more. It is supported admission, not a measured minimum disk footprint.
Available RAM, active workloads and Docker/container limits must also admit the
joint algorithms. Do not allocate nearly all host RAM to Docker or assume GPU use.

On macOS use `uname -m` and the current Docker Desktop context. Native Apple
Silicon is arm64, Intel amd64; scripts inspect architecture and reject emulation.
`df /var/lib/docker` is native Linux only; Docker VM evidence/PG filesystems are
checked from their actual containers on every host. See `macbook_runbook.md` for
online Mac prerequisites/routing; no Mac performance claim is made here.

## 3. GeoIP and fresh native PostgreSQL installation

Verify the existing licensed compiled cache rather than overwriting it:

```sh
.venv/bin/python -m scripts.prepare_geoip_cache --verify-existing --source deploy/appliance/geoip-cache/compiled
export TRACEX_CONTEXT=default
export TRACEX_IMAGE="tracex:grouped-$TRACEX_RUN"
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" build --image "$TRACEX_IMAGE"
```

If absent, first prepare via `python -m app.engine.geoip --dir var/geoip download`
and `python -m scripts.prepare_geoip_cache --source var/geoip/compiled`, preserving
DB-IP CC-BY-4.0/IPtoASN PDDL provenance. This is online preparation, not analysis.
Never invent GeoIP observations. Optional `build --candidate-backends` installs
locked CPU comparators for an explicitly trusted candidate deployment, not needed
for the chosen v2. Do not mount the rejected research hybrid into this release.

Choose an integer worker MiB budget from actual available memory, leaving OS,
API, PostgreSQL, parent/child and native allocations headroom. On the measured
small installation 3,500 MiB/one worker was admitted; this does **not** establish
a safe 1M configuration. Fresh init refuses an existing directory.

```sh
export TRACEX_BUDGET_MB=ENTER_AN_ACTUALLY_SAFE_INTEGER_MIB_BUDGET
export TRACEX_INSTALL="var/laptop-install-$TRACEX_RUN"
export TRACEX_PROJECT="tracex-benchmark-laptop-$TRACEX_RUN"
export TRACEX_BASE=http://127.0.0.1:8794
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" init --install "$TRACEX_INSTALL" --image "$TRACEX_IMAGE" --memory-mb "$TRACEX_BUDGET_MB" --workers 1 --port 8794
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" start --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT"
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" preflight --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --estimate-transactions 1000000 --output "var/laptop-reports/$TRACEX_RUN-estimated-preflight.json"
```

Use another unused port if 8794 already serves the retained test installation.
Do not stop/reuse the owner's existing stack. The init marker and project prefix
enforce this boundary; new persistent named volumes retain evidence and PG data.
`--estimate-transactions` is metadata-only (1.5 inputs/2.7 outputs per nominal TX),
not proof of source counts. Checks include actual container memory/native threads,
GeoIP inventory, API readiness, native architecture, code identity, database/library
versions and Docker evidence/PG/host disk. Stop on rejection; do not bypass it.
Native Linux Docker's MemTotal is shared host RAM, not separate VM allocation.
Loopback UI routing is not strict native-Linux API-egress isolation evidence.

## 4. Small functional screening, not model selection

```sh
export TRACEX_SCREEN="var/laptop-screen-$TRACEX_RUN"
.venv/bin/python -m scripts.quality_matrix fixture --count 128 --seed 104031 --role deployment-verification --output "$TRACEX_SCREEN"
.venv/bin/python -m scripts.macbook_benchmark count --source "$TRACEX_SCREEN/ingestion_rows.ndjson" --output "var/laptop-reports/$TRACEX_RUN-screen-counts.json"
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" screen --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_SCREEN/ingestion_rows.ndjson" --counts "var/laptop-reports/$TRACEX_RUN-screen-counts.json" --workers 1 2 --minimum 128 --target 1800 --timeout 300 --output "var/laptop-reports/$TRACEX_RUN-screen.json"
```

Read `selected_safe_workers`; each setting gets an independent fresh case and
resource admission. Env is restored. A 128-TX time does not estimate 1M. The
measured host selected one worker; use it conservatively unless a separately
admitted representative screening workload supports a different choice.
Optional `profile` has the same source/count/minimum/target/timeout flags and a
unique `--output`; parent-only profiles are not native/child CPU flamegraphs.

## 5. Authorized large generation/count/preflight — ONLY AFTER ADMISSION

Do not run on this current resource-blocked installation. Generator preparation
and independently counting sources are outside the upload clock. Truth is kept
separate and never imported or used to fit on the acceptance file.

```sh
export TRACEX_DATASET="var/laptop-1m-$TRACEX_RUN"
export TRACEX_COUNTS="var/laptop-reports/$TRACEX_RUN-counts.json"
.venv/bin/python -m scripts.macbook_benchmark generate --rows 1000000 --seed "grouped-laptop-fresh-$TRACEX_RUN" --workers 1 --output "$TRACEX_DATASET"
.venv/bin/python -m scripts.macbook_benchmark count --source "$TRACEX_DATASET/ingestion_rows.ndjson" --output "$TRACEX_COUNTS"
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" preflight --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_DATASET/ingestion_rows.ndjson" --counts "$TRACEX_COUNTS" --output "var/laptop-reports/$TRACEX_RUN-exact-preflight.json"
```

Nominal rows are not canonical accepted count. The independent SQLite disk index
checks raw rows, unique TX/input/output/network counts, duplicate/quarantine
reconciliation and source/manifest hashes in `.provenance.json`. Acceptance
requires at least 1,000,000 distinct canonical TX and exact worker receipt counts.
If exact admission rejects after preparation, keep the data/reports and stop.

## 6. Fresh acceptance, ordinary owner UI and browser checks

Open `$TRACEX_BASE`, register your own owner account, then run with `--user` so
the new case remains accessible to that browser account. Password is securely
prompted, never placed in argv/report. Omitting it uses a disposable API-only
account. Do not use the acceptance labels for model selection/debugging and
subsequently call it an untouched quality final.

```sh
export TRACEX_OWNER=YOUR_REGISTERED_DISPLAY_NAME
.venv/bin/python -m scripts.macbook_benchmark --context "$TRACEX_CONTEXT" accept --install "$TRACEX_INSTALL" --project "$TRACEX_PROJECT" --base "$TRACEX_BASE" --source "$TRACEX_DATASET/ingestion_rows.ndjson" --counts "$TRACEX_COUNTS" --minimum 1000000 --target 1800 --timeout 7200 --expected-release anomaly-stack-v2 --scoring-mode unsupervised --user "$TRACEX_OWNER" --output "var/laptop-reports/$TRACEX_RUN-acceptance.json"
```

Clock starts before upload and ends after final authenticated review group/member
retrieval. All mandatory analysis stages, including grouping, must pass; no
resumed elapsed time substituted. Report upload, durable stage timings, retrieval,
first observed provisional output, counts/hashes, model/confidence/code identity,
memory/scratch/spill, errors and logs. Unsupported metrics remain null. Container
lifetime cgroup peaks may include earlier cases; sampled RSS peaks are not exact
run maxima. Diagnostic timeout is 7,200 s, target strictly **less than 1,800 s**.
Completed slower runs retain diagnostics and are TIME TARGET MISSED, not passes.

Through the UI check real group counts, backlog access at capacities 0/1/100,
member pages, separately labelled supporting/opposing replay, persisted group
reviews without member wrongdoing labels, graph bounds and cross-case denial.
Automated browser acceptance uploads a **separate small** case, not a second 1M:

```sh
cd frontend
TRACEX_E2E_BASE="$TRACEX_BASE" TRACEX_E2E_SOURCE="../$TRACEX_SCREEN/ingestion_rows.ndjson" TRACEX_E2E_OUTPUT="../var/browser-runs/$TRACEX_RUN" node e2e/acceptance.mjs
cd ..
```

Only a successful actual 1M result permits the PPT sentence with its measured
canonical count/time/CPU/procedure. Never extrapolate this laptop or a heavy host's
runtime, call AP accuracy, or equate 1M completion with 85–90% precision.

## 7. Separate bounded registered ML/group evaluation

Existing consumed finals are not reusable. New study directory and final paths;
freeze grouping/feature/queue procedure before labels. Predeclare independent
unseen families/domain/benign controls and applicable episode truth **before**
selection if claiming those validations; do not tune generator labels to win.
The following reproduces the bounded comparison wiring, not real-domain approval:

```sh
export TRACEX_STUDY="var/bounded-study-$TRACEX_RUN"
.venv/bin/python -m scripts.candidate_lifecycle register --protocol "$TRACEX_STUDY/protocol.json" --final "$TRACEX_STUDY/final-base" --final "$TRACEX_STUDY/final-shift" --final "$TRACEX_STUDY/final-coverage"
.venv/bin/python -m scripts.bounded_study_fixture --rows 5000 --seed "training-$TRACEX_RUN" --output "$TRACEX_STUDY/training"
.venv/bin/python -m scripts.bounded_study_fixture --rows 4000 --seed "calibration-$TRACEX_RUN" --output "$TRACEX_STUDY/calibration"
.venv/bin/python -m scripts.bounded_study_fixture --rows 4000 --seed "validation-$TRACEX_RUN" --output "$TRACEX_STUDY/validation"
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python -m scripts.candidate_lifecycle compare --protocol "$TRACEX_STUDY/protocol.json" --training "$TRACEX_STUDY/training" --training-truth "$TRACEX_STUDY/training/evaluation_truth.json" --calibration "$TRACEX_STUDY/calibration" --calibration-truth "$TRACEX_STUDY/calibration/evaluation_truth.json" --validation "$TRACEX_STUDY/validation" --validation-truth "$TRACEX_STUDY/validation/evaluation_truth.json" --review-budget 100 --finding-budget .01 --output "$TRACEX_STUDY/comparison"
```

Select using validation only and record the reason **before** generating finals.
Do not choose a winner from final tables. If none passes, explicitly retain v2.
For diagnostic transfer choose an actual compared artifact (this phase selected
hybrid diagnostically, not for deployment), then hash its frozen manifest:

```sh
export TRACEX_MODEL="$TRACEX_STUDY/comparison/hybrid"
export TRACEX_MODEL_SHA=$(sha256sum "$TRACEX_MODEL/manifest.json" | awk '{print $1}')
.venv/bin/python -m scripts.bounded_study_fixture --rows 5000 --seed "fresh-final-$TRACEX_RUN" --output "$TRACEX_STUDY/final-base"
.venv/bin/python -m scripts.quality_matrix stress --protocol "$TRACEX_STUDY/protocol.json" --source "$TRACEX_STUDY/final-base" --output "$TRACEX_STUDY/final-shift" --variant timing_value --seed 10403
.venv/bin/python -m scripts.quality_matrix stress --protocol "$TRACEX_STUDY/protocol.json" --source "$TRACEX_STUDY/final-base" --output "$TRACEX_STUDY/final-coverage" --variant missing_network --seed 20403
.venv/bin/python -m scripts.candidate_lifecycle freeze --protocol "$TRACEX_STUDY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --validation-report "$TRACEX_STUDY/comparison/comparison.json" --reason "YOUR_PRE_FINAL_VALIDATION_REASON"
.venv/bin/python -m scripts.candidate_lifecycle evaluate --protocol "$TRACEX_STUDY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --dataset "$TRACEX_STUDY/final-base" --truth "$TRACEX_STUDY/final-base/evaluation_truth.json" --review-budget 100 --finding-budget .01 --output "$TRACEX_STUDY/final-base-transfer.json"
.venv/bin/python -m scripts.candidate_lifecycle evaluate --protocol "$TRACEX_STUDY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --dataset "$TRACEX_STUDY/final-shift" --truth "$TRACEX_STUDY/final-shift/ground_truth.json" --review-budget 100 --finding-budget .01 --output "$TRACEX_STUDY/final-shift-transfer.json"
.venv/bin/python -m scripts.candidate_lifecycle evaluate --protocol "$TRACEX_STUDY/protocol.json" --artifact "$TRACEX_MODEL" --manifest-sha256 "$TRACEX_MODEL_SHA" --dataset "$TRACEX_STUDY/final-coverage" --truth "$TRACEX_STUDY/final-coverage/ground_truth.json" --review-budget 100 --finding-budget .01 --output "$TRACEX_STUDY/final-coverage-transfer.json"
.venv/bin/python -m scripts.quality_matrix aggregate --result "$TRACEX_STUDY/final-base-transfer.json" --result "$TRACEX_STUDY/final-shift-transfer.json" --result "$TRACEX_STUDY/final-coverage-transfer.json" --review-budget 100 --output "$TRACEX_STUDY/quality-matrix.json"
```

On Mac use `shasum -a 256` instead of `sha256sum`. Shifted derivatives are not
independent populations and retained surge truth needs review after perturbation.
Register further genuinely independent labelled populations beforehand for stronger
evidence. The default generator's incomplete truth cannot prove product-queue
precision: unknowns are not negatives. Missing/inapplicable metrics yield
INCOMPLETE/NOT EVALUABLE with dataset reasons, never passing gates.

Actual group precision additionally requires an unreviewed complete worker case
of the exact registered source, frozen eligible scorer and **independently declared
episode-proposition truth**. Do not use "contains any positive TX" as group truth:

```sh
.venv/bin/python -m scripts.group_quality --base "$TRACEX_BASE" --case YOUR_AUTHORIZED_FRESH_FINAL_CASE_ID --dataset "$TRACEX_STUDY/final-base" --protocol "$TRACEX_STUDY/protocol.json" --truth PATH_TO_PREDECLARED_EPISODE_TRUTH --capacity 100 --max-groups 10000 --output "$TRACEX_STUDY/final-base-groups.json"
```

Supply case owner's token securely through `TRACEX_REVIEW_TOKEN`, never argv,
Git or exported logs. Capture group quality **before** transfer evaluation and
provide its file with `evaluate --group-quality-result` on that one-use final.
The example without applicable group truth reports NOT EVALUABLE, not fabricated
precision. Unknown/mixed/overmerged groups fail closed; fragmentation is reported.
Promotion `--help` specifies required `--protocol`, artifact hash, every final
report and owner approval. Do not promote this rejected diagnostic artifact.
No transaction-wide criminality labels are learned from group/finding reviews.

## 8. Compact reports to return for review

Large acceptance wrapper points to unique `var/appliance-runs/RUN_ID/result.json`;
retain progress/resource NDJSON and API/worker/PG logs privately even on failure.
Export actual JSON tables, not only checksums:

```sh
.venv/bin/python -m scripts.export_review_package --result PATH_TO_ACTUAL_ACCEPTANCE_RESULT_JSON --result "$TRACEX_STUDY/quality-matrix.json" --result "$TRACEX_STUDY/final-base-transfer.json" --result "$TRACEX_STUDY/final-shift-transfer.json" --result "$TRACEX_STUDY/final-coverage-transfer.json" --output "var/compact-review-$TRACEX_RUN"
```

Manual review remains necessary before publication. Include timings/counts/source
and model/calibration/grouping hashes, stage outcomes/resources/errors, scorer
eligibility/fallback, task/prevalence/queue applicability and group quality.
Do not copy truth rows, sensitive case sources, private full exports, tokens or
model binaries into Git. Current phase's compact package has readable release
decision, underlying metric tables, tests and browser summary; blocked 1M values
remain null, not invented numbers.

To re-export **this phase's already measured summaries only** into a new exclusive
directory (no training, labels, uploads or final evaluation are executed):

```sh
.venv/bin/python -m scripts.grouped_release_report --study var/grouped-study-20261004-03 --admission var/grouped-benchmark-20261004-01/final-native-1m-admission.json --preflight var/grouped-benchmark-20261004-01/final-native-1m-preflight.json --small-acceptance var/grouped-benchmark-20261004-01/final-native-acceptance.json --browser var/browser-runs/grouped-review-20261004-04/result.json --screen var/grouped-benchmark-20261004-01/worker-screen-small.json --junit var/grouped-benchmark-20261004-01/release-tests.xml --frontend-summary var/grouped-benchmark-20261004-01/frontend-final-summary.json --output "var/grouped-compact-review-$TRACEX_RUN"
```

The repository's final sanitized package is
`experiments/grouped_review_20261004/final/`; the parent package retains an earlier
verification generation. The full private inputs listed above are deliberately
not committed. Every export refuses an existing output directory.
