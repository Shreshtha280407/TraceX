# TraceX implementation acceptance and manual-review report

Implementation started 2026-10-03; final verification/handoff completed
2026-10-04 in Asia/Kolkata. Artifact names retain the original run date.

Final manual-review handoff for the user's conditional 1M scope: copied-image07
PostgreSQL 1M passes in **1289.993 s (21.50 minutes)** versus image06's
1388.134 s, a **7.07% observed elapsed reduction**. Every required stage remains
enabled. The complete published 1M ML findings and confidence pins agree exactly;
production scoring remains v2. This is one workstation pair, not a guarantee of
minimum runtime, improved production AP or accuracy on every future upload.

The 3M full-ML admission remains unsupported on this worker. Higher synthetic
ML targets, independent real-data validity, official-PS provenance and actual
grounded local-chat evaluation remain unpassed or unavailable. The implementation,
experiments and handoff are delivered with those explicit qualifications;
**not all release gates pass**. No weaker model or disabled stage substitutes
for a failed gate. The user authorized the 1M fallback when 3M is infeasible.
No staging, commit, push, merge or publication has been performed.

## Scope and baseline

Source: `TraceX_Tomorrow_Implementation_Plan (1).pdf`, 13 pages, SHA-256
`b9cde7f00249fccc89fd028ca58e92981e39f5dcbd4b1ae7598f75f75565c6e0`.
All work is uncommitted on `e469a82a24a177288a331a79eef8fe5d6793e29c`.
A fresh GitHub fetch gives HEAD=origin/main and ahead/behind 0/0. Claude's
`e4132ed` (8,237 insertions) and `e469a82` (302 insertions) are present; the
failures were real allocation, shared-spill and disk problems, not absent code.
The earlier [steps 1–5 checkpoint](implementation_checkpoint_2026-10-03.md)
is historical; the user subsequently authorized the continuation and verified
disposable-data retirement. Unrelated code, the root `.env`, real case data,
and the user's older native `tracex` Docker stack are preserved.

Environment: Arch Linux x86_64, kernel 7.2.4-arch1-2, i5-13420H, 12 logical
CPUs in affinity, 15.2 GiB physical RAM, 16 GiB swap, approximately 202 GiB
ext4 root filesystem. Available RAM/disk vary with desktop applications and
Docker Desktop. Host Python 3.12.13, uv 0.12.10, SQLite 3.50.4; DuckDB 1.5.5,
PyArrow 23.0.1, numpy 2.5.3, sklearn 1.9.1, scipy 1.18.1, SQLAlchemy 2.1.1,
psycopg 3.3.6. Copied-image Python is 3.11.17 with numpy 2.4.6/scipy 1.17.1
selected by the same lock's Python markers. Different runtimes are not
implicitly claimed bit-identical. Actual container versions and every deployed
`app`/`workers` Python SHA are captured in the appliance benchmark results.

| Frozen identity | SHA-256 / value |
| --- | --- |
| `uv.lock` | `d890653b76319c9a2794c59ae234659ee70940ec1706540383f99bee3fa2415f` |
| `frontend/package-lock.json` | `727baec17e38f4ee0e8d04e479b0ef3e21164b6e55fcd7598712849ad5a213ff` |
| Production scoring procedure | `anomaly-stack-v2`; no synthetic supervised promotion |
| Production manifest | `eaa862db090baf74db92ff69cddc53eb89e44389f48734eae381f9180542dda4` |
| Research protocol | `e025b2d987d80574407b99057a68b5905f6bb50dc3e4644ee6da9daf939fea7c` |
| Research feature-column contract | `34db72ce46c2a53e963b7e8c452cf2ffb21c3adcfd05b3c10ff20b19e414d9b8` |
| Active numerical calibration maps | `confidence-v1`, unchanged; new provenance/applicability pins |
| Deployed image07 calibration registry file | `ee850e84dffd5875bd40a1007a960b6cc50d8b29d99712dce4d4e6651595e789` |
| Deployed image07 bounded engine file | `3e63c288fc004a8849c814db75200da6dfc96ab34f2c4d5ac2a89f70d54b7063` |
| Image06 comparison bounded engine file | `abdae97bd779656d4088ba648ff2cb22f973c616d26b15cc5b678da511bc6c0e` |
| Deployed image07 confidence code | `b489d3d61006980d87e4aac587e56a23c4b659ff668d6b26538ac18447fed206` |

Each benchmark separately records its actual dirty diff, untracked hashes,
runtime-source hashes, lock hashes and configuration. Documentation/test/helper
edits after an image build do not retroactively change that image's source.
Image IDs and deployed runtime-source hashes, not an unchanged Git commit
alone, identify the tested uncommitted implementation.

## Requirement matrix

IDs refer to PDF sections. PASS means the stated implementation/check has
evidence, not a universal precision guarantee. Timing, research and deployment
are separate gates. BLOCKED/NOT RUN does not mean successful.

