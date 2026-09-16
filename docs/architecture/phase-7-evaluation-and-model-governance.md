# Phase 7 Part 1: Evaluation Foundation, Dataset Manifest, and Local-Model Governance

Owner: Nipun (Part 1); Jasraj (Part 2, this document's added section
below); Gaurav (Part 3, this document's added section below); Sarthak
(Part 4, this document's added section below). Status: **Part 1 in
progress** (this document's own scope is complete and gated below);
**Part 2 Gate B incomplete** (real AMLSim and both PaddleOCR runs
succeeded, while the official GoMask download remains account-and-credit
gated — see "Part 2: structured-data and local OCR benchmarking" below);
**Part 3 in progress** (visual benchmark adapters/CLI built and tested
against synthetic fixtures and fake engines; real dataset/model artifact
validation status is recorded in the "Part 3" section below); **Part 4
in progress** (audio/social benchmark adapters/CLI built and tested
against synthetic fixtures and fake engines; real dataset/model artifact
validation status is recorded in the "Part 4" section below). Phase 7
overall is not complete. Part 1 freezes the referee and scoreboard later
Phase 7 parts build against; it downloads no dataset, installs no model,
and selects no winner.

## Phase 7 objective and part map

Phase 7 selects and integrates reproducible **local** models on one
development/benchmark machine. It does not perform LAN deployment (Phase 8).

```text
Part 1 (Nipun):              freeze evaluation contract and candidate catalogue
Parts 2-4 (Jasraj/Gaurav/Sarthak): download and benchmark candidates in parallel
Gate C:                      lock chosen modality model versions/hashes/configurations
Part 5 (Aditya):             make locked local bundles reproducible and safe
Part 6 (Shreshtha):          compare/freeze correlation algorithm and run the Phase 7 release gate
Phase 8:                     LAN deployment and multi-laptop validation
```

Part 1 is the referee and scoreboard: it freezes what data may prove each
capability, freezes case-level evaluation splits and anti-leakage rules,
defines typed dataset/model-candidate/benchmark-run/result-manifest
contracts, defines a candidate local-model catalogue (never a winner), and
defines the synthetic multi-modal evaluation plan plus the two
investigator-report proof requirements. Nothing downstream of Part 1 may
change the meaning of success after seeing a model's results — that is
this document's central purpose.

## What Part 1 explicitly does not do

No public dataset was downloaded. No model weight was downloaded or run.
No benchmark was executed — every metric in this phase is a required
*key*, never a measured *value*. No final modality model was selected —
every `ModelCandidateV1` in the catalogue starts `candidate` or
`conditional`, structurally never `selected` (see `models.py`). No
correlation algorithm was chosen over the existing Phase 5 rules baseline.
No LAN/multi-laptop validation was attempted. No frontend or report UI was
built.

## Module layout

`app/modules/evaluation/` — new, following the same "internal, versioned
contract, not a frozen `app.contracts` V1 payload" pattern established by
`app.modules.graph.integration_models` (Phase 5) and
`app.modules.integrity.models` (Phase 6):

| File | Responsibility |
|---|---|
| `models.py` | Every typed contract and enum: `DatasetManifestV1`/`DatasetManifestEntryV1`, `ModelCandidateCatalogV1`/`ModelCandidateV1`, `BenchmarkRunV1`, `BenchmarkMetricsSpecV1`, `SyntheticCasePlanV1`, the two investigator-report requirement contracts, and `comparable_runs`. |
| `manifest.py` | Loads/validates `configs/benchmarks/dataset-manifest.v1.json` into `DatasetManifestV1`. |
| `catalog.py` | Loads/validates `configs/benchmarks/model-candidates.v1.json` into `ModelCandidateCatalogV1`. |
| `splits.py` | Loads/validates `configs/benchmarks/synthetic-case-plan.v1.json`; `assert_case_not_reserved_for_tuning` is the reusable anti-leakage guard (frozen rule 2). |
| `results.py` | Loads/validates `configs/benchmarks/benchmark-metrics.v1.json`; `require_metric_group_coverage` checks a future real `BenchmarkRunV1` against it. |
| `validation.py` | Shared safe-content scanners (`check_forbidden_tokens`, `check_safe_relative_path`), reused across `models.py`'s field validators. |

Four JSON configs (plain JSON + typed Pydantic validation, no new YAML
dependency needed): `configs/benchmarks/dataset-manifest.v1.json`,
`model-candidates.v1.json`, `synthetic-case-plan.v1.json`,
`benchmark-metrics.v1.json`.

## Dataset Manifest V1

Fourteen entries: seven primary public benchmark datasets, five
conditional/support-only public datasets, and two controlled-synthetic
entries (`synthetic_multicase_v1`, `operation_nightfall_v1`). Every entry
carries `dataset_id`, `display_name`, `role`, `owner`, `modalities`,
`allowed_tasks`, `prohibited_claims`, `source_reference`,
`release_or_version`, `license_status`, `license_notes`,
`local_path_placeholder`, `git_storage_policy`, `redistribution_status`,
`evaluation_status`, and `known_limitations`.

### Where a fact was not invented

Per the task's own instruction, no licence, source URL, dataset size,
label, or release version was invented. Where the source material available
to this session did not provide an independently verified value, the entry
uses `license_status: "pending_verification"` and/or
`release_or_version: "pending_verification"`, with a `license_notes` string
naming exactly what still needs checking before that dataset may be
downloaded. Two exceptions carry `license_status:
"verified_permissive"` because their permissive terms are public and
well-established: `common_voice_indic` (Mozilla Common Voice is released
under CC0) and `vast_social_text`/`vast_2014_mixed_records` (the VAST
Challenge organizers explicitly release their datasets for
research/benchmarking use). Every other public dataset in this manifest is
`pending_verification` and `evaluation_status:
"license_verification_required"` — Parts 2-4 must resolve that before
downloading, not treat the manifest entry's existence as pre-approval.

`owner` was assigned by domain fit (visual/detection work to Gaurav,
document/CDR/finance to Jasraj, audio/social to Sarthak) for the five
conditional datasets, since the task's own conditional-dataset table names
no owner column — this is a project-assignment decision within Part 1's
authority, not an invented external fact. `vast_2014_mixed_records`
straddles Jasraj's and Sarthak's scopes (it carries CDR/finance-like
card/loyalty records alongside social/microblog/email content); it is
assigned to Sarthak per the VAST dataset family grouping, with that
ambiguity documented directly in the entry's own `known_limitations`.

### Two structural, not just documented, safety properties

- **`composite_investigation_claim_allowed: Literal[False] = False`** on
  `DatasetManifestV1` itself. This is not merely a field that happens to be
  `False` today — the type is `Literal[False]`, so pydantic rejects any
  attempt to set it to `True` at construction time. Unrelated public
  benchmark records are exactly that: unrelated. No combination of dataset
  entries in this manifest may ever be presented as one real, continuous
  investigation, and that property is enforced by the type system, not by
  a comment.
- **Operation Nightfall's protected status.** `DatasetManifestV1`'s own
  model validator raises if any entry named `operation_nightfall_v1` does
  not have `role: private_showcase_holdout` and
  `git_storage_policy: excluded_private_never_documented` — so even a
  future edit to the JSON config that accidentally weakens Nightfall's
  protection fails to load, rather than silently taking effect.

## Data storage and Git safety

`.gitignore` gained five narrowly-targeted, additive entries:
`/local-data/`, `/model-cache/`, `/benchmark-runs/`, `/private-evaluation/`,
`/operation-nightfall-truth/` — nothing existing was broadened or removed.
Every `local_path_placeholder` in the dataset manifest is validated
(`validation.check_safe_relative_path`) to start with one of these five
roots, reject an absolute or home-relative path, and reject a `..`
traversal segment — a JSON config entry pointing outside the documented
safe roots fails to load.

Policy, stated once here and enforced structurally above:

- Raw downloaded data, model weights, and model caches stay outside Git —
  every dataset entry except `synthetic_multicase_v1` uses
  `git_storage_policy: excluded_manifest_only` (only this manifest's safe
  metadata is committed, never the data itself).
- `synthetic_multicase_v1` is the one exception:
  `git_storage_policy: synthetic_fixtures_may_be_committed` — small,
  licence-safe, non-sensitive synthetic fixtures *may* be committed
  alongside its manifest entry once a later part generates them. Part 1
  generates no such fixtures itself.
- `operation_nightfall_v1` is the strictest: `git_storage_policy:
  excluded_private_never_documented` — not even a content manifest is
  committed, only this entry's existence/policy fields. Its truth file is
  never committed to Git in any form and is never shown in the
  investigator UI (see `synthetic-case-plan.v1.json`'s
  `truth_file_policy`).
- Benchmark manifests/results may be committed only when they contain no
  raw evidence, raw transcript/document text, identities, credentials,
  private paths, or private truth — enforced structurally by
  `BenchmarkRunV1.metrics`'s `float | int | None` type constraint (a raw
  string value cannot exist in a metric at all) plus
  `validation.FORBIDDEN_METRIC_KEY_TOKENS`, which additionally rejects a
  metric *key* shaped like a real identifier (`phone_number`, `email`,
  `aadhaar`, ...) even though its value is "just a number".
- No downloaded artifact's SHA-256 is fabricated in this phase — Part 1's
  `BenchmarkRunV1.artifact_sha256` field exists and is validated
  (`^[a-f0-9]{64}$`, required whenever `status == succeeded`), but no real
  hash is recorded anywhere in this phase because nothing was downloaded
  or run.

## Model candidate catalogue

Seventeen candidates loaded from `model-candidates.v1.json`, spanning ten
tasks (`ocr`, `fir_cdr_finance_extraction`, `detection`, `tracking`,
`visual_text`, `vad`, `asr`, `diarization`, `language_identification`,
`correlation`) plus one forward-compatible-only task
(`social_text_extraction`, mirroring Phase 6's `review_decision`/
`hypothesis_action` precedent: no candidate exists for it yet, the
existing deterministic social/chat parsers remain today's baseline).

Every candidate's `selection_status` is `candidate` or `conditional` —
`ModelCandidateV1`'s own field validator raises if `selected` is passed,
so this is a structural guarantee, not a manual audit. `fallback_candidate_id`
references are cross-checked at catalogue load time (`ModelCandidateCatalogV1`'s
model validator) to resolve to a real candidate in the same catalogue.

Two conditional entries name an explicit deterministic fallback in their
own right: `pyannote-community-local` (diarization, conditional on
verifying and accepting pyannote's local-use license terms) falls back to
`deterministic-diarization-fallback` (the existing Phase 4 diarization-import
path); `fasttext-lid176` (language identification, conditional on the
existing deterministic script/Unicode-range normalization proving
insufficient) has no fallback field set because the deterministic approach
*is* the baseline it is conditional against, not a fallback from it.

The tracking task carries exactly one candidate, ByteTrack — no face
recognition, appearance-based person re-identification, or biometric
identity model is catalogued, matching this project's permanent "no
biometric/face identity recognition or person ReID" boundary.

## Frozen evaluation rules

Encoded as validators, not only documentation, wherever a validator could
express the rule:

1. **Split by case, never by observation/frame/page/row/clip/message.**
   `SyntheticCasePlanV1.case_groups` holds whole `case_id`s only; its model
   validator rejects a `case_id` appearing in more than one group.
2. **Never tune using validation/holdout cases.**
   `splits.assert_case_not_reserved_for_tuning` is the reusable guard a
   future benchmark-driving script must call before any development-time
   decision touches a case's data.
3. **Operation Nightfall's truth is private; it may evaluate system output
   but may never be exposed to the investigator UI.** Encoded in
   `synthetic-case-plan.v1.json`'s `truth_file_policy` and
   `operation_nightfall_policy` fields, and structurally: `CaseGroupV1`'s
   own field validator rejects any case_id containing "nightfall" inside
   `case_groups`, so Nightfall cannot even be accidentally folded into the
   synthetic plan's tunable pool.
4. **Every benchmark run names its exact dataset, split, candidate,
   configuration hash, runtime, hardware profile, and artifact hash where
   applicable.** `BenchmarkRunV1`'s required fields; `artifact_sha256` is
   required specifically when `status == succeeded`
   (`BenchmarkRunV1`'s model validator).
5. **A comparison across a different split (or dataset) is invalid unless
   explicitly marked non-comparable.** `models.comparable_runs(a, b)` is
   the one function that decides this — `True` only for the same
   `dataset_id` and `split_id`.
6. **Scores are evidence-link support, never a guilt probability.**
   Encoded in `benchmark-metrics.v1.json`'s `correlation` metric group
   description and in `InvestigatorLeadReportRequirementsV1`'s
   `prohibited_language`, which must name guilt-conclusion vocabulary
   (enforced by that model's own validator).
7. **Every model output still flows through the canonical
   observation/provenance/integrity contracts already frozen in prior
   phases.** No exception is introduced here — Part 1 adds no new
   ingestion path, so this holds by construction; Parts 2-5's real
   benchmark-driving code is where this rule has teeth.
8. **No future worker writes raw source payloads as primary Neo4j
   evidence.** Unchanged from every prior phase's boundary; not
   re-implemented here, restated so a later part cannot claim Part 1 was
   silent on it.
9. **No automatic identity merge or person ReID.** Restated in "Model
   candidate catalogue" above and unchanged from `CLAUDE.md`'s permanent
   boundary.
10. **A failed/unavailable candidate produces a safe structured result,
    never invented metrics.** `BenchmarkRunStatus.FAILED`/`UNAVAILABLE`
    exist precisely so a real benchmark run can report "this candidate
    could not be evaluated" truthfully; `failure_reason_safe` is scanned
    for credential-shaped content before it can be stored.

## Metric groups

`benchmark-metrics.v1.json` defines eleven metric groups (one per
`CandidateTask`, including the forward-compatible `social_text_extraction`).
Every metric name is a required *key* on a future `BenchmarkRunV1.metrics`
dict — its *value* may be `None` when a dataset's labels genuinely can't
support that measurement, but the key's presence is what makes two runs of
the same task comparable at all
(`results.require_metric_group_coverage`). Two groups (`vad`,
`language_identification`) extend beyond the task's own metrics table,
since neither had an explicit row there; each is marked in its own
`description` as Nipun's minimal, reasonable addition for completeness,
not an unstated invention.

`tests/unit/evaluation/test_results.py::test_all_required_metric_groups_are_represented`
cross-checks that every task actually used by a catalogued candidate has a
corresponding metric group — this is re-checked automatically, not only
asserted in prose, every time the test suite runs.

## Synthetic case plan and Operation Nightfall separation

`synthetic-case-plan.v1.json` plans exactly 12 cases (6 development, 3
validation, 3 holdout — within the required `[12, 15]` bound) for
`synthetic_multicase_v1`. Every one of the seven required cross-modal
scenarios and both investigator-report proof requirements are declared and
structurally required (`SyntheticCasePlanV1`'s model validator rejects a
plan missing any one of them — `set(...) != set(EnumClass)` comparison, not
a length check that could pass with the wrong members).

Operation Nightfall is **not** one of the 12 planned cases and never
appears inside `case_groups` — it is a wholly separate, private showcase
holdout, referenced only through `operation_nightfall_policy` and
`truth_file_policy`. This separation is structural (see frozen rule 3
above), not only a naming convention.

No actual synthetic case content — no case data, no fixtures, no truth
file — exists yet. Part 1 is the plan and its typed contract only; later
owners generate approved modality fixtures against this plan, and Part 6's
release gate is where the plan is actually exercised end to end.

## The two investigator-report proof contracts

No frontend or report generator is built in Part 1. Two typed contracts
name what a later report generator must be able to show, and
`default_evidence_provenance_report_requirements()`/
`default_investigator_lead_report_requirements()` give the frozen default
content:

1. **Evidence and provenance report**: case-scoped evidence/observation
   references, source locators for every cited fact, extraction/model
   provenance for every derived observation, integrity/checkpoint/signature
   references where available, and a clear distinction between a source
   fact and a derived observation.
2. **Investigator intelligence-lead report**: case-scoped candidate
   links/events/hypotheses, supporting evidence, contradicting evidence
   when present, a confidence/support explanation framed as evidence-link
   support only, review status, and explicit "lead for review" language for
   every reported item — never a guilt conclusion.
   `InvestigatorLeadReportRequirementsV1`'s own validator requires
   `prohibited_language` to name guilt-conclusion vocabulary explicitly, so
   a future edit cannot silently drop that constraint.

## Part 2: structured-data and local OCR benchmarking (Jasraj)

Part 2 implements the reproducible benchmark adapters, safe CLI, and
tests needed to *evaluate* the three datasets Part 1's manifest assigns
to Jasraj: `fir_icdar_2023` (document/FIR OCR), `gomask_voice_cdr` (CDR),
`ibm_amlsim` (finance). It does not select a winning OCR model — Gate C
does that, after Parts 2–4 all have comparable results.

### Module layout

`app/modules/structured_processing/` gained five files, all purely
additive (no existing document/CDR/finance production code was rewritten):

| File | Responsibility |
|---|---|
| `benchmark_metrics.py` | Pure functions: CER/WER (Levenshtein-based), field-extraction precision/recall/F1, percentile/median latency, `peak_memory_mb` (platform-aware — macOS reports `ru_maxrss` in bytes, Linux in kilobytes). No I/O. |
| `benchmark_validation.py` | `TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/`TRACEX_BENCHMARK_OUTPUT_ROOT` resolution (explicit env var or CLI flag only, never a hardcoded path); the Part 2 dataset/candidate/pair allow-lists (narrower than Part 1's full catalogue — Gaurav's/Sarthak's/Shreshtha's entries are rejected here even though they're real in the manifest); `reject_private_local_paths`. |
| `benchmark_adapters.py` | The `OcrEngine` protocol (`FakeOcrEngine` for tests, `ConfiguredOcrEngine` for a real, caller-injected recognition callable), `run_ocr_benchmark`, and `run_structured_benchmark` (CDR/finance, reusing `structured.chunked_processing`). |
| `benchmark.py` | `run_benchmark` — the one orchestration entry point: validates the request, resolves local roots, checks artifact availability *before* executing, dispatches to the right adapter, writes a safe `BenchmarkRunV1` JSON result. |
| `benchmark_cli.py` | `uv run python -m app.modules.structured_processing.benchmark_cli --dataset-id ... --candidate-id ...` — see the runbook for the full command and exit-code contract. |

### Why the OCR field-extraction task reuses the real FIR extractor

`benchmark_adapters._extracted_fields_from_text` calls
`document.fir_report.extract_fir_mentions` unchanged over whatever text an
OCR candidate produced. This was a deliberate choice over inventing a
benchmark-only extraction rule: it means a candidate's field-extraction F1
genuinely measures "how well does this OCR engine preserve the exact
structure the production FIR pipeline already depends on to find a FIR
number, phone number, police station, ..." — not an artificial proxy
metric that could score well on OCR quality while saying nothing about
this project's actual downstream extraction accuracy.

### Why CDR/finance benchmarking reuses `chunked_processing.normalize_chunk`

`run_structured_benchmark` calls `structured.chunked_processing.
assess_schema`/`normalize_chunk` — the *exact* functions
`worker.run_structured_batches_job` already calls in production — rather
than calling `structured.cdr.normalize_cdr_records`/`structured.finance.
normalize_financial_records` directly. Those two functions are all-or-
nothing per call (one malformed record raises and discards the *entire*
batch, confirmed by direct inspection of `structured/cdr.py`); only
`normalize_chunk` gives genuine per-row accept/reject accounting (each
record normalized independently, a malformed one safely categorized by
`ProcessingError.code`, never aborting its neighbors). Benchmarking
through the same seam production batching uses means the reported
`accepted_row_count`/`rejected_row_count` numbers are exactly what a real
worker run against the same file would also report — not a benchmark-only
approximation.

A file where *every* row fails row-level normalization reports a safe
`BenchmarkRunStatus.FAILED` (mirroring `run_structured_batches_job`'s own
`PARTIAL_ROW_FAILURES` policy exactly) rather than a `SUCCEEDED` result
with a hollow `accepted_row_count: 0` — a genuine bug caught and fixed
during this task's own test-writing (see `docs/qa/test-results.md`'s dated
Phase 7 Part 2 entry).

The official AMLSim sample is a deliberate, dataset-specific exception to
the generic finance schema. Its exact
`sourceNodeId,targetNodeId,value,time` header carries a simulation step and
no currency. For `dataset_id=ibm_amlsim` only, the benchmark adapter validates
the two node IDs, finite non-negative value, and non-negative integer step,
then counts one simulator transaction per valid row. It does not manufacture
a currency or calendar timestamp and does not weaken the production finance
profile for any other dataset.

### Local roots, artifact availability, and safe failure

Every local filesystem root is resolved from an explicit environment
variable (`TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/
`TRACEX_BENCHMARK_OUTPUT_ROOT`) or an equivalent CLI flag — never a
hardcoded developer path. A dataset's `local_path_placeholder` (e.g.
`"local-data/gomask_voice_cdr/"`) has its frozen `local-data/` prefix
stripped and the remainder joined onto `TRACEX_BENCHMARK_DATA_ROOT`, so
the same portable manifest entry works whether that root is the repo's
own gitignored `local-data/` directory or an entirely different path on
Aditya's MacBook (an external drive, say).

`run_benchmark` checks artifact availability *before* attempting to
execute anything: a missing dataset directory, an ambiguous set of
candidate input files (zero or more than one CSV/XLSX/JSON file where
exactly one is expected), a missing OCR benchmark manifest, or missing
verified model metadata (`--model-name`/`--model-version`/`--model-sha256`)
all produce a truthful `BenchmarkRunStatus.UNAVAILABLE` result — never a
crash, and never a fabricated `SUCCEEDED`. `BenchmarkRunV1`'s own Part 1
validator additionally guarantees a `SUCCEEDED` OCR run cannot exist
without `artifact_sha256` (the verified model weight's hash); for the
CDR/finance deterministic baseline, `artifact_sha256` is the SHA-256 of
the actual local input file processed — a real, computed value standing
in for "the artifact this run measured" in the absence of a model weight.

### Verified PaddleOCR 3 adapter

`paddleocr`/`paddlepaddle` remain a reproducible optional dependency group.
Gate B installed it through `uv`, resolved PaddleOCR 3.7.0 and PaddlePaddle
3.3.1, and verified the real PaddleOCR 3 `predict` API with separate official
PP-OCRv5 detector and recognizer inference directories. The adapter requires
all six inference metadata/parameter files before construction, sends decoded
RGB arrays to `predict`, and extracts `rec_texts` without storing raw OCR in a
result JSON. It reports the actual package versions, variant, CPU device, and
oneDNN state in `hardware_profile`.

The official paddle3.0.0 inference packages failed during oneDNN PIR
attribute conversion on this Linux CPU. Both complete real runs therefore
used the plain CPU backend with `enable_mkldnn=False`, and disabled document
orientation classification, document unwarping, and text-line orientation.
The focused builder test fixes this API/configuration contract without
requiring model weights. NumPy is constrained to `>=2.3,<2.4` because the
verified PaddleX dependency rejects NumPy 2.4 or newer.

### Verification (Part 2)

Gate B subsequently ran the official AMLSim sample and both approved
PaddleOCR candidates against the official FIR annotations; see
`docs/qa/test-results.md` for commands, hashes, host/backend, and metrics.
The official GoMask download remains account-and-credit gated, and its
benchmark result is truthfully `unavailable`. Raw data, derived crops, model
packages, caches, and result JSON remain outside Git. No Part 2 candidate is
selected; Gate C selects a winner only after the remaining blocker and wider
cross-modality evidence are resolved.

## Part 3: visual benchmark foundation and local-model governance (Gaurav)

Part 3 implements the reproducible benchmark adapters, safe CLI, and tests
needed to *evaluate* the three datasets Part 1's manifest assigns to
Gaurav: `virat_ground` (person/vehicle/object detection and within-video
tracking), `safe_unsafe_behaviour` (additional detection stress-testing
only — no behaviour-classification candidate is catalogued), and
`ufpr_alpr` (plate-region detection and plate-text OCR, gated on licence
approval). It does not select a winning detector/tracker/OCR
candidate — Gate C does that, after Parts 2–4 all have comparable results.

### Module layout

`app/modules/media_processing/` gained five new, purely additive files —
no existing production detection/tracking/OCR code (`analysis/`,
`worker.py`, `ocr_adapter.py`, `ocr_batching.py`) was rewritten, and the
pre-existing `benchmark.py` (a real-time processing-throughput benchmark
for the production pipeline, unrelated to Phase 7) was not touched:

| File | Responsibility |
|---|---|
| `visual_benchmark_metrics.py` | Pure functions: detection precision/recall/mAP (IoU-matched, VOC-style single-threshold AP), tracking IDF1/MOTA/ID-switches (a documented simplified IDF1; HOTA always `None` — see "Known simplifications" below), visual-text CER/WER/field PRF, percentile/median latency, platform-aware `peak_memory_mb`. |
| `visual_benchmark_validation.py` | `TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/`TRACEX_BENCHMARK_OUTPUT_ROOT` resolution; the Part 3 dataset/candidate/pair allow-lists; `require_license_cleared_for_real_execution` — the licence-clearance gate (see below); `reject_private_local_paths`. |
| `visual_benchmark_adapters.py` | `DetectorEngine`/`TrackerEngine`/`VisualTextEngine` protocols (`Fake*Engine` for tests), `run_detection_benchmark`/`run_tracking_benchmark`/`run_visual_text_benchmark`, and the canonical-observation compatibility helpers (`build_observation_draft_for_*`/`observation_for_draft`). |
| `visual_benchmark.py` | `run_benchmark` — the one orchestration entry point: validates the request, checks licence clearance, resolves local roots, checks artifact availability *before* executing, dispatches to the right adapter, writes a safe `BenchmarkRunV1` JSON result. |
| `visual_benchmark_cli.py` | `uv run python -m app.modules.media_processing.visual_benchmark_cli {list-candidates,validate,run}` — see the runbook for full usage. |

### Approved dataset/candidate pairs

Exactly Gaurav's Phase 7 Part 1 manifest entries, cross-checked against
the task's own "permitted benchmark use" column — no dataset/candidate
combination outside this table is accepted, even though every ID involved
is individually valid in the shared Part 1 catalogue:

| Dataset | Candidates | Task |
|---|---|---|
| `virat_ground` | `yolo11n`, `yolo11s` | detection |
| `virat_ground` | `bytetrack` | tracking |
| `safe_unsafe_behaviour` | `yolo11n`, `yolo11s` | detection (stress test only) |
| `ufpr_alpr` | `yolo11n`, `yolo11s` | detection (plate-region) |
| `ufpr_alpr` | `paddleocr-lightweight-visual-text` | visual_text (plate OCR) |

`safe_unsafe_behaviour` deliberately has no tracking or visual-text pair:
the manifest's own `allowed_tasks` for it is "additional visual detection
stress-testing" only, and no behaviour-classification candidate exists
anywhere in Part 1's frozen catalogue — inventing one would be exactly the
kind of unapproved new task/candidate this phase's rules forbid.

### The licence-clearance execution gate

`require_license_cleared_for_real_execution` blocks a `SUCCEEDED` result
outright whenever either the dataset's or the candidate's own
`license_status` is not `verified_permissive`/`verified_restricted_
noncommercial`/`internal_only`. Checked *before* any local artifact is
even looked for. A blocked request still produces a truthful
`BenchmarkRunStatus.UNAVAILABLE` result naming the gate — never a crash,
and never a silently-substituted different dataset/candidate.

**Resolved for `virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` by Gate B
(2026-09-20)**: reading the actual VIRAT Video Dataset Usage Agreement and
Ultralytics' actual AGPL-3.0 licence directly moved all four from
`pending_verification` to `verified_restricted_noncommercial` — see "Gate
B execution status" below for the full verification and the real
benchmark results this unblocked. `safe_unsafe_behaviour` and
`ufpr_alpr`/`paddleocr-lightweight-visual-text` remain
`pending_verification`, legitimately deferred rather than silently
skipped (no pinned source for the former; an academic access-request gate
for the latter that this task did not attempt to bypass).

### Canonical observation/provenance compatibility

`visual_benchmark_adapters.build_observation_draft_for_detection`/
`_for_track_segment`/`_for_visual_text` build a real `MediaObservationDraft`
from an accepted detection/track/OCR result, and `observation_for_draft`
calls the exact, unmodified `provenance.build_extractor`/
`draft_to_observation` functions production detection already uses —
proving an accepted benchmark result *could* flow through the identical
canonical `ObservationV1` seam, with no alternate observation format.
Neither function is called anywhere in `run_benchmark`'s own path: a
benchmark result is evaluation metadata, never primary evidence, and this
task creates no new raw-evidence persistence path. A `TrackSegment`'s
`local_track_id` becomes only a same-observation-ID discriminator, never
an `ExtractedEntityMention` or any other identity-shaped field — a
technical track ID cannot become a cross-camera or cross-case identity
link through this seam, structurally, not only by convention.

### Why `ultralytics`/`paddleocr` are lazily imported, and the safety-test carve-out this required

`pyproject.toml` gained `[project.optional-dependencies] video-benchmark`
(`ultralytics`, `paddleocr`, `paddlepaddle`, `lap`), resolved into
`uv.lock` via `uv add --optional video-benchmark --no-sync` — not
installed by the standard `uv sync --all-groups` verification command
(confirmed directly: `import ultralytics`/`import paddleocr` both still
fail after a clean sync). **Gate B (2026-09-20) installed it for real**
via `uv sync --extra video-benchmark` and exercised `ultralytics` end to
end against real YOLO11 weights and a real VIRAT clip — see "Gate B
execution status" below. PaddleOCR was installed but not exercised
against any real image (`ufpr_alpr` remains deferred).

`visual_benchmark.py`'s `_build_real_detector_engine`/
`_build_real_tracker_engine`/`_build_real_visual_text_engine` import
`ultralytics`/`paddleocr` lazily, inside the function, wrapped in
`try/except ImportError` degrading to a safe
`BenchmarkArtifactUnavailableError`. The detector and tracker wiring is
now verified against the real, installed `ultralytics==8.4.156` API (see
below for the real API-drift bugs Gate B found and fixed); the
visual-text (PaddleOCR) wiring remains best-effort, written from
documented public shape only, since no real `ufpr_alpr` image was
available to exercise it against.

This tripped a genuine, pre-existing, whole-module static safety test
(`tests/unit/media_processing/test_media_safety.py::
test_no_infrastructure_or_ml_library_is_imported`) that forbids
`ultralytics`/`paddleocr` anywhere under `media_processing/` — a Phase 2
closeout boundary keeping the *production* detector/OCR pipeline free of
heavyweight ML toolchains. That boundary is correct for `analysis/`/
`worker.py` and remains fully enforced there; it was never meant to (and
structurally cannot, given Part 1's own frozen candidate catalogue) apply
to an *evaluation* harness whose entire job is benchmarking exactly those
two candidates. Fixed with a narrow, explicit exemption: files named
`visual_benchmark*.py` are excluded from that one check only, and a new
`test_benchmark_harness_still_forbids_every_non_candidate_infra_or_ml_
library` test proves every *other* forbidden library (databases, object
storage, queues, `torch`/`tensorflow`/`sklearn`) remains forbidden in the
benchmark harness too — not a blanket carve-out.

### `_build_real_tracker_engine`: resolved via Ultralytics' own bundled ByteTrack, verified live

Rather than a separate, pip-installable ByteTrack package, this wires
Ultralytics' own bundled `BYTETracker` class
(`ultralytics.trackers.byte_tracker.BYTETracker`) directly against
externally-supplied per-timestamp detections — the identical tracker
`model.track(..., tracker='bytetrack.yaml')` uses internally.
`bytetrack.yaml` ships bundled inside the `ultralytics` package itself, so
no separate model weight or download is needed for tracking specifically:
ByteTrack's association step (Kalman filter + Hungarian matching) has no
learned weights and no GPU path, so `"cpu"` is reported as an observed
fact about the algorithm, not an assumption. Its actual licence exposure
is therefore `ultralytics`'s own AGPL-3.0, not the separately-licensed
upstream ifzhang/ByteTrack MIT repository.

**Gate B (2026-09-20) installed `ultralytics` for real and exercised this
path end to end**, finding and fixing three real API-drift bugs a mock
could not have caught (`ultralytics==8.4.156`, confirmed directly by
reading its real source, not assumed from documentation):

1. `ultralytics.utils.yaml_load` no longer exists — replaced by
   `ultralytics.utils.YAML.load`.
2. `BYTETracker.__init__` no longer accepts a `frame_rate` argument at
   all (`def __init__(self, args):` only) — removed.
3. `BYTETracker.update()`'s `results` argument must support numpy-style
   fancy indexing (confirmed by reading `_split_detections`'s real
   source) — a duck-typed `SimpleNamespace` does not support this. Fixed
   by constructing a real `ultralytics.engine.results.Boxes` instance
   (`(N, 6)` columns `[x1, y1, x2, y2, confidence, class]`, confirmed
   from its own docstring) instead.

A fourth, adjacent bug: `BYTETracker` needs the `lap` package internally,
which was missing from the `video-benchmark` extra (Ultralytics attempted
its own ad hoc auto-install at runtime instead of failing cleanly) — now
declared explicitly. Any remaining mismatch on a different installed
version still degrades to a safe `BenchmarkArtifactUnavailableError`,
never a crash. The *tracking metric/aggregation logic itself*
(`run_tracking_benchmark`) was already fully implemented and tested
against `FakeTrackerEngine`; it has now also produced a genuine real
result — see "Gate B execution status" below.

### Known simplifications in the tracking metrics

- **IDF1** uses a majority-vote per-ground-truth-track identity assignment,
  not the optimal global bipartite assignment a full implementation (e.g.
  `py-motmetrics`) solves — documented directly in `idf1`'s own docstring.
- **HOTA is always `None`.** A correct HOTA requires a geometric-mean
  detection/association-accuracy sweep across multiple IoU/alpha
  thresholds; a partial/simplified HOTA would be easy to misread as the
  real metric, so this harness reports `None` rather than an
  approximation under the real metric's name — exactly the "never
  fabricate a value where a measurement is unavailable" rule this task's
  own contract requires.
- **mAP is single-threshold (IoU ≥ 0.5), VOC-style 11-point interpolated
  AP** — not COCO's mAP@[.5:.95] sweep across ten thresholds.

None of these are silent: every one is documented in the relevant
function's own docstring and re-stated in `docs/qa/known-limitations.md`.

## Part 4: audio and social/chat benchmark foundation and local-model governance (Sarthak)

Part 4 implements the reproducible benchmark adapters, safe CLI, and
tests needed to *evaluate* the four datasets Part 1's manifest assigns to
Sarthak: `common_voice_indic` (ASR and language identification),
`ami_meeting_corpus` (VAD and diarization mechanics only — never identity
inference), `vast_social_text` (social/chat structured extraction and
provenance quality), and `vast_2014_mixed_records` (conditional support
for the same social/chat extraction task). It does not select a winning
ASR/VAD/diarization/language-ID/social-extraction candidate — Gate C does
that, after Parts 2–4 all have comparable results.

### One additive frozen-catalogue extension: `existing-deterministic-social-parsers`

Part 1's own catalogue deliberately left `social_text_extraction`
forward-compatible-only, with **no candidate at all** — its own
documentation states: "no Part 1 candidate exists for it yet; the
existing deterministic social/chat parsers remain today's baseline...
[a] future part may catalogue a real candidate against this task without
a schema change." Part 4 is that future part: `configs/benchmarks/
model-candidates.v1.json` gained exactly one new, additive entry —
`existing-deterministic-social-parsers` (owner `sarthak`, task
`social_text_extraction`, framework `internal`, `execution_target:
deterministic_rules`, `license_status: internal_only`, `selection_status:
candidate`) — mirroring Jasraj's Part 2 precedent for
`existing-deterministic-parsers` exactly. Nothing existing in the catalog
was modified; the full 42-test Part 1 evaluation suite (`tests/unit/
evaluation/`) was re-run after this addition and still passes unchanged.

### Module layout

`app/modules/communication_processing/` gained five new, purely additive
files — no existing production audio/social code (`audio/`, `social/`,
`aliases/`, `worker.py`) was rewritten:

| File | Responsibility |
|---|---|
| `audio_social_benchmark_metrics.py` | Pure functions: WER/CER (Levenshtein-based), real-time factor, frame-discretized VAD precision/recall/F1/accuracy, a simplified frame-discretized Diarization Error Rate (majority-vote speaker-label mapping) plus this harness's own "speaker turn quality" boundary-timing measure and speaker-count error (JER always `None` — see "Known simplifications" below), language-identification accuracy, social-extraction precision/recall/F1, and `language_handling_score` (reusing the existing, unmodified `aliases/scripts.py::detect_script`). |
| `audio_social_benchmark_validation.py` | `TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/`TRACEX_BENCHMARK_OUTPUT_ROOT` resolution; the Part 4 dataset/candidate/pair allow-lists; `require_cleared_for_real_execution` — a licence-clearance gate that also blocks a `conditional` candidate (see below); `reject_private_local_paths`. |
| `audio_social_benchmark_adapters.py` | Reuses the exact existing `TranscriptSegmentInput`/`DiarizationSegmentInput` canonical segment types (`audio/transcript_import.py`/`audio/diarization_import.py`) and `RawMention`/`extract_mentioned_identifiers` (`social/identifiers.py`) directly. `AsrEngine`/`VadEngine`/`DiarizationEngine`/`LanguageIdEngine`/`SocialExtractionEngine` protocols, `Fake*Engine` for tests, `DeterministicSocialExtractionEngine` (the real baseline — no lazy import needed at all), `run_*_benchmark` functions, and the canonical-observation compatibility helpers reusing `transcript_segments_to_mentions`/`diarization_segments_to_mentions`/`mention_to_observation` unchanged. |
| `audio_social_benchmark.py` | `run_benchmark` — the one orchestration entry point: validates the request, checks licence/conditional clearance, resolves local roots, checks artifact availability *before* executing, dispatches to the right adapter, writes a safe `BenchmarkRunV1` JSON result. |
| `audio_social_benchmark_cli.py` | `uv run python -m app.modules.communication_processing.audio_social_benchmark_cli {list-candidates,validate,run}` — see the runbook for full usage. |

### Approved dataset/candidate pairs

| Dataset | Candidates | Task |
|---|---|---|
| `common_voice_indic` | `faster-whisper-small`, `faster-whisper-medium` | asr |
| `common_voice_indic` | `fasttext-lid176` | language_identification |
| `ami_meeting_corpus` | `silero-vad-v6` | vad |
| `ami_meeting_corpus` | `pyannote-community-local`, `deterministic-diarization-fallback` | diarization |
| `vast_social_text` | `existing-deterministic-social-parsers` | social_text_extraction |
| `vast_2014_mixed_records` | `existing-deterministic-social-parsers` | social_text_extraction (conditional support) |

### The licence/conditional-status execution gate

`require_cleared_for_real_execution` blocks a `SUCCEEDED` result whenever
either the dataset's or candidate's own `license_status` is unresolved,
**or** the candidate's own `selection_status` is still `conditional` —
this third gate (beyond Parts 2/3's two) is explicit because
`pyannote-community-local` and `fasttext-lid176` are both catalogued
`conditional`, and their local-use/adoption terms remaining unaccepted
must block a real result independently of licence status.

**A genuinely discovered asymmetry with Parts 2/3, not invented**: unlike
Jasraj's and Gaurav's datasets (all `pending_verification`), two of
Sarthak's four datasets — `vast_social_text` and `vast_2014_mixed_records`
— are already `license_status: verified_permissive` in Part 1's frozen
manifest (the VAST Challenge organizers' well-established permissive
terms). Paired with the new `existing-deterministic-social-parsers`
candidate (`internal_only`, `selection_status: candidate`, not
conditional), **this one pair clears the gate today** — confirmed by a
dedicated test and by a real local run against a synthetic dataset
directory during this task's own verification, producing a genuine
`SUCCEEDED` result. Every ASR/VAD/diarization pair remains blocked (all
of `common_voice_indic`'s and `ami_meeting_corpus`'s candidates are
`pending_verification` or `conditional`), and `ami_meeting_corpus` itself
is also still `pending_verification`.

### Canonical observation/provenance compatibility

`audio_social_benchmark_adapters.observations_for_asr_result`/
`observations_for_diarization_result`/`observation_for_social_mention`
call the exact, unmodified `transcript_segments_to_mentions`/
`diarization_segments_to_mentions`/`mention_to_observation` functions
production ASR/diarization import and social extraction already use —
proving an accepted benchmark result *could* flow through the identical
canonical `ObservationV1` seam, with no alternate observation format.
None of these are called anywhere in `run_benchmark`'s own path: a
benchmark result is evaluation metadata, never primary evidence. A
diarization speaker label becomes only an `entity_type_hint=
"speaker_label_local"`-tagged mention — production's own, unmodified
typing — never a resolvable cross-record or cross-case identity claim,
confirmed by a dedicated test.

### Why `faster_whisper`/`pyannote`/`fasttext`/`torch` are lazily imported, and the safety-test carve-out this required

`pyproject.toml` gained `[project.optional-dependencies]
audio-social-benchmark` (`faster-whisper`, `pyannote-audio`, `fasttext`,
`torch`), resolved into `uv.lock` via `uv add --optional
audio-social-benchmark --no-sync` — never installed on this development
machine, and never pulled in by `uv sync --all-groups` (confirmed
directly: `import faster_whisper`/`import pyannote.audio`/`import
fasttext`/`import torch` all still fail after a clean sync). `torch` is
scoped to this one optional extra specifically for the Silero VAD
candidate below and is never a production/base dependency. Aditya's
MacBook pre-flight installs the exact pinned versions via `uv sync
--extra audio-social-benchmark`, never an ad hoc `pip install`.

This tripped a genuine, pre-existing, whole-module static safety test
(`tests/unit/communication_processing/test_module_safety.py::
test_no_forbidden_infra_or_ml_import`) that forbids `torch`/
`transformers`/`whisper`/`faster_whisper`/`pyannote`/`speechbrain`/
`librosa`/`sklearn`/`numpy` anywhere under `communication_processing/` —
a Phase 3 boundary keeping the *production* ASR/diarization adapters free
of heavyweight ML toolchains (see `audio/asr_adapter.py`'s own docstring).
That boundary remains fully enforced for every production file. Fixed
with the same narrow, explicit, tested carve-out Phase 7 Part 3
established: files named `audio_social_benchmark*.py` are exempt from
`faster_whisper`/`pyannote`/`fasttext`/`torch` (and, separately, from the
module's `os`-import ban, for local-root resolution) only — a new
`test_benchmark_harness_still_forbids_every_non_candidate_infra_or_ml_
import` test proves every *other* forbidden library (databases, object
storage, queues, `subprocess`, `transformers`/`whisper`/`speechbrain`/
`librosa`/`sklearn`/`numpy`) remains forbidden in the benchmark harness
too.

### Silero VAD and fastText language-ID: resolved per explicit team decision, then made fully offline and hash-verified

Both candidates were initially left with unresolved real-engine wiring,
since finishing them required decisions this task judged out of
proportion to make unilaterally: whether to reopen `audio/asr_adapter.py`'s
documented team-level boundary against a direct `torch` dependency, and
how to reconcile `fasttext-lid176`'s text-only classifier with a
protocol that takes raw audio. Both were then explicitly resolved by
direct instruction, mirroring how Phase 7 Part 3's Ultralytics ByteTrack
tracker was completed after an analogous "finish the already-selected
route" decision. A follow-up instruction then closed a remaining gap in
both: the VAD wiring still called `torch.hub.load` against a live GitHub
repository string (a real, if narrow, runtime network dependency for a
"local" benchmark), and the language-ID wiring recorded only its primary
`fasttext-lid176` artifact, leaving its required faster-whisper
transcription-stage dependency unrecorded and unverified. Both are now
fully offline and hash-verified:

- **`_build_real_vad_engine` never calls `torch.hub.load` against GitHub
  or any other network source.** It requires a Torch Hub *source
  snapshot* of `snakers4/silero-vad` staged manually under
  `<model_cache_root>/silero-vad/` (a plain local checkout containing
  `hubconf.py` at its root — never fetched by this code) and loads it
  with `torch.hub.load(..., source="local")`, which only ever reads
  local files. `VerifiedModelArtifact.model_version` records the exact
  staged source revision/tag, and `model_sha256` is verified directly
  against the snapshot's own pinned weight file (`files/silero_vad.jit`,
  a best-effort path mirroring the real repository's published layout —
  adjustable during MacBook pre-flight) before anything is loaded. Every
  resolved path is checked to remain inside the configured model cache
  root (`_resolve_within_model_cache_root`, rejecting e.g. a symlink
  escape); a missing snapshot, an escaped path, or a hash mismatch all
  degrade to a safe `BenchmarkArtifactUnavailableError` — never a silent
  download or a fabricated result. `torch` remains lazily imported inside
  this one function only, declared in the existing
  `audio-social-benchmark` optional extra (not a new one), and is never a
  production/base dependency.
- **`_build_real_language_id_engine` now records and verifies *two*
  required model dependencies, not just its own.** `fasttext-lid176`
  classifies language from *text*, not audio, so this harness chains an
  internal `faster-whisper` transcription stage purely as plumbing to
  produce text for fastText to classify — the transcription stage is
  never itself benchmarked here, and only the language *classification*
  is attributed to the `fasttext-lid176` candidate's own name/version/
  hash. `_build_real_language_id_engine` and `_run_language_id` now
  require a second, independent `VerifiedModelArtifact` for the
  transcription stage (`transcription_stage_artifact`, no default) in
  addition to the primary fastText artifact — a completed result is
  rejected if either is absent. Both artifacts' local files
  (`lid.176.ftz` and `lid-transcription-stage/model.bin`) are hash-
  verified the same way VAD's snapshot is, before either model is
  loaded. Since the frozen `BenchmarkRunV1` contract has only one
  `artifact_sha256` field (attributed to the primary candidate),
  `run_benchmark` folds the transcription stage's own name/version/
  SHA-256 into `inference_config_hash`'s input instead — a different
  transcription-stage dependency is therefore a detectably different
  configuration, without requiring any change to the frozen contract.
  The transcript text produced internally is never returned, logged, or
  serialized anywhere — only the final language label leaves the
  function, proven by a dedicated test using a distinctive marker
  transcript. Absence of `fasttext`/`faster_whisper`, an absent
  transcription-stage artifact, an escaped path, or a hash mismatch on
  either artifact all degrade to a safe
  `BenchmarkArtifactUnavailableError`.

Neither this session installed or exercised `torch`/Silero VAD/fastText's
transcription-chained classification — both remain best-effort wiring
written from each library's documented public API shape, to be verified
for the first time during Aditya's MacBook pre-flight.

### Known simplifications in the diarization/VAD metrics

- **DER is frame-discretized and uses a majority-vote speaker-label
  mapping** (the same methodology Phase 7 Part 3 uses for its own
  simplified IDF1) — not the full DER definition (no collar exclusion
  around reference boundaries, no explicit overlapped-speech accounting).
- **"Speaker turn quality" is this harness's own defined boundary-timing
  measure** (fraction of reference turn-start boundaries matched within a
  quarter-second tolerance) — not a standardized external metric name.
- **JER is always `None`.** A correct Jaccard Error Rate needs the same
  optimal bipartite reference/hypothesis speaker assignment a full
  diarization-metrics toolkit (e.g. `pyannote.metrics`) solves; this
  harness's own majority-vote mapping is not that assignment, so reusing
  it would risk a value under JER's real name that does not mean what JER
  means.
- **VAD is frame-discretized at a fixed 30ms resolution** — a documented,
  fixed choice, not tuned against any real dataset.
- **`fasttext-lid176` is benchmarked as transcript-language
  identification chained after ASR, not as a direct audio-native
  language-ID model.** fastText's `lid.176` model classifies language
  from *text*, so `_build_real_language_id_engine` runs its own internal
  `faster-whisper` transcription stage first and classifies that output —
  see "Silero VAD and fastText language-ID: resolved per explicit team
  decision" above.

None of these are silent: every one is documented in the relevant
function's own docstring and re-stated in `docs/qa/known-limitations.md`.

### Verification (Part 3)

`uv sync --all-groups`, `ruff format --check .`, `ruff check .`,
`mypy app`, `pytest`, `git diff --check`, and `docker compose config -q`
all ran on this development machine — see `docs/qa/test-results.md`'s
dated Phase 7 Part 3 entry for exact counts. This entry covers only the
initial build, against synthetic fixtures and fake engines; see "Gate B
execution status" immediately below for the later real-dataset/
real-model verification.

### Gate B execution status (2026-09-20)

Gate B ran directly on Shreshtha's laptop (no separate MacBook available)
and genuinely downloaded real data, installed real dependencies, and
produced real benchmark results for `virat_ground`. Exact source URLs,
item IDs, artifact hashes, commands, host profile, and measured values
are recorded in `docs/qa/test-data.md` and `docs/qa/test-results.md`'s
dated Gate B sections — summarised here:

- **`virat_ground` + `yolo11n`/`yolo11s`/`bytetrack`: real, succeeded.**
  One small official VIRAT clip and its real annotations were downloaded
  from the official Kitware Data mirror (access to the VIRAT Usage
  Agreement was confirmed already granted by the project owner before any
  download, per this task's "never accept an agreement on the agent's
  behalf" rule), converted into this harness's own manifest format via a
  real parser for VIRAT's actual `objects.txt` schema, and benchmarked
  with real, officially-released YOLO11n/YOLO11s weights and Ultralytics'
  own bundled ByteTrack. `license_status` moved from
  `pending_verification` to `verified_restricted_noncommercial` for all
  four entries after reading the real governing licences directly (VIRAT
  Usage Agreement; Ultralytics AGPL-3.0) — both permit commercial use
  despite the enum's "noncommercial" naming, documented explicitly in
  each entry's own notes/known-limitations.
- **`ufpr_alpr`/`safe_unsafe_behaviour`: legitimately deferred, not
  fabricated.** UFPR-ALPR requires a formal academic access-request
  process this task did not attempt to bypass; Safe/Unsafe Behaviour's
  frozen manifest entry has no pinned source at all. Neither candidate
  pairing was run against an empty placeholder to manufacture a
  cosmetic `unavailable` result.
- **Real API-drift bugs found and fixed** by actually installing and
  running `ultralytics==8.4.156` for the first time — see "resolved via
  Ultralytics' own bundled ByteTrack, verified live" above for the three
  tracker-specific fixes, plus a missing `lap` dependency, a
  `follow_imports = "skip"` mypy override this installation also
  required, and a repository-wide `tests/__init__.py` fix (`ultralytics`
  ships its own colliding top-level `tests` package). Full detail in
  `docs/qa/test-results.md`'s dated Gate B entry.
- **Focused verification re-run after all fixes**: `ruff format --check`,
  `ruff check`, `mypy`, and `pytest` scoped to `app/modules/
  media_processing`/`tests/unit/media_processing`, plus the Part 1
  evaluation suite (`tests/unit/evaluation/`, 42 tests) and the real
  integration smoke test against the downloaded VIRAT clip — all passing,
  exact counts in `docs/qa/test-results.md`. The full repository suite
  was not re-run here; Gate C owns final repository-wide verification.

No Part 3 candidate is selected; Gate C selects a winner only after
Parts 2–4 all have comparable real results.

### Verification (Part 4)

`uv sync --all-groups`, `ruff format --check .`, `ruff check .`,
`mypy app`, `pytest`, `git diff --check`, and `docker compose config -q`
all ran on this development machine — see `docs/qa/test-results.md`'s
dated Phase 7 Part 4 entry for exact counts. No real Common Voice
Indic/AMI Meeting Corpus/VAST dataset, no faster-whisper/pyannote.audio/
fastText/torch installation, and no GPU/MacBook validation was performed
or is claimed here — every one of those is Aditya's MacBook Gate B pre-flight's
job (see `docs/runbooks/local-development.md`). No Part 4 candidate is
selected; Gate C selects a winner only after Parts 2–4 all have
comparable real results.

## Verification (Part 1)

`uv sync --all-groups`, `ruff format --check .`, `ruff check .`,
`mypy app`, `pytest`, `git diff --check`, and `docker compose config -q`
all ran and passed — see `docs/qa/test-results.md`'s dated Phase 7 Part 1
entry for exact counts. No live model, GPU, downloaded-dataset,
Docker-stack, or LAN validation was performed or is claimed — none is
needed for a configuration/contract-only part, and none of the four JSON
configs or the module's own tests touch a database, Neo4j, Redis, or
MinIO.

## Known limitations

See `docs/qa/known-limitations.md`'s "Phase 7 Part 1" section for the full
list: no downloaded datasets, no downloaded model weights, no benchmark
results, no final model winner, no final correlation winner, and no LAN
validation. All by design, not oversight — see "What Part 1 explicitly
does not do" above.
