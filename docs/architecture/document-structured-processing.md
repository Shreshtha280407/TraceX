# Document, FIR/OCR/NER, CDR, and Financial Evidence Processing (Phase 3 — Jasraj)

This is the authoritative reference for Jasraj's Phase 3 work: real local
document/OCR/NER/relation extraction and real, vectorized CDR/finance
micro-batch processing, submitted through Nipun's `/observations` endpoint.
It builds on, and does not replace, two earlier docs in this same
directory:

- `document-and-structured-processing-v1.md` — Phase 1's foundational
  design (deterministic FIR regex extraction, CSV/XLSX/JSON parsing,
  provenance policy). Everything there is unchanged.
- `structured-processing-worker.md` — Phase 2's one-shot CLI/claim/
  submit-result integration (`client.py`, `input_resolver.py`,
  `run_once`/`main`). Also unchanged in shape; this phase adds a second,
  additional way `run_once` can deliver a job's output (batches, not just
  a single terminal result) — see "Producer rules" below.

## Supported formats — what's real and what's deliberately unsupported

| Domain | Format | Status |
|---|---|---|
| Document/FIR | Text-bearing PDF | Real: embedded-text extraction, trust-classified per page |
| Document/FIR | Scanned/image PDF page | Real: local Tesseract OCR fallback, per page |
| Document/FIR | DOCX, TXT | Real: unchanged from Phase 1 (no OCR concept — no page/scan distinction) |
| Document/FIR | Structured JSON report export | **Not supported.** `SourceType.DOCUMENT` only routes PDF/DOCX/TXT (Phase 1, unchanged) — no JSON report shape is defined or routed. Not added in this phase (see "Open questions" in `phase-3-decisions.md`) |
| CDR | CSV, XLSX, JSON | Real: vectorized/chunked (CSV via Polars, XLSX via PyArrow-batched openpyxl, JSON bounded-then-chunked) |
| Finance | CSV, XLSX, JSON | Real: identical chunking strategy to CDR |

Nothing here changes `evidence_lifecycle/routing.py`: `SourceType.DOCUMENT`
already accepted PDF/DOCX/TXT, and `SourceType.CDR`/`SourceType.FINANCIAL`
already accepted CSV/XLSX/JSON, since Phase 1. **No routing change was
needed for this phase** — every format this phase processes was already
reachable through a real upload.

## Text-layer trust checks and selective OCR policy

See `app/modules/structured_processing/document/page_trust.py`. Every PDF
page is classified into exactly one of four outcomes before any extraction
runs against it:

| Level | Meaning | Behavior |
|---|---|---|
| `TEXT_TRUSTED` | Substantial, readable embedded text | Extraction runs directly; OCR never runs |
| `SCANNED_NO_TEXT` | No meaningful embedded text (< 20 non-whitespace chars) | OCR candidate |
| `UNTRUSTWORTHY_TEXT_LAYER` | Text exists but fails a quality check | OCR candidate — never blindly trusted just because `len(text) > 0` |
| `CORRUPT` | `pypdf` itself raised extracting this page | Reported safely in the terminal result's checkpoint; the rest of the document is still processed |

Quality checks for `UNTRUSTWORTHY_TEXT_LAYER` (both must pass for
`TEXT_TRUSTED`):

- **Printable-character ratio ≥ 0.85** — the fraction of characters that
  are real content (whitespace counts; the Unicode replacement character
  and other C0/C1 control characters do not).
- **Token-quality ratio ≥ 0.6** — the fraction of whitespace-separated
  tokens containing at least one letter or digit.

None of these thresholds are tuned against a large real-world corpus (see
`docs/qa/known-limitations.md`) — they are fixed, documented heuristics
applied consistently, not a trained classifier.

A `TEXT_TRUSTED` page **never** runs OCR — this is a hard rule, not a
best-effort default, so a large text-native PDF is never re-processed
through a slow, unnecessary OCR pass.

## Real local OCR (no cloud API, no bundled model)

`app/modules/structured_processing/document/ocr.py` + `pdf.py`'s
`render_pdf_pages`.