| ID | Implementation / verification | Observed result | Status |
| --- | --- | --- | --- |
| 01–02 baseline / provenance | Unique runs; Git fetch; lock/source manifests; historical checkpoint | Claude base present; failures retained; no commit/push | PASS |
| 02 official PS provenance | `docs/ps26146_compliance.md`, data manifest | Official PS unavailable locally; Dataset Link Nil; plan is not official PS | BLOCKED/NOT RUN |
| 03 evidence / authorization | Canonical hashes; `test_analysis_acceptance.py`, mixed-format/replay/access suites; browser raw replay | Original-byte SHA/locators, integer values, receipt visibility, no guessed spends; new routes/cursors case-authorized | PASS |
| 04 stage outcomes / retries | `app/jobs/analysis.py`, `analysis_routes.py`, pipeline/request models; recovery/degradation tests | Durable per-stage reasons; complete/degraded/failed status in API/SSE/UI/export; retries preserve receipts/reviews/pins | PASS |
| 04 strict benchmark | `scale_benchmark.py`, `appliance_acceptance.py`, failure-path tests | Mandatory-stage/model/count/Geo-IP/analytics failures exit nonzero and retain results/logs; no auth-error retry | PASS |
| 05 bounded optimizations | Typed SQL/Arrow, bulk graph, SQL windows, 8192-row exports, isolated spill dirs; profiling and full parity | Same canonical graph/features/findings; shared-spill reproduction closed by independent directories | PASS |
| 06 supported global limits | `app/resources.py`, bounded ML and analytics/risk estimates; resource tests | No constant-memory claim; 3M ML estimate 7884 MiB exceeds tested 5120 MiB budget | PASS implementation; resource limit remains |
| 06 100K parity / worker screen | Saved full canonical and published ML/pin fingerprints; serial 300K w1/2/4/6/8 | Exact 100K parity; 300K w1/2/4 pass; 6/8 native budgets fail and now reject earlier; choose 4 | PASS supported configurations |
| 06 equivalent old 300K speedup | Untouched 100K failure and retained historical controls | No successful equivalent untouched 300K control; no invented matched speedup | BLOCKED/NOT RUN |
| 06 SQLite 1M completion | `ladder-1m-w4-20261003-final-01/result.json` | 1,014,581 TX, every stage complete, 1444.038 s, 5415 MiB sampled tree RSS, no OOM | PASS |
| 06 SQLite 3M attempts | First upload and receipt recovery results, logs/archives | All 3,043,062 TX ingested; graph completes on recovery; next peeling grouping hits old quarter-budget; both phase budgets corrected, 100K parity passes | FAIL attempts retained |
| 06 PostgreSQL 1M / <=1800 s | Copied-image07 real bearer-authenticated upload, no host-code worker; paired image06 comparison | PASS 1289.993 s versus 1388.134; every stage/count/model/Geo-IP/analytics/embedding verified; all published ML/pins equal; sampled worker tree5681 MiB | PASS 1M supported workload; single-pair 7.07% observed improvement |
| 06 PostgreSQL 3M full-ML | Actual deployed-worker admission against exact 3M counts | 8267692976 bytes /7884 MiB >5120 MiB budget; preflight unsupported, NOT a fresh 3M end-to-end run; prior real SQLite attempts retained | BLOCKED resource gate; full PG NOT RUN |
| 07 calibration / explanations | Compound registry matching; immutable `FindingConfidence`; chronological diagnostics; old-pin/mismatch tests | Shift/legacy unknown; synthetic calibration labelled; network 1−p not posterior; ECOD descriptive, bounded IF sensitivity and A/D contributions separate | PASS honesty/provenance |
| 07 uniformly improved calibration | Brier/ECE/bins/sample counts in retained diagnostic | Peeling worse than constant-rate comparator; rapid redistribution poorly calibrated; no map promotion | FAIL quality claim, safe baseline retained |
| 08 separate top-K queue | Authorized queue route; FindingsFeed; tie/cap/zero/filter tests; actual UI K=2/0 | Distinct TX denominator, floor/fraction semantics, stable ties, no fill, threshold findings retained | PASS |
| 08 graph continuation | Query/case/snapshot-bound untrusted cursor; GraphContinuation/EvidencePackage; auth/tamper/stale tests | Actual UI Load more and 46-page traversal match full graph without missing/duplicate rows | PASS |
| 08 provisional activity | Receipt-derived read model/status/SSE; interruption/reconciliation tests; both browser demos | Provisional activity observed before completion; finalized receipt view; no uncommitted or guessed links | PASS |
| 09 controlled experiments / analyst labels | `ml_controlled_study.py`, `analyst_labels.py`; E1–E7 and causal/cutoff/label tests | All registered candidates measured on 100K/300K/1M; optional native research deps excluded from production; representative-label gate/fallback explicit | PASS synthetic research |
| 10 fresh reserved evaluation | Frozen selection and two exclusive final markers; registered observable stress, prevalence variation, cluster bootstrap | Frozen E4 evaluated once; final/reference entities/episodes disjoint; no final retuning; same-generator limitations disclosed | PASS registered experiment |
| 10 AP/P@100 project gates | Detailed model decision and final metrics | >=.80 point AP and >=.90 P@100 pass; final point AP >=.85; .90 surge stretch and all-dataset .85 surge preferred target missed | PASS initial; FAIL missed higher targets |
| 10 real-world / strong positive-family validity | Representative real labels and independent authored data absent | Benign payroll holdout is not positive-family generalization; no arbitrary judge-data guarantee | BLOCKED/NOT RUN |
| 11 adoption / older evidence | Unchanged v2 manifest, explicit research decision, old finding/pin tests | Retain eligible unsupervised v2; no v3 branding or synthetic-trained production model | PASS fallback decision |
| 11 copied offline install / no egress | Bundle07 manifests/checksums; native review project; IPv4/IPv6 runtime probe | Image absent in target before install; 39.606 s copied install, no registry pulls; API/worker/PG internal networks; host internet unchanged | PASS scoped isolation |
| 11 PostgreSQL investigation demo | Browser smoke08/unfamiliar06/missing05 cases; image07 | 42/42, 70/70 and 31/31; entity/embedding/network, risk seed, raw replay, review history and export verified | PASS |
| 11 optional local chat | Prompt-contract/malicious-source tests; real inside-appliance request | Actual HTTP503 unavailable state; investigation unaffected; no Ollama/model installed | PASS unavailable handling; grounded-model checks NOT RUN |
| 12 backend/frontend/migrations | Full JUnit, Ruff, frontend build/lint, fresh SQLite and image07 PG upgrade probes | 240 tests pass; frontend typecheck/build/lint succeed; both migrations preserve fixture rows and are idempotent | PASS; mypy NOT RUN |
| 13 handoff | This report, model decision, `experiments/implementation_review_inventory_20261003.json` | Local checksum index and uncommitted manual-review handoff; failed/unavailable gates retained separately | PASS handoff, not blanket release approval |

