# Grouped investigation release — 2026-10-04

Implementation base: actual local HEAD `84dd826d1581c2324c70232a44a154e34271c8b3`.
Subsequent changes from reviewed `d88af37` were inspected before editing. No
applicable AGENTS.md was found. Existing unrelated frontend styling was retained.
No LLM, commit, push, publication, external deployment or destructive cleanup.

## Outcome and explicit limits

Human review now operates on durable investigation groups, not every transaction
alert. Selected deployment: **anomaly-stack-v2**. No candidate has been promoted;
neither improved deployed merchant precision nor group P@100 has been established.
Fresh 1M acceptance is **BLOCKED**, not a timing failure or a passing benchmark.
No 1M dataset was generated/imported because the mandatory admission gate rejects
the available resources. No 3M run. Earlier 1M reports remain historical.

Review the machine-readable decision and underlying sanitized metrics in
[`experiments/grouped_review_20261004/final`](../experiments/grouped_review_20261004/final/).
The parent package is an earlier task-created report generation and is retained,
not overwritten as though it measured the final build.
Full private logs, small cases and fitted diagnostic weights remain under `var/`.

## Grouping and decision contract

Procedure `observed-episode-groups-v1`, policy `group-family-round-robin-v1`,
procedure SHA-256 `b429e8ee965ab3fe7ac78edd719cd565ee06c31fe38da4da9890e2e0780a0769`.
The hash includes implementation bytes and policy, not merely a version string.

- Scope: case, immutable evidence snapshot, procedure and immutable input digest.
- Episodes: fixed UTC days `[00:00, next 00:00]`; a window ending exactly at the
  boundary is eligible for its starting day. Crossing/long/invalid windows stay
  singleton. Maximum 256 members and 256 frozen representative anchors.
- Peeling: same detector/version/day and directly overlapping **verified
  canonical SPENT_BY transaction endpoints** with the representative backbone.
  The existing peeling detector already consolidates a chain's overlapping tails.
- Other repetition: same focal reference, detector/version/day and an exact
  immutable source-record overlap with the representative. Fan-in and fan-out
  remain separate propositions/families; a common focal address alone is not enough.
- Adding a member never expands anchors. IP/ASN, inferred wallet membership,
  truth/scenario IDs, analyst verdicts and shared-address hubs cannot connect groups.
  Conservative fixed backbones can fragment long episodes; no arbitrary merging
  is used to achieve a small group count. No finding is suppressed.
- Representative is first in stable immutable time/family/focal/finding-ID order,
  not the largest duplicate group. Queue ranking uses its raw score only inside
  the same detector/version family. Bands: escalated, needs-data, open; family
  round-robin with stable ID ties. No incompatible score addition, member-count
  boost or claim of calibrated group probability.
- Default capacity 100; validated 0–10,000. Exact finding/group/unresolved/queued/
  backlog counts are exposed. Backlog/all/history pages retain access. Triaged,
  confirmed and dismissed free capacity; escalated/needs-data/open remain unresolved.
- Group proposition: investigate the observed directly connected pattern episode,
  **not** wrongdoing, ownership, origin or automatic confirmation of member claims.
  Individual finding histories/confidence pins are unchanged. Mixed member
  decisions are displayed. Reviews are append-only, reasoned and version-checked.
- Membership and subjects are normalized SQL rows; evidence stays on the original
  findings, not a duplicated group blob. Members/subjects/reviews/exports are
  bounded pages (maximum 200). Transaction counts enumerate canonical detector
  participation and verified path endpoints; entity counts enumerate focal
  references, not proven wallet owners. Legacy/capped paths have explicit limits.
- Representative supporting/opposing evidence is separately labelled, replayable
  under case authorization; every member's original evidence remains accessible.
  Benign alternatives are not observed contradictions. Reviewed source references
  are receipt/hash checked, never invented. No universal innocence/criminality claim.
- Snapshot lock plus transactional publication makes retries deterministic and
  idempotent. A failed materialization leaves no partial active generation. Changed
  immutable inputs produce replacement generations with overlap links; prior
  membership and group decisions are preserved, not copied or silently broadened.
  A new import's distinct snapshot is independent and leaves previous snapshots
  accessible; it does not rewrite prior reviewed groups.

`investigation_grouping` is required before final successful analysis. Durable
failure/retry state is visible. Additive table creation preserves existing data.
For pre-grouping jobs, use authorized analysis retry; until materialized, missing
groups are not a successfully analyzed zero backlog. Do not mass-recompute a
large existing case without admission. A separately observed review group may
remain singleton; a capacity is not a guarantee of at most 100 meaningful groups.