- **Rendering**: `pypdfium2` (a self-contained PDF renderer with no
  external system binary of its own — unlike `pdftoppm`/`poppler-utils`),
  at a configurable DPI (`DOCUMENT_OCR_DPI`, default 300).
- **Recognition**: the same real, local `tesseract` binary via
  `pytesseract` that `media_processing.analysis.tesseract_ocr` already
  established for image/video evidence — no bundled model weights, no
  network access, no cloud OCR API. Requires the `tesseract-ocr` system
  package (already in this repository's `Dockerfile`).
- **Grouping**: tesseract's own layout analysis groups words into lines;
  each line becomes one `OcrRegion` with its own confidence and normalized
  `[0, 1]` bounding box (`bbox_xyxy_normalized`) — bbox precision is
  therefore **line-level, not word-level**, the same choice
  `media_processing`'s own OCR adapter already made for the identical
  reason (a line is generally the more useful atomic OCR observation for
  investigative evidence than an isolated word).
- **Runtime unavailable**: if the `tesseract` binary or its `eng` language
  pack genuinely isn't installed, the pages that needed OCR are reported
  `DEFERRED` in the terminal result's checkpoint — trusted pages' batches
  are still submitted; the job never crashes or fabricates a result for
  pages it couldn't process.

### OCR runtime setup

Already satisfied by this repository's `Dockerfile` (`tesseract-ocr`
installed for `media_processing`). For a host-run worker: install
`tesseract-ocr` (and any non-`eng` language pack via
`tesseract-ocr-<lang>`) via your system's package manager.

### Gate B macOS OCR configuration

`OcrConfig`'s two OCR-quality knobs (`page_segmentation_mode`, `binarize`)
were tuned twice against real Gate B evidence from Aditya's MacBook, not
assumed from documentation or from this project's own (Linux) development
environment, where every tested combination already reads the fixtures at
`recall=1.00` and gives no distinguishing signal at all.

**Round 1 (macOS Tesseract 5.5.0).** `binarize=True` (a fixed grayscale
threshold applied before OCR, briefly the default in commit f84aa4d) was
found to actively destroy valid characters on that Tesseract build: recall
on the `91/2026` fixture dropped from an already-poor `0.33` to `0.00`,
with no `fir_reference` recovered at all. Reverted to `binarize=False` by
default.

**Round 2 (macOS Tesseract 5.5.3).** A full matrix —
`page_segmentation_mode` ∈ {3, 4, 6, 11, 12} × `binarize` ∈ {`False`,
`True`} — was run against the real scanned-PDF fixtures for all three FIR
identifiers this project's tests exercise. Measured results, `binarize=False`
unless noted:

| Fixture | PSM | Raw OCR text | Result |
|---|---|---|---|
| `91/2026` | 6 | `FIR Nex 91/2026 Police Station: Colaba Phone: 9876543210 Amount Rs. 25000` | recall `0.67` — `25000`/`9876543210` extracted, `91/2026` present in text but not (at the time) matched by the label regex |
| `20/2026` | 6 | `FIR Ma 20/2026 Police Station: Colaba` | `20/2026` present in text but not (at the time) matched |
| `30/2026` | 6 | `FIR Not 30/2026 tiled today` | `30/2026` present in text but not (at the time) matched |

All `binarize=True` results were worse than the corresponding
`binarize=False` result for every PSM tested (consistent with Round 1).
No other tested PSM value (3, 4, 11, 12) recovered the label line better
than PSM 6 did. **Decision: `page_segmentation_mode=6`,
`binarize=False`.**