## Correctness and performance evidence

Latest fresh 100K pipeline: `rapid-cache-parity-100k-20261003-final-01`,
2 workers / 2600 MiB / forced bounded, all mandatory stages plus Geo-IP,
103.203 s and 2381 MiB sampled tree peak. It overlapped diagnostic tests/builds;
this is not a clean throughput comparison. Its canonical and full published ML
reports are `experiments/runs/rapid-cache-{canonical,full-ml}-parity-100k-20261003.json`.
The comparison uses saved verified fingerprints after approved retirement of
old reference vaults; their original comparison report hashes are pinned.

| Complete compared artifact | Rows | Canonical SHA-256 |
| --- | ---: | --- |
| Graph nodes | 589,987 | `2a5c2dd5783d93ed8c04f043fdab98fce899794bf795b7c02097feb22d4ca1c1` |
| Graph edges | 970,260 | `76fa95cf4106d703c52ff9cc321b8f4a324c503669a215f375a59294d0f5c064` |
| Address-window features | 833,912 | `9d00b6e283ede09570cab4565a484ce363a35d3d994a9fabfb9b3838f2843254` |
| Deterministic findings | 21,876 | `673457a8fc1bab20d4da1daae165dea6ab6c003199ead3d4f41aa5320bacffc5` |
| Full published ML findings, normalized run IDs | 1,223 | `b0117f01317411af3b2d3f65d5b60610634d70594f681436941967d8f21d78b8` |
| Pinned ML confidence provenance | 1,223 | `7edb679fd802fe1597fa4ba15a8ed5cdf0280d93e64693ecaf73eb6394850f87` |

Original feature hashes and model-run links are independently verified before
normalizing only case/source/snapshot/graph/job/model IDs between fresh imports.
Scores, ranks, features, locators, explanations and pins are not excluded.
Earlier same-snapshot memory vs bounded w1/w2 full-field parity also passes.
Against the older untouched frozen ML metadata, only the explicitly weaker
scoring/evidence comparison is claimed: new honest explanations/IF sensitivity
are an intentional metadata addition, not a hidden numerical change.

Matched 303,850-TX serial screen at 5120 MiB: w1 395.054 s/3664 MiB; w2
337.532 s/3779 MiB; w4 291.829 s/5053 MiB. Six/eight fail at 193.483/205.566 s
under insufficient native child export budgets. Graph/features/deterministic
hashes agree across w1/2/4. Result: select four, the smallest within 5% of the
fastest passing configuration. Older 515.312 s profiling shared CPU with a
build. Parent profile hotspots include ordered-process waits/IPC, Parquet/feature
part copying and confidence pinning. Typed projections, SQL windows, compact
intermediates and batched pins address these; no unjustified overall speedup
over a successful equivalent untouched 300K control is asserted.