## Actual verification

Audited final small suite: **198 passed**, 32.58 s; full XML retained as
`var/grouped-benchmark-20261004-01/release-tests.xml`. Tests cover grouped
peeling/repetition, episode separation, hub non-merging, capacities 0/1/100,
authorization including exports/decisions, immutable replacement/history,
failure recovery, stage degradation, receipt parity, immutable calibration pins,
frozen artifact/queue/protocol checks, time cutoffs, portable telemetry and strict
failure reports. Small serial/parallel and bounded/memory parity passed.

Exact audited test discovery scope (Linux host; the timeout is diagnostic, not a
benchmark target). Every listed module was inspected for heavy fixture/study
launches before execution; do not substitute blind full-suite discovery:

```sh
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TRACEX_MEMORY_BUDGET_MB=2600 timeout 300 .venv/bin/pytest -q \
  tests/unit/test_investigations.py tests/unit/test_analysis_acceptance.py \
  tests/unit/test_ml_pipeline_integration.py tests/unit/test_recipient_history_release.py \
  tests/unit/test_promotion_registry.py tests/unit/test_integrated_improvements.py \
  tests/unit/test_integrated_boundaries.py tests/unit/test_group_quality.py \
  tests/unit/test_quality_matrix_reporting.py tests/unit/test_quality_wiring.py \
  tests/unit/test_macbook_orchestration.py tests/unit/test_appliance_acceptance.py \
  tests/unit/test_auth_and_case_reads.py tests/unit/test_phase_six_crash_retry.py \
  tests/unit/test_research_contract.py tests/unit/test_pattern_grouping_and_resources.py
```

Frontend commands actually used: `node_modules/.bin/tsc -b --pretty false`,
`npm run build`, `npm run lint` and `node --test e2e/graph-window.test.mjs`, from
`frontend/`. There is no `npm run typecheck` alias; that attempted alias failed,
then the actual compiler/build succeeded. Full test XML and the exported frontend
summary distinguish warnings, unsupported aliases and successful checks.

An initial two-worker test correctly rejected an implicit 2,543 MiB budget;
rerunning with **admitted** 2,600 MiB passed. Admission was not forced/disabled.
Earlier failing XMLs are retained. Test-only fabricated promotion metadata is
never exported as model quality evidence.

Actual copied native Linux PostgreSQL/API/worker acceptance (128 canonical TX,
3 inputs, 304 outputs, 128 network observations, zero quarantine) passed all
stages. Final build run `20261004-230535-a2c9eecc` took **4.235550 s** fresh upload
through authenticated group/member retrieval, including 0.048423 s upload,
0.347690 s grouping and 0.073393 s final group/member retrieval. All stages and
count/hash checks passed. Sampled worker tree RSS peaked at 314,724,352 bytes;
API at 173,322,240 bytes. PostgreSQL sampled RSS is unsupported (null), not zero;
cgroup lifetime peaks are separately recorded and not run-scoped measurements.
Earlier passing runs took 4.215544 s, 4.190644 s and 6.285858 s; these are all
small functional results, not throughput estimates. First strict attempt failed
on missing API zero-quarantine count; a stable known-zero count contract fixed it.
Subsequent code-identity mismatches correctly rejected stale images before upload.

Small worker screening: one worker **4.130974 s**, two **8.232871 s**; conservative
choice one. Do not extrapolate these small times to 1M. The fixture produced
42 findings and 42 groups, not demonstrated alert-volume reduction.
Final browser **65 checks passed** (`var/browser-runs/grouped-review-20261004-04`):
group counts/0/1 capacities, persisted independent reviews,
bounded member/source/graph access, signed continuation, exports, embeddings,
cross-case group read/export/review denial, no language-model endpoint or external
browser resources. Actual `tsc -b`, frontend build and graph-window tests passed; lint
has existing React warnings, not a warning-free certification.

Full-suite discovery was audited rather than blindly run. Existing 100K fixture
generators, heavy phase5 studies, large-scale parity, 1M/3M and previously consumed
reserved holdouts were intentionally not run. Tiny tests do not establish scale.

## Bounded ML comparison and final decision

All four CPU backends were installed from locked optional extras. Training 5,000,
calibration 4,000 and validation 4,000 canonical transactions used distinct fresh
seeds. Only permitted labelled rows entered fitting; final funding/unknown rows
were not invented negatives. Fixed bounded tuning, causal 38-column structure/
recipient feature contract for all four candidates; v2 retains its documented
32-column/retrospective procedure. No GPU path was assumed.