In every one of the three cases above, Tesseract's own PSM-6 recognition
of the FIR-label line was close but not exact — `No`/`No.` was misread as
a short, different word (`Nex`, `Ma`, `Not`), while the actual identifier
(`91/2026`, `20/2026`, `30/2026`) was read correctly. Since the raw text
already contained the real identifier, the second half of the fix was in
`document/fir_report.py`'s `_FIR_REFERENCE` regex, not in OCR
configuration: the literal `FIR` label is still required, but the token
between `FIR` and the identifier now also accepts a short (≤6 letters)
OCR-garbled stand-in for `No`/`Number`, and the identifier itself is
required (via lookahead) to actually contain a digit — the same structural
requirement every real FIR reference in this project's fixtures already
satisfies (`45/2026`, `TEST/2026/001`, `SECRET/9999/999`). This means the
matcher still only ever emits an identifier that is genuinely present in
the OCR text next to a genuine `FIR` mention; it does not invent, guess,
or hard-code any value. See
`tests/unit/structured_processing/test_fir_report.py::
test_fir_reference_tolerates_an_ocr_garbled_label` (positive cases) and
`test_fir_reference_garbled_label_tolerance_does_not_invent_a_match`
(negative case: an unrelated digit-bearing identifier elsewhere in a
sentence that also contains "FIR" is never emitted).

**Confirmed on the real Gate B MacBook.** Aditya re-ran the focused
structured-processing OCR tests and then the complete test suite on the
actual Gate B machine (macOS, Python 3.12.7, Tesseract 5.5.3, pytesseract
0.3.13, pypdfium2 5.13.0) with `page_segmentation_mode=6` and
`binarize=False` in place and the tolerant FIR-label regex applied — both
passed. Exact command and full-suite counts are recorded in
`docs/qa/test-results.md`'s dated Gate B confirmation entry. This confirms
the three fixtures this task measured (`91/2026`, `20/2026`, `30/2026`)
recover correctly on that machine with this configuration; it does not
claim that every future document type, layout, or real police evidence
will OCR at this accuracy — see `docs/qa/known-limitations.md` for what
remains unverified (a real benchmark dataset, real-world field-quality
measurement).

## Layout/text normalization

`app/modules/structured_processing/document/normalization.py`.

Regex/NER/relation extraction all run against *normalized* text, not raw
embedded/OCR text, so a pattern anchored on an explicit label still
matches across incidental whitespace/line-wrap noise:

1. Unicode NFC normalization.
2. Line-wrap dehyphenation (`"Ram-\nesh"` → `"Ramesh"`; a hyphen before an
   uppercase letter or digit is left alone — more likely a genuine
   hyphenated identifier than a wrapped word).
3. Whitespace collapsing (any run of whitespace → one space).