| SQLite mandatory stage | 1M successful attempt seconds | 3M original / recovery seconds |
| --- | ---: | --- |
| Source verification | 1.415 | 6.810 / 6.722 |
| Ingestion | 244.910 | 836.188 / receipts reused |
| Graph | 186.573 | 437.344 failed / 587.318 complete |
| Deterministic findings/features | 708.767 | not reached / 15.829 failed |
| ML | 141.748 | not reached |
| Entity/network/embedding analytics | 151.846 | not reached |

The first 3M run fails at 1303.161 s, 3894 MiB tree peak; recovery fails at
611.503 recovery-only seconds, 3623 MiB. Neither is a <=1800-second full
success. Recovery publishes 17,885,618 nodes / 29,372,118 edges, 4,388,594
resolved inputs, 45,917 missing outpoints, zero double-spend conflicts and
zero complete-prevout value violations. Quarter-budget native hash/grouping
joins—not missing GitHub code or disk exhaustion—cause the recorded failures.
Serial graph/peeling SQL now uses the planned half-budget/four-thread maximum;
exact detector settings are restored before pools run. Global ML remains an
explicit supported limit, not partitioned/disabled to fabricate a pass.

The first copied-image PostgreSQL 1M attempt is also retained as FAIL,
1975.723 seconds, 5808 MiB worker-tree peak: ingestion, graph and deterministic
findings complete, then confidence pinning during ML stalls. Read-only EXPLAIN
shows the old `NOT IN` predicate materializing the entire 233512-row estimated
pin set repeatedly (estimated total cost640638978.58). A correlated `NOT EXISTS`
produces a hash anti-join (cost72172.18); these are planner estimates, not
measured timings. The pin primary key is non-null, so the selected rows are
equivalent. Only the exact stalled query was canceled; the worker recorded
`QueryCanceled` and the benchmark exited nonzero. No completion or speedup is
claimed for this interrupted attempt. Image06 includes the replacement,
missing-pin/idempotency regressions, and the fresh 100K complete-output parity
above. Evidence: `experiments/runs/confidence-postgres-plan-diagnosis-20261003.json`.

The fresh copied-image06 PostgreSQL 1M run
`var/appliance-runs/offline-postgres-1m-20261003-final-02/result.json` passes
all stages in1388.134 seconds. Source verification .858 s, ingest274.291,
graph181.620, deterministic findings/features640.350, ML126.274 and
analytics155.561. ML scores1014581 transactions and writes10133 findings;
74978 entities,919789 wallets,420222 embeddings and183 network findings
are verified. Canonical input/output/observation counts match independently
computed expectations. Graph5958002 nodes/9784767 edges,1462500 resolved
inputs,15427 missing prevouts, zero conflicts and zero complete-prevout value
violations. Worker-only RSS4405 MiB, sampled worker-tree5779 MiB; API405,
PostgreSQL tree1253. Sampled scratch peak8156412347 bytes, spill2801532928,
minimum filesystem free27131322368. Scratch is logical, all review cases,
sampled ~5 seconds; timings include observer overhead and brief read-only
diagnostics, not a controlled laboratory workload or proven global minimum.

After the user changed priority to 3M only if feasible, otherwise further 1M
optimization, the actual deployed worker rejected the exact 3M global-ML
preflight. Required8267692976 bytes, planning5368709120, available7786139648
at observation. Even raising the planning number would not satisfy the 80%
available-memory safeguard. No arrays were allocated or stages disabled;
this is explicitly NOT a new PostgreSQL 3M pipeline measurement. Evidence:
`experiments/runs/offline-postgres-3m-ml-resource-preflight-20261003.json`.
3M full-ML/<=1800 s remains unpassed. The user authorized focusing on 1M under
this condition; the resource blocker is not misreported as successful completion.

Final image07 optimization: cache the exact rapid-spend relation once before parallel
address windows, instead of repeating global prevout/time joins for each of
48 partitions in the 1M plan. Raw JSON/source sequence, actual prevouts and
inclusive0..3600-second bounds remain unchanged. Bucket order permits native
zone-map filtering. Serial construction restores child memory/thread limits
in `finally`. Unit tests independently compare the original SQL for1/7/48
partitions, duplicate spenders, missing prevouts/times, blank/NULL addresses,
pre-epoch and boundary delays, including restoration after native failure.
Fresh100K all-stage PASS103.203 s/2381 MiB, exact full canonical and published
ML/confidence pins: `experiments/runs/rapid-cache-{canonical,full-ml}-parity-100k-20261003.json`.
The independent 1M comparison verifies all180884 changed relation rows, original
raw fields and unique input sequences, with zero wrong-partition rows. Original
and cache SHA both `0998e3b07092f48c56274b6557607e5001ea6ef9fabc4c2e6ac727b1ea7082ee`.
All634 baseline receipted artifacts are verified first. This158.086-second
diagnostic includes staging and hashing, NOT full-pipeline throughput.
Report: `experiments/runs/rapid-spend-parity-1m-20261003.json`.

The fresh image07 paired full pipeline passes in1289.993 seconds. Identical
immutable source, lockfiles, manifests, PostgreSQL version, Python/library
versions, settings, production manifest and calibration hashes are recorded;
the sole deployed `app`/`workers` source difference is `app/engine/bounded.py`.
Expected canonical counts, graph counts/coverage and all-stage checks pass.

