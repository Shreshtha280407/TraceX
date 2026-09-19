# Phase 7 Part 1: Evaluation Foundation, Dataset Manifest, and Local-Model Governance

Owner: Nipun (Part 1); Jasraj (Part 2, this document's added section
below). Status: **Part 1 in progress** (this document's own scope is
complete and gated below); **Part 2 Gate B incomplete** (real AMLSim and
both PaddleOCR runs succeeded, while the official GoMask download remains
account-and-credit gated — see "Part 2: structured-data and local OCR
benchmarking" below). Phase 7 overall is not complete. Part 1 freezes the
referee and scoreboard later Phase 7 parts
build against; it downloads no dataset, installs no model, and selects no
winner.

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