| Validation procedure | Motif AP | Surge AP |
| --- | ---: | ---: |
| v2 (label-free per-population refit) | 0.695329 | 0.675866 |
| HistGradientBoosting | 0.981414 | 0.348602 |
| XGBoost CPU | 0.982221 | 0.378057 |
| LightGBM | 0.997927 | 0.316850 |
| Hybrid | 0.948014 | 0.572482 |

378 validation rows were labelled (of 4,000 eligible); motif prevalence 0.177249,
surge 0.044974. Thus even theoretical subset P@100 upper bounds were 0.67/0.17.
Model training/validation took 0.431/0.308/0.309/0.667 s respectively, excluding
shared feature/baseline preparation. Recorded cumulative process peak 693,362,688
bytes includes baseline and prior models, not isolated per-model peaks.

Before opening final labels, `selection-decision.json` chose v2 for deployment
and hybrid **only** for diagnostic transfer (largest minimum validation task AP
among candidates). Protocol 03 registered future finals before selection;
sources/truth hashes and fitted weights were frozen before one-use evaluation.
Earlier protocols 01/02 were superseded before evaluating their finals because
the grouping implementation changed; none of those finals was reused for selection.

| Frozen diagnostic final | Hybrid motif AP | Hybrid surge AP | v2 motif AP | v2 surge AP |
| --- | ---: | ---: | ---: | ---: |
| Fresh base | 0.961725 | 0.619101 | 0.761694 | 0.640035 |
| Timing/value shift | 0.960171 | 0.514135 | 0.768629 | 0.636899 |
| Missing network | 0.961725 | 0.619101 | 0.761694 | 0.640035 |

Each final has 499 labelled of 5,000 eligible rows, motif prevalence 0.268537,
surge 0.104208. Worst hybrid motif/surge AP: **0.960171 / 0.514135**; worst
surge AP delta against v2 **−0.122764**. Base motif AP 95% cluster-bootstrap
interval [0.921287, 0.991482], surge [0.384777, 0.800039]; shifted surge
[0.269526, 0.740481]. Full per-task/per-family precision/recall, confusion,
calibration and uncertainty are in the underlying reports, not reduced to AP.
Subset motif P@100 0.96–0.97 and surge 0.41–0.42 are **not** product queue/group
precision. Actual retained queues contain only 50 candidate findings; many
canonical rows and merchant controls lack applicable labels.

Matrix status **INCOMPLETE**. Product queue/group P@100: **NOT EVALUABLE**.
Representative merchant/payroll/exchange/batching false-positive reductions are
not demonstrated by this study. Diagnostic base motif false positives at .5:
5; shifted: 6. Surge .5 classification has 6/10 false positives; these are
synthetic task-frequency thresholds, not calibrated real-domain wrongdoing.
Shifted/coverage variants are correlated copies, not independent seeds; copied
surge truth may become inapplicable after timing shifts. Address/entity-disjoint
and unseen-family generalization remain unverified, not passed.

Hybrid payload SHA `5b6a0313d77a94abfe695c750ca03f9ab4c8dfd9c811afc008a6f34907c8ca96`;
manifest `45d41079ada5b4ca2f7e586ce395d49d48e949fe10420daec4f05463cb543724`;
feature `4e8f13ee68f0bb31ff67d51e96448d6015356d79891302ad944689d35fb4f743`;
protocol `ea4febf32f3e04a4d1748fecf9b7c6fa3b34cb14e81d3044da2557123213b293`.
Weights are private local artifacts, not Git payloads. Later promotion-only
hardening does not rewrite these measurements: their old unpinned capacity
provenance is ineligible under the new gate, not retroactively fixed. Do not
re-evaluate their consumed finals. New studies must pin capacity/fraction in
fitted provenance and use genuinely fresh final registrations.

No candidate meets the joint objectives/no-regression/eligibility evidence.
Keeping v2 preserves the existing procedure; it does not certify high AP or
merchant precision. Real/unknown cases remain unsupervised. Synthetic artifacts
are not automatically activated; no demo candidate is chosen for deployment.

## Resource blocker and readiness

Actual host: Arch Linux 7.2.4-arch1-2, x86_64 Intel i5-13420H, 12 logical/8
physical cores, 16,366,317,568 bytes system RAM; available RAM fluctuated around
4.3–5.3 GB. Native Docker shares host RAM; Docker Desktop's separate VM reports
8,572,964,864 bytes. No WSL layer on this native Linux host. NVIDIA driver was
unavailable; the GPU was not used. The actual worker budget was 3,500 MiB with
one worker, native thread environment caps 1. Actual container limits, DB/library
versions and code/image identities are saved in preflight/acceptance reports.