| PostgreSQL mandatory stage | Image06 baseline seconds | Image07 seconds |
| --- | ---: | ---: |
| Source verification | .858 | 2.359 |
| Ingestion | 274.291 | 273.931 |
| Graph | 181.620 | 202.187 |
| Deterministic findings/features | 640.350 | 537.955 |
| ML | 126.274 | 121.740 |
| Analytics | 155.561 | 140.976 |
| Full upload through strict terminal inspection | 1388.134 | 1289.993 |

Findings/features elapsed falls15.99%; full elapsed falls98.140 seconds/7.07%.
This is a single sequential pair on a shared desktop, not randomized repeats,
an isolated causal speedup interval or a global minimum. Neither run is a
successful untouched-original-code control. No other scale run or heavy build
overlapped candidate timing; brief focused tests/read-only diagnostics did.

Image07 sampled worker-only RSS4752 MiB and tree5681 MiB; API421, PostgreSQL
tree1360. Tree RSS is lower, but parent/API/PG RSS is higher than image06, so
not every resource metric improved. Sampled scratch8237382075 bytes (about1%
higher), spill704118784 (lower), minimum free17645936640. The baseline case was
retained throughout; free-space differences are not exclusive candidate scratch
costs. Container-lifetime cgroup peaks include file cache/prior cases and are
not per-run process RSS. Resource sampling can miss peaks/double-count pages.

All10133 published ML rows agree exactly across these two PostgreSQL imports:
SHA `8af1c0621249e9561dcc0a38b3a9c2879ef3c85b730da0a6b34edd2ae975fc64`;
complete confidence-pin SHA
`ca17f8f8358a1058eddc02862f014a1b574edde2cda2fbd6db56e2d7ca7673a3`.
Original feature hashes and model links are verified before normalizing only
known run IDs; scores/ranks/features/explanations/evidence/status/pins remain
in comparison. This is full PUBLISHED ML parity, not all unpublished feature
vectors or complete1M graph/features/deterministic canonical parity. The latter
is established completely at100K; equal1M counts alone are not full parity.
Reports: `experiments/runs/appliance-full-ml-parity-1m-20261003.json` and
`experiments/runs/paired-appliance-1m-performance-20261003.json`.
The speed optimization is retained with this tested-output evidence, without
claiming improved production AP or a new scoring/model release.

## Dataset identities and quality decision

| Dataset | Accepted TX / source rows / quarantine | NDJSON SHA-256 |
| --- | --- | --- |
| 100K original fixture | 100000 / 100025 / 25 | `ef3020f84208ba9a6506902a4f3af672d7bcf21269dafd18da37163c0e0375ac` |
| 300K development/screen | 303850 / 303925 / 75 | `977cf3f4852e38a2567fc454c212ccea6f8110d03aa05ee12af2f85ecfe7b280` |
| 1M generator v3 | 1014581 / 1014831 / 250 | `641b39a2864cf7e67d1b941d837dc062b3b1bbf152aa470970a549c581cfca73` |
| 3M generator v3 | 3043062 / 3043812 / 750 | `d7fc67addc58ffd24dd80905c0c9e8e4b423b24139ef0c9a95337f119ea3c854` |

1M independently expected inputs/outputs/observations: 1477927/2639262/1014581.
3M: 4434511/7927169/3043062. Counts include injected scenarios, deduplication
and quarantine rather than assuming nominal generator sizes. Truth is strictly
evaluation-only and is not uploaded or used in production scoring.

The [model decision](../experiments/model_decision_review_20261003.md) supplies
all per-dataset AP, prevalence, P@100, capacity recall, confusion counts,
Brier/ECE, cluster-bootstrap intervals, mean/worst results and limitations.
Selected synthetic comparator E4: reserved-final A/B motif AP .988365/.926867,
surge .884631/.868913, restricted discrimination .999903/.999867. Mean/worst
motif .957616/.926867; surge .876772/.868913. Final B surge AP interval
[.768564,.917474] crosses below .80. At 1% capacity, motif recall is
.3046/.3394 and surge .5838/.6782; precision is not presented without coverage.
The .90 surge stretch target is missed; all-dataset worst validation surge
.835682 also misses the .85 preferred target. No final retuning or repeated
final evaluation was done.

The user's later requirement was explicit: improve ML accuracy/AP, retain
the document's targets during manual testing and avoid any system downgrade.
This cannot honestly mean identical AP on every arbitrary upload: AP/precision
requires complete proposition-matched ground truth and changes with prevalence,
coverage and distribution. Unlabelled uploaded cases have no measured AP.
Manual testing must separately check stages/counts/evidence and, when suitable
independent labels exist, evaluate AP, P@100, capacity recall and calibration
against the registered baseline/targets. Selectively reviewed findings are
not a representative full population. Neither historical synthetic metrics
nor unknown-domain confidence values are shown as new-case accuracy. E4 remains
research-only; greater synthetic AP is not automatic real-case eligibility.
The remaining higher-target/independent-labelled-data gates are disclosed,
not bypassed by lowering thresholds, changing targets or branding a new release.