Every normalized character carries an exact offset back to the original
text (`OffsetMap`) — a regex/NER match's span is always remapped through
this map before becoming a `SourceLocator`, so provenance is never lost to
normalization, and (for an OCR'd page) the remapped span is further
resolved to the exact OCR region bounding box(es) it came from
(`locate_bbox_for_span`).

## Document identifier extraction: two layers

### 1. High-confidence regex (`document/fir_report.py`, Phase 1, unchanged)

Fixed patterns for FIR numbers, phone numbers (Indian mobile only), email
addresses, dates, currency amounts, transaction references,
account/UPI-shaped identifiers, and legal section references. Confidence
`0.95` — an exact, explicit deterministic match, always.

### 2. Local NER (`document/ner.py`, `ner_fallback.py`, `ner_spacy.py`)

Two adapters behind one `NerAdapter` protocol, emitting `PERSON`,
`ORGANIZATION`, `LOCATION` mentions only (`DATE` is deliberately excluded —
the regex layer already covers dates exactly; a second, statistical date
extractor would only produce redundant, lower-confidence duplicates):

- **`DeterministicNerAdapter`** (confidence `0.50`) — always available, no
  ML model: Title-Case-sequence heuristics for `PERSON`, a documented
  organizational-suffix list for `ORGANIZATION` (including short all-caps
  acronyms like `HDFC`/`ICICI`), and a small fixed gazetteer of common
  Indian city/state names for `LOCATION`. This is what every unit test
  runs against, and the automatic runtime fallback if the real model
  below isn't bootstrapped.
- **`SpacyNerAdapter`** (confidence `0.75`) — spaCy's small English
  pipeline (`en_core_web_sm`, version `3.8.0`, MIT-licensed), loaded
  **only** from an operator-bootstrapped local directory — never
  downloaded automatically at worker runtime.

Neither adapter's confidence is a native per-entity model probability —
`en_core_web_sm`'s NER component doesn't expose one through spaCy's
standard pipeline output. Both are fixed, documented extraction-quality
tiers, stated honestly rather than fabricating false per-entity precision.

### Local NER bootstrap (never automatic)

    uv run python -m app.modules.structured_processing.bootstrap_ner_model

Downloads the pinned wheel
(`en_core_web_sm-3.8.0-py3-none-any.whl`, from
`github.com/explosion/spacy-models`' `en_core_web_sm-3.8.0` release),
verifies it against a pinned SHA-256, then **extracts** (never
`pip install`s) the model directory it contains to
`models/nlp/en_core_web_sm` (`Settings.ner_model_path`) — the wheel is
just a ZIP archive whose payload is a plain, `spacy.load()`-able directory,
so no package installation step of any kind is ever needed or run. Mirrors
`media_processing.bootstrap_models`' identical "explicit, operator-invoked,
checksum-verified, never automatic" posture. Never re-downloads an
already-valid model unless `--force` is passed.

**Offline/graceful-degradation behavior**: `worker.py`'s
`_build_ner_adapter` tries `SpacyNerAdapter` first; if the model directory
is missing or fails to load, it falls back to `DeterministicNerAdapter`
automatically — a missing NER model asset degrades quality, it never
fails or crashes a job (the same posture `media_processing.worker.
_build_analysis_components` already established for a missing
detector/OCR model).

### Honest accuracy note

`en_core_web_sm` is a small, general-purpose English model, not fine-tuned
for Indian names, places, or investigative-document vocabulary. It will
miss some real mentions (an unfamiliar Indian given name may be tagged
incorrectly or missed entirely) and occasionally produce a false positive
on an unrelated capitalized phrase. `docs/qa/test-results.md` records the
actual measured field-match precision/recall from a real, labelled
synthetic fixture — not an assumed or industry-average figure.

## Rule-based relation/event extraction

`app/modules/structured_processing/document/relations.py`. Four
transparent, versioned, deterministic rules look for two or more
already-extracted mentions within `PROXIMITY_MAX_CHARS` (200) characters
of each other on the same page (or the same single segment, for DOCX/TXT):

- `person_contact_proximity_v1` → `person_contact_association`
- `dated_communication_reference_v1` → `dated_communication_reference`
- `transaction_claim_v1` → `transaction_claim`
- `incident_event_v1` (a page with an FIR/police-station marker, a date,
  and at least one participant or location) → `incident_event_mention`

Every relation observation cites the exact combined source span of its
constituent mentions, at confidence `0.60` (an inference from proximity,
weaker than either constituent mention's own confidence — see
`ner.py`/`fir_report.py`'s own confidence tiers). **None of this is
identity resolution, entity merging, or a graph relationship** — every
observation produced here is exactly as reviewable and provisional as any
other `ObservationV1`. No rule ever asserts guilt, criminal-network
membership, or any certainty beyond "these source-extracted facts
co-occur in this document" (see `CLAUDE.md`'s "No automatic identity
merge, guilt conclusion" rule, and `phase-3-decisions.md`).

## CDR/finance header alias and schema-mapping policy

`app/modules/structured_processing/structured/{cdr,finance,profiles}.py`.
Both profiles use explicit, versioned header-alias maps (`ParserProfile.
field_aliases`) resolved case/spacing-insensitively — never silently
guessed. `chunked_processing.assess_schema` checks the header **once**,
before any row is read: if none of a required field's aliases are present,
the whole job fails immediately with `ambiguous_schema`, naming only the
missing canonical field names and the actual column headers present (safe
schema metadata, never a row value).

CDR aliases: `caller_number`, `callee_number`, `timestamp`,
`source_timezone`, `duration_seconds`, `call_type`, `cell_tower_id`,
`imei`, `imsi`. Required: `caller_number`, `timestamp`.

Finance aliases: `transaction_id`, `timestamp`, `source_timezone`,
`sender_account`, `receiver_account`, `amount`, `currency`, `direction`,
`reference`, `channel`, `status`, `balance`, `counterparty`. Required:
`amount`, `currency`.

## Canonical-value normalization decisions

- **Phone numbers**: normalized to **E.164** (`+91XXXXXXXXXX`) only for an
  unambiguous 10-digit Indian mobile number (the only country context this
  codebase has ever established — see `fir_report.py`'s identical Indian-
  mobile-only regex). The original value is always kept alongside
  (`caller_number_raw`/`callee_number_raw`), never discarded.
- **Timestamps** (both CDR and finance): resolved via
  `cdr.parse_record_timestamp` —
  1. an explicit per-record `source_timezone` field (an IANA name or a
     fixed `+HH:MM` offset), if present;
  2. otherwise `Settings.structured_default_timezone` (default
     `Asia/Kolkata`) — a fixed, documented default, never silently
     assumed to be UTC.

  The canonical `timestamp` attribute is always the UTC instant computed
  from the resolved zone; `timestamp_source_timezone` and
  `timestamp_source_utc_offset` record which zone/offset was actually
  used, and `timestamp_raw` keeps the untouched original string. A
  timestamp matching none of the documented formats is rejected
  (`required_field_missing`) rather than guessed at. For finance,
  `timestamp` is **optional** — an unparseable value is preserved raw
  without failing the record (only `amount`/`currency` are required).
- **Duration**: kept in **seconds** (a `float`) — already the documented
  canonical unit from Phase 1, unchanged; not converted to milliseconds
  (the task's own wording allows "milliseconds or another documented
  canonical unit consistent with existing contracts").
- **Money**: always `decimal.Decimal`, never `float` — unchanged from
  Phase 1. `amount`/`amount_raw` both kept.
- **Currency**: uppercased; `currency_is_known_iso4217` is a safe,
  informational flag against a curated (intentionally non-exhaustive) set
  of ~30 common ISO 4217 codes — a well-formed but unlisted code is
  **never rejected**, only flagged `False` (this module validates format,
  it does not maintain the authoritative currency-code registry).
- **Debit/credit direction**: a documented, case-insensitive vocabulary
  (`debit`/`dr`/`d` → `"debit"`; `credit`/`cr`/`c` → `"credit"`); a value
  outside this set is preserved only as `direction_raw`, never guessed.

## Malformed-row policy (CDR and finance)

`app/modules/structured_processing/structured/chunked_processing.py`.

- **Schema-level** (a required field's aliases are entirely absent from
  the header): the whole file is rejected immediately, before any row is
  read — `ambiguous_schema`.
- **Row-level** (a specific row is missing a required field, or its
  amount/timestamp doesn't parse): that one row is reported safely
  (`MalformedRow`: row index/sheet/reason — a field-name-only reason,
  never the offending raw value) and normalization continues with the
  remaining rows in the same chunk — never aborts the chunk or the file.
- **Terminal reporting**: if at least one row succeeded, the job is
  `SUCCEEDED` with a `checkpoint` naming the total
  `valid_row_count`/`malformed_row_count` whenever the malformed count is
  non-zero. If **every** row failed, the job is `FAILED` with the safe
  `partial_row_failures` error code.

## Vectorized, bounded-memory batch processing

`app/modules/structured_processing/structured/chunked_processing.py`,
`Settings.structured_batch_size` (default 500 rows per micro-batch).

- **CSV**: `polars.scan_csv(...).collect_batches(...)` — a true lazy,
  chunked reader; rows past the current chunk are never touched until the
  next chunk is requested.
- **XLSX**: `openpyxl`'s `read_only` row iterator (already a lazy,
  streaming reader) is consumed in bounded groups, each assembled into a
  `pyarrow.Table` before being handed back out as records — genuinely
  vectorized batch handling, even though (honestly — neither Polars nor
  PyArrow has a native chunked-XLSX reader without an extra optional
  engine this project does not depend on) the disk-level read remains
  row-by-row via `openpyxl`.
- **JSON**: a JSON array has no streaming-friendly structure, so the whole
  (size-bounded) array is parsed at once and only the *output* is chunked
  into batch-submission-sized groups.

Documents are batched **one page (or one DOCX/TXT segment) per
micro-batch** — a natural, simple chunking unit that also matches the
`incident_event_v1` relation rule's own page-scoping.

## Producer rules for Nipun's batch-ingestion API

`app/modules/structured_processing/batching.py` builds every
`ObservationBatchSubmissionV1`/`TransformationProvenanceV1`/
`ObservationBatchProgressV1` this phase submits:

- `batch_id = f"batch-{batch_sequence:08d}"`, `idempotency_key =
  sha256({job_id, batch_sequence})` — both **deterministic** functions of
  `(job_id, batch_sequence)`, so retrying the exact same logical batch
  (e.g. after a transport failure) always produces the exact same two
  tokens; the server's own idempotent-replay rule (same `batch_id` →
  replay, never a duplicate) works correctly with no retry state of this
  module's own to track.
- Every batch carries at least one of: real observations, transformation
  provenance for the step(s) that ran, and a progress update — never an
  empty submission (Nipun's contract already rejects one; every code path
  here structurally always attaches at least a progress update).
- `worker.py` submits every accepted batch's observations through
  `client.submit_batch`, then submits exactly **one** terminal
  `WorkerResultV1` (via the pre-existing `/result` endpoint, unchanged)
  with `observations=[]` — the observations were already delivered. This
  is not a new terminal-result shape; it is the exact "then completes with
  one terminal `WorkerResultV1` exactly as before" pattern
  `docs/architecture/phase-3-decisions.md` (Nipun's own section) already
  documents for any batch-submitting worker.
- `run_structured_batches_job` calls `client.renew_lease` every 5 batches
  as a best-effort heartbeat (a failure to renew is logged but never fails
  the job) — reusing Aditya's existing `/renew` endpoint unchanged, for a
  CDR/finance file large enough that its lease could otherwise expire
  mid-processing.
- `generic_tabular_v1`/`generic_json_v1` (Nipun's own fallback profiles)
  are **unchanged** — `process_job` still runs synchronously and submits
  one full terminal result directly, exactly as every phase before this
  one. Only `fir_report_text_v1`/`cdr_generic_v1`/
  `financial_transaction_generic_v1` (Jasraj's own profiles) route through
  the new batch orchestration.

## What this phase explicitly does not do

- Never converts a mention into `EntityV1` or performs identity merging —
  every `ExtractedEntityMention`/relation observation remains raw,
  unresolved, and reviewable.
- Never claims an account holder's identity, criminality, or relationship
  from a financial transaction row, or a person's guilt from a document
  mention or relation observation.
- Never writes directly to Neo4j, the graph-projection outbox, PostgreSQL,
  or MinIO — every observation reaches the graph exclusively through
  `app/modules/graph/`'s existing, unmodified outbox/projector (see
  `docs/architecture/graph-projection.md`).
- Never requires PostgreSQL, Neo4j, Redis, or MinIO credentials in the
  worker process — only the API base URL, a worker credential, and the
  per-job claim token received through the normal claim flow.

## QA test IDs owned by Jasraj (Phase 3)

See `docs/qa/test-matrix.md` for the full rows:
`DOC-PAGE-TRUST-001`, `DOC-OCR-001`, `DOC-NORMALIZATION-001`,
`DOC-NER-001`, `DOC-RELATIONS-001`, `DOC-OCR-PRECISION-001`,
`CDR-CHUNKED-001`, `FINANCE-CHUNKED-001`, `WORKER-DOC-BATCH-001`,
`WORKER-STRUCTURED-BATCH-001`, `WORKER-CLIENT-BATCH-001`,
`WORKER-LIVE-BATCH-001`.

## Phase 7 Part 2: structured-data and local OCR benchmarking (Jasraj)

Status: **in progress** — see
`docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part 2"
section for the full design, `docs/decisions/ADR-016-phase-7-evaluation-
and-model-selection.md` for the frozen evaluation rules this benchmark
layer must respect, and `docs/qa/known-limitations.md`'s "Phase 7 Part 2"
section for what remains genuinely unverified until Aditya's MacBook
pre-flight.

Five new, purely additive modules under `app/modules/structured_processing/`
— `benchmark_metrics.py`, `benchmark_validation.py`, `benchmark_adapters.py`,
`benchmark.py`, `benchmark_cli.py` — measure this phase's *existing*
production code (`document/fir_report.py`, `structured/chunked_processing.py`,
`structured/cdr.py`, `structured/finance.py`) against the three datasets
this task owns in Phase 7 Part 1's frozen manifest: `fir_icdar_2023`
(document/FIR OCR), `gomask_voice_cdr` (CDR), `ibm_amlsim` (finance). None
of Parts 1–6's production processors are modified — every benchmark
adapter calls the same functions `worker.py` already calls in production,
never a parallel extraction path.

- **OCR** (`fir_icdar_2023`): an injectable `OcrEngine` protocol lets a
  `FakeOcrEngine` (unit tests) or a real PaddleOCR-backed
  `ConfiguredOcrEngine` (real runs, wired by the caller — this module
  never imports `paddleocr` itself) stand in for either approved
  candidate (`paddleocr-ppocrv5-mobile`/`-server`). Character/word error
  rate is computed only when a sample carries reference text; field
  extraction precision/recall/F1 reuses the *exact* existing
  `document.fir_report.extract_fir_mentions` regex extractor over the
  OCR'd text, never a bespoke benchmark-only extraction rule — so a
  benchmark's field-extraction score genuinely measures "how well does
  this OCR candidate preserve what the real FIR pipeline already depends
  on," not an artificial proxy task.
- **CDR/finance** (`gomask_voice_cdr`/`ibm_amlsim`): the one candidate,
  `existing-deterministic-parsers`, wraps
  `structured.chunked_processing.assess_schema`/`normalize_chunk` (the
  *same* functions `worker.run_structured_batches_job` calls in
  production) to get genuine per-row accept/reject accounting — a
  malformed row is safely categorized by its `ProcessingError.code`
  (a small fixed vocabulary) and never inflates the accepted count,
  mirroring production's own "zero valid rows despite malformed ones is a
  FAILED result, never a fabricated SUCCEEDED" policy exactly.
- **Local-only, safe-by-construction**: every local filesystem root
  (`TRACEX_BENCHMARK_DATA_ROOT`/`TRACEX_MODEL_CACHE_ROOT`/
  `TRACEX_BENCHMARK_OUTPUT_ROOT`) comes from an explicit environment
  variable or CLI flag, never a hardcoded path; a missing dataset/model
  artifact produces a truthful `BenchmarkRunStatus.UNAVAILABLE` result,
  never a fabricated success; every result is Phase 7 Part 1's own frozen
  `BenchmarkRunV1` (no parallel result contract), whose `metrics: dict[str,
  float | int | None]` type constraint makes a raw string value in a
  metric structurally impossible; `benchmark_validation.
  reject_private_local_paths` additionally scans every free-text field for
  an absolute/home-relative path before a result is written to disk.
- **No dataset/model was downloaded, and no candidate is selected** —
  every real run this session could attempt reports `UNAVAILABLE` (no
  local FIR ICDAR/GoMask/AMLSim data or PaddleOCR installation exists on
  this development machine, by design — see this task's "do not download"
  rule). Aditya's MacBook pre-flight is where a real `SUCCEEDED`/`FAILED`
  result first becomes possible; Gate C (not this task) selects a winner.

See `docs/runbooks/local-development.md`'s "Phase 7 Part 2 benchmark CLI"
and "Phase 7 Part 2 MacBook validation handoff" sections for the exact
commands and pre-flight checklist.

## QA test IDs owned by Jasraj (Phase 7 Part 2)

See `docs/qa/test-matrix.md` for the full rows: `BENCH-VALIDATION-001`,
`BENCH-OCR-001`, `BENCH-STRUCTURED-001`, `BENCH-CLI-001`,
`BENCH-SAFETY-001`, `BENCH-SMOKE-001`.