All source/evidence/PG/scratch locations share `/dev/nvme0n1p5`. At the first
admission check free disk was 20,748,656,640 bytes against a **zero-source/input/
output lower bound** of 33,663,676,416 bytes. Final free space was 10,479,861,760
bytes (deficit 23,183,814,656), including retained task-created images and reports.
The initial check already rejected 1M before those later images were built. No existing files,
images, volumes or evidence were deleted. This is the **predeclared conservative
supported gate**, not a measured minimum storage requirement. Every worker
configuration has this same disk floor; changing workers cannot admit 1M here.
The final actual container preflight also rejected the screened 3,500 MiB budget:
estimated joint ML memory 3,808,521,728 bytes versus budget 3,670,016,000 bytes,
with approximately 5.3 GB available host RAM at that instant. A larger memory
budget might be admissible, but **every configuration still fails disk admission**;
do not interpret this as proof that all memory configurations fail. Estimated
1M/1.5M-input/2.7M-output disk requirement was 46,200,547,328 bytes, also rejected
on the actual worker, PostgreSQL and host filesystems. These are metadata-only
estimates, not actual 1M processing or memory peaks.

Application/group workflow: small verification passed. Fresh 1M and <1800 s:
BLOCKED/NOT RUN. Deployment validation blockers: adequate admitted disk/RAM for
the new 1M run; independently applicable full-population/group labels if promoting
a candidate or claiming quality targets. GeoIP licences/checksums were available,
not fabricated; no missing credential blocker. Do not state the requested PPT
1M success sentence. Offline operation and source integrity are retained; a
published loopback test port is not native-Linux strict network-isolation proof.

### Deployment-readiness checklist

- [x] Additive durable groups, bounded authenticated membership/evidence, independent
  append-only decisions, stable queue/backlog and immutable replacement history.
- [x] Required resumable grouping stage; injected failure/retry tests and actual
  PostgreSQL worker completion with matched application source hashes.
- [x] Explicit v2 scorer/domain/fallback policy; fail-closed frozen candidate
  provenance, full-population/queue/group applicability and promotion checks.
- [x] Offline resources verified, no LLM or cloud runtime request; small browser
  behavior, source replay and authorization verified.
- [x] Sanitized compact results, report inventory and exact reproduction commands.
- [ ] Fresh 1M all-stage acceptance and <1800 s target: blocked by supported SSD
  admission; the screened memory budget also needs a safely admitted setting.
- [ ] Group P@100 or merchant-precision improvement claim: unavailable applicable
  group/full-population labels and rejected candidate quality gates. This does
  not block the explicit v2 choice, but it blocks those quality/promotion claims.
- [ ] Large/contended multi-worker operation: not verified. Keep one worker.
  The existing lease is renewed at ingestion commits, not continuously during
  long final stages (`pipeline.py` / `claim_next_job`); do not deploy competing
  claimers as though long-stage lease protection were established.

These checks qualify local offline use, not public-network production security.
Existing rate-limit/TLS/session/vault-encryption gaps in README §16.3 remain out
of scope; external exposure was neither requested nor performed.

## Requirement-to-code/test/evidence mapping

| Requirement | Code | Small verification / evidence |
| --- | --- | --- |
| Durable bounded groups and replacements | `app/engine/investigations.py`, `app/models.py` | `test_investigations.py` |
| Capacity/backlog/independent review/auth/export | `app/api/group_routes.py`, grouped UI pages | grouping tests and actual browser |
| Required resumable grouping | `app/jobs/analysis.py`, ingestion pipeline | injected failure/retry and PostgreSQL stage outcomes |
| Canonical participation without large group JSON | deterministic/bounded finding persistence | bounded/memory/parallel parity |
| Exact promotion registry/capacity/frozen payload | promotion, candidate, lifecycle modules | registry/recipient/wiring tests; candidate BLOCKED decision |
| Honest missing metrics | `scripts/quality_matrix.py` | reporting regression tests and INCOMPLETE matrix |
| Safe portable admission/acceptance | telemetry, runtime probe, orchestration | mocked large counts, actual small worker/container checks |
| Compact underlying results | export and grouped release report scripts | compact JSON + checksum inventory, no weights/data/tokens |

Official PS wording/source provenance remains unresolved in `docs/requirements.md`.
This is a project requirement mapping, not complete official PS certification.