The user confirmed no independent labelled manual-test dataset is available
and allowed generating one if needed. Three generated development sizes and
two reserved labelled finals already cover this research; generating more
same-source labels cannot establish arbitrary real-upload accuracy. Their
retained truth/manifests are evaluation-only. Further independent authored
positive-family/real-data tests require a new pre-registered experiment and
fresh reserved finals, not reuse or retuning of the inspected final datasets.

Same generator, independently seeded finals plus registered missingness and
timing/endpoint-construction stress are not independent real-world validation.
The procedure refits on each dataset's own earlier reference, not transferred
frozen weights. Benign payroll exclusions do not prove positive-family
generalization. Scenario diagnostic: 45/45 illicit episodes touched, but 11/12
benign merchant controls also touched; entity pairwise precision 1.0/recall
.083 and risk sampled recall .929/AUC .961 do not justify inventing ownership
links. These limitations prevent a blanket real-world precision claim.

Production retains v2. Its numeric scoring, fusion, seed, reference horizon and
release ID remain unchanged. Synthetic research gains are not installed for
real cases. Active calibration numerical maps are unchanged; a diagnostic
file with historical `confidence-v1`/“shipped” wording is explicitly not the
active registry. Diagnostic ML Brier .187840 vs constant .204742/ECE .093288
(n306); peeling Brier .042780 vs constant .016144 and rapid redistribution
Brier .872810/ECE .879798 are poor. New diagnostic CLI outputs have a separate
identity and cannot overwrite active calibration or claim automatic promotion.
The frozen release manifest also retains historical “ECOD attribution” wording
for identity continuity; current explanations/UI correctly say descriptive
feature-tail context, not IF/fused-score attribution.

Settings' remaining static accuracy claims were corrected too: historical
generator-v2 reuse is explicit, “evaluated exactly once” is removed, and
the displayed v2 discrimination AP is .9269 separately from the rule's .9569,
not a substituted winning-rule value. The reference quantile is not labelled
a guaranteed 1% queue cap. This is an explanatory/UI correction, not a scoring
release change. The final frontend package includes this correction, verified
by three additional Settings browser assertions.

## Offline image, investigation and regression checks

Final copied bundle: `dist/tracex-offline-0.1.0-8lVo4AHs`, compressed tar about
337 MiB; installed copy `var/offline-install-20261003-release-07`, installation
39.606 seconds. API/worker
image ID `sha256:00d435870b27181987161125b19c8e1ec661075437d7c5d8e9e1708254af9b95`;
PostgreSQL ID `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`.
Base tags resolved to recorded digests during build; Dockerfile FROM tags are
not claimed permanently digest-pinned. SHA256SUMS detects corruption, not
publisher authenticity. Research XGBoost/LightGBM are not runtime dependencies.

Geo-IP/ASN editions are June 2026, with DB-IP CC-BY-4.0 and IPtoASN PDDL-1.0
attribution. Country archive SHA
`78a6009a9c18434e6b690d703431241900d6127ddcd7c26ed48852549ee0df4f`;
ASN archive SHA `3e8c478cf0774d653602c07bd91ca2d6a744652122b3ce5f8e94ca790035bc8f`.
Compiled cache hashes are verified in the image. IPv4/IPv6/private/reserved/
unknown behavior is covered; network endpoints are context, not wallet owners.

Native review project `tracex-review-final` alone is upgraded. All three
runtime networks are internal; API/worker public IPv4 and IPv6 connect tests
return errno101/ENETUNREACH with no default IPv4 route. Host networking remains
unchanged. Strict private-bridge access is verified on native Linux, not Docker
Desktop published-port access. Current URL `http://172.23.0.2:8000` can change
after recreation. Review `.env` files have mode0600, preserved fresh secrets,
workers4/budget5120/bounded, ML1, lease600 and max upload64GiB; secrets/root
`.env` are never included in reports. The example/review installer uses the
actual `TRACEX_ML_FINDINGS` key; the earlier unused example key did not disable
ML because its default was on, as actual stage outcomes demonstrate.

Image07 browser results: smoke08 42/42, unfamiliar06 70/70, missing05 31/31.
They exercise real signup/auth/upload/worker, provisional progress, queue,
raw replay, durable review, UI graph Load more, entities/embeddings/network,
analytics-pinned risk, JSON download, no external browser resources and no
runtime JS exceptions. Missing outpoints produce zero resolved spends and
incomplete lineage; missing network is degraded, not normal. Demo sources
are 10K label-blind prefixes retained after frozen selection, not a pristine
new untouched research holdout. Their SHAs are
`eeaceb6b0304e5541533be97de5f6c9399c4c25eb789f764d4cb201ec6f04b8d`
and `df944905e1a1fadf54745f63b9df3b861a9ca32880d1d24872bc6a214393084e`.
Actual optional-chat HTTP503 is tested inside the appliance; prompt tests cover
malicious text, but no installed model/grounded-answer or hallucination-prevention
guarantee is claimed. Earlier harness failures (outdated modal/combobox
assumptions, DOM-mount race and changed private address) are preserved.

Full backend: **240 passed**, one dependency deprecation warning, 117.99 s,
`var/verification/backend-final-20261003-07.xml`. Earlier original baseline
passed175; new tests cover real recovery/provenance/resource/auth failures.
Repository Ruff and `git diff --check` pass. Frontend TypeScript/Vite build
and lint exit0; existing lint warnings remain. `mypy` is unavailable/unconfigured
and NOT RUN. Additive SQLite and image07 PostgreSQL probes create new
disposable fixture databases, add old missing columns, preserve rows/defaults
and perform an empty second upgrade. No user database is reset.

## Approved cleanup and remaining evidence

Only verified disposable synthetic benchmark data was retired under the user's
explicit approval, after fingerprints, receipts, file hashes, metrics and logs
were saved. Every actual removal has an archive/deleted marker. Real case
vaults, analyst-reviewed demos, truth/manifests, reserved-final markers, reports,
profiles and logs remain. Guard refusals preserve nonterminal old benchmarks;
the old `fresh-300k-w1-20261003-review-02` checkpoint was not deleted.

Retired data include matched300K vaults, the completed1M vault, duplicate100K
references/replays, profiled300K vault, verified terminal failure vaults and
completed3M SQLite failure evidence vault. 3M source NDJSON was checksum-verified
against its immutable upload and converted to an identical hard link before
vault retirement, saving one duplicate 3,817,991,736-byte inode. The source
path/bytes remain available for the fresh PostgreSQL repeat. Generated canonical
development/final components were retired after evaluation/demos; final truth,
source manifests, reservations and once-only markers remain.

Removal is irreversible for derived databases/vaults without regeneration;
results and hashes cannot replace full byte-level evidence. Logical removed
bytes can exceed physical reclaimed space due to hard links, database free
pages and concurrent filesystem work. No real-case deletion or generic Docker
pruning was performed. Exact local archives are indexed in
`experiments/implementation_review_inventory_20261003.json` at final handoff.

After separate explicit approval and passing release06 installation/browser
checks, 12 obsolete review payloads from releases02–05 (source/copy `images.tar`
and compressed bundle copies) were checksum-verified and removed: 4261450508
logical bytes. Both release06 comparison and release07 final complete bundles/copies, every older manifest,
SHA256SUMS, installation report/log and `.env` remain. No Docker image or volume
was pruned. The checksum archive and deleted marker are
`experiments/runs/obsolete-review-bundle-retirement-20261003{,.deleted}.json`.
The terminal interrupted PostgreSQL 1M case was also verified and retired,
3692036188 vault bytes, with DB counts/receipt hashes archived. Vacuum after
deletion first hit PostgreSQL's default shared-memory limit; the nonparallel
`VACUUM (ANALYZE, TRUNCATE, PARALLEL 0)` retry succeeded without changing retained
cases or resetting the database.

No further deletion is needed at handoff (~25.9GB filesystem free). Both
passing PostgreSQL1M case vaults/rows and the checksum-identical1M upload source
are retained for manual inspection, as are the3M source, all truth/manifests,
reviewed/seeded demos and both final/comparison images. Approved unused old
task-image removal was not needed and was NOT performed. Rebuilding/cleaning
the user's pre-existing stack, generic image pruning and volume deletion were
not performed. The inventory is an integrity index, not blanket gate approval.

## What remains before broader release claims

The conditional1M implementation/verification handoff is finished. Remaining
acceptance limitations are not concealed as completed targets:

- Full3M with ML and the <=1800-second target: provide enough real available
  memory to admit8,267,692,976 bytes plus other services, then repeat all stages
  with the same strict harness. Raising a number or disabling ML is not a fix.
- New production ML promotion: stronger synthetic target/calibration evidence
  and independent representative/positive-family labels are missing; E4 stays
  research-only and v2 remains active. Never promise AP on unknown unlabelled
  datasets or retune over the existing reserved finals.
- Official PS source provenance and an installed offline Ollama/model's actual
  grounded-answer evaluation remain unavailable. The system's unavailable
  handling is tested, not the absent external inputs/model.
- `mypy` is unconfigured/unavailable; existing frontend lint warnings remain.
  No whole1M graph/features/deterministic canonical parity or statistically
  replicated timing speedup is asserted beyond the evidence above.

For manual testing, open the current isolated review URL, register/sign in,
create a case, upload `datasets/review_dev_1m_20261003/ingestion_rows.ndjson`
(truth is NOT uploaded), inspect stage outcomes/counts/coverage, queue/graph
continuation, raw replay, review persistence and export. Smaller retained
unfamiliar/missing fixtures are in `var/demo-inputs/review-20261003/`.
Evaluate AP only against complete proposition-matched held-out truth; selective
analyst reviews and “no errors” are not accuracy measurements. Existing final
research holds are once-only and must not be reused for new selection.
Review the diff and this report yourself before staging/committing/pushing;
no such Git action was taken by the agent.

## Exact reproduction commands

Use new run/output names; do not overwrite reports or re-evaluate inspected
final holdouts. Some archived inputs require manifest-matching regeneration.
AP cannot be measured on unlabelled files. From the repository root:

```bash
uv run --extra ml pytest -q --junitxml=var/verification/NEW-backend.xml
uv run ruff check .
git diff --check
cd frontend
npm run build
npm run lint
# Use the current private API address, not the user's older port8000 stack.
TRACEX_E2E_BASE=http://172.23.0.2:8000 TRACEX_E2E_OUTPUT=../var/browser-runs/NEW-smoke npm run e2e
TRACEX_E2E_BASE=http://172.23.0.2:8000 TRACEX_E2E_SOURCE=../var/demo-inputs/review-20261003/unfamiliar.ndjson \
  TRACEX_E2E_OUTPUT=../var/browser-runs/NEW-demo node e2e/acceptance.mjs
TRACEX_E2E_BASE=http://172.23.0.2:8000 TRACEX_E2E_SOURCE=../var/demo-inputs/review-20261003/missing-ip-prevouts.ndjson \
  TRACEX_E2E_EXPECT_MISSING=1 TRACEX_E2E_OUTPUT=../var/browser-runs/NEW-missing node e2e/acceptance.mjs
```

From the repository root, fresh exact-parity verification:

```bash
uv run --extra ml python scripts/scale_benchmark.py datasets/phase5a_100k/ingestion_rows.ndjson \
  --name NEW-100k --port 8766 --workers 2 --memory-budget-mb 2600 --execution-mode bounded \
  --expected-counts experiments/acceptance_100k_counts.json --timeout 900 --keep
uv run --extra ml python scripts/canonical_parity.py var/scale-run/NEW-100k \
  --reference-report experiments/runs/serial-sql-budget-canonical-parity-100k-20261003.json \
  --output experiments/runs/NEW-canonical-parity.json
uv run --extra ml python scripts/ml_output_parity.py var/scale-run/NEW-100k --normalize-run-identities \
  --reference-report experiments/runs/serial-sql-budget-full-ml-parity-100k-20261003.json \
  --output experiments/runs/NEW-ml-parity.json
```

Actual copied-image scale harness (replace only name for a new run):

```bash
uv run python -m scripts.appliance_acceptance --base http://172.23.0.2:8000 \
  --compose var/offline-install-20261003-release-07/docker-compose.yml \
  --project tracex-review-final --context default \
  --source datasets/review_dev_1m_20261003/ingestion_rows.ndjson \
  --expected-counts experiments/acceptance_1m_counts_20261003.json \
  --name NEW-offline-postgres-1m --min-transactions 1014581 --timeout 7200
uv run python -m scripts.appliance_acceptance --base http://172.23.0.2:8000 \
  --compose var/offline-install-20261003-release-07/docker-compose.yml \
  --project tracex-review-final --context default \
  --source datasets/review_scale_3m_20261003/ingestion_rows.ndjson \
  --expected-counts experiments/acceptance_3m_counts_20261003.json \
  --name NEW-offline-postgres-3m --min-transactions 3043062 --max-seconds 1800 --timeout 7200
```

The3M command is for a host that first passes the unchanged memory admission,
not a claim that this worker can complete it. Full published-ML parity on two
new passing retained appliance cases (copy their reports to the given paths):

```bash
docker --context default exec -i tracex-review-final-api-1 python - \
  --baseline /tmp/NEW-baseline-result.json --candidate /tmp/NEW-candidate-result.json \
  --output /tmp/NEW-ml-parity.json < scripts/appliance_ml_parity.py
```

This helper was added after image07's build and is deliberately run via stdin;
it is not retroactively claimed bundled. Deployed runtime code hashes are
verified separately by the benchmark.

Research reproduction must reserve new final directories before generating
them, freeze validation-only selection and use the registered iteration budget:

```bash
uv run --extra ml --extra research python scripts/ml_study.py --controlled \
  --dataset datasets/phase5a_100k --dataset datasets/review_dev_300k_regenerated_20261003 \
  --dataset datasets/review_dev_1m_20261003 --release-protocol experiments/release_review_protocol_20261003.json \
  --max-iterations 100 --output experiments/runs/NEW-development.json
uv run python scripts/freeze_research_selection.py --help
uv run --extra ml --extra research python scripts/ml_study.py --help
uv run python scripts/acceptance_inventory.py --output experiments/NEW-review-inventory.json
```

Existing final directories are deliberately once-only; these help commands
are not permission to burn their final holdouts again. New protocol paths/seeds
and reservation markers are required for a new experiment. No automatic
supervised production fit is enabled.
