# Document and Structured-Data Processing (Jasraj Phase 1)

`app/modules/structured_processing/` is the first source-processing layer: it turns local FIR/report documents (PDF/DOCX/TXT) and structured CDR/financial/generic data (CSV/XLSX/JSON) into canonical `ObservationV1` objects and safe `WorkerResultV1` results. Full field-level profile reference lives in `docs/architecture/parser-profiles-v1.md`. Design rationale for the provenance/determinism policy lives in `docs/decisions/ADR-002-deterministic-source-processing-and-provenance.md`.

## Why workers return `ObservationV1`, not database writes

`worker.process_job(job, evidence, resolver)` is a pure function from `(WorkerJobV1, EvidenceRecordV1, SourceResolver)` to `WorkerResultV1`. It never imports `sqlalchemy`, `asyncpg`, `neo4j`, `redis`, or `minio` (enforced by `tests/unit/structured_processing/test_safety.py::test_no_infrastructure_client_is_imported`, which statically parses every module's imports). This mirrors the same boundary `app/modules/graph/` sits behind on the write side: a worker's job is to read bytes and emit canonical facts; persisting those facts into PostgreSQL/Neo4j is an orchestrator's job in a later phase. This keeps the module trivially testable (every test in this module is a pure in-memory unit test) and keeps worker credentials scoped to nothing at all — a worker that's compromised or buggy cannot reach a database because it was never given the means to.

## Why extracted mentions are never automatically resolved to identities

Every observation this module emits carries `extracted_entities` (raw, unresolved mentions — a phone number as literally written, an account number as literally written) via `ObservationV1.extracted_entities`, never an `EntityV1`. `tests/unit/structured_processing/test_safety.py::test_no_entity_or_event_contract_is_constructed` statically asserts no file in this module even imports `EntityV1`/`EventV1`. Two different CDR rows mentioning the same phone number produce two independent `cdr_device_identifier_mention` observations — this module makes no claim that they refer to the same subscriber. Entity resolution (deciding two mentions are "the same" identity) is an explicit, reviewable, later-phase operation per `CLAUDE.md` — never something inferred here.

## The `SourceResolver` boundary

```python
class SourceResolver(Protocol):
    def read_bytes(self, object_uri: str) -> bytes: ...
```

`worker.process_job` takes bytes only through this protocol — it has no idea whether they came from a local file, an in-memory fixture, or (in a later evidence-lifecycle phase) MinIO. `models.StaticBytesResolver` is the trivial in-memory implementation tests use; `tests/integration/structured_processing/test_local_file_pipeline.py` additionally exercises a small local-file-backed resolver, demonstrating the "read from disk" shape a later-phase resolver would take. Nothing in this module knows how to talk to MinIO, and it doesn't need to.

## Classification: content-type + extension, cross-checked

`document/classifier.classify(content_type, filename)` accepts the six Phase 1 formats (`application/pdf`, DOCX's OOXML MIME type, `text/plain`, `text/csv`, `application/json`, XLSX's OOXML MIME type). Neither signal is trusted alone: a renamed file (real PDF bytes given a `.txt` name) is rejected as `unsupported_extension`, and a content type outside the six is rejected as `unsupported_content_type`. `worker.process_job` classifies *before* resolving the parser profile, so a fundamentally unsupported format gets the more specific `unsupported_content_type` error rather than a profile-mismatch error.

## Document extraction and OCR routing

- **PDF** (`document/pdf.py`, via `pypdf`): pages are processed independently. Encrypted/password-protected PDFs are rejected immediately as `encrypted_pdf_unsupported` — no password, not even an empty one, is ever attempted (that would still be password guessing). A page's embedded text is checked against a fixed "meaningful text" threshold (`document/text_extractors.MEANINGFUL_TEXT_MIN_CHARS = 20`, a documented heuristic, not tuned against a real corpus); a page under that threshold is *never* given fabricated text — it's recorded in `ocr_required_pages` instead.
- **OCR routing** (`document/ocr_routing.py`): if every page needs OCR, the whole document is `DEFERRED` with `observations=[]` and a small deterministic JSON checkpoint (`{"reason": "document_requires_ocr", "deferred_pages": [...], "total_pages": N}`). If only some pages do, the extractable pages are processed normally (full observations, full provenance) and the result is still `DEFERRED` overall — because the evidence isn't *fully* processed, a downstream OCR worker (later phase) still has work to do. A real OCR engine is **not** implemented in this phase (see CLAUDE.md non-goals) — this module only ever makes the *routing decision*.
- **DOCX** (`document/docx.py`, via `python-docx`): paragraphs and text-bearing table cells are concatenated into a single deterministic text stream (paragraphs first, then tables — see the known-limitation about visual document order). No macros or external links are ever executed; `python-docx` only reads document XML.
- **TXT** (`document/txt.py`): `utf-8-sig` first (handles a BOM transparently), falling back to `latin-1` (a total function over all byte values, so TXT decoding never itself raises) if that fails. This is the complete, documented encoding policy — no third fallback exists.
- **FIR/report extraction** (`document/fir_report.py`): every PDF/DOCX/TXT text stream (whatever its source) is scanned by a single fixed set of regexes requiring either an explicit label (`FIR No.`, `Police Station`, `Section`, `A/C No.`) or a structurally-distinctive format (an email address, an Indian vehicle plate, a currency-symbol-prefixed amount, a recognized UPI handle). No NLP model, no LLM, no fuzzy matching. A value with no such signal is never captured.

## Structured-data safety

CSV (stdlib `csv`), XLSX (`openpyxl`, `data_only=False`), and JSON (stdlib `json`) parsing all enforce the limits in `limits.py`: input byte size, row/column/cell-text counts, JSON nesting depth and node count, and — for DOCX/XLSX's ZIP containers — entry count, total uncompressed size, and per-entry compression ratio (`limits.validate_zip_container`, checked *before* the bytes ever reach `python-docx`/`openpyxl`). XLSX formula cells are read as their formula *string* (`"=SUM(A1:A2)"`), never evaluated — `openpyxl` doesn't execute formulas either way, but `data_only=False` also avoids silently trusting a workbook's last-cached computed value.

## CDR and financial normalization

Both `structured/cdr.py` (`cdr_generic_v1`) and `structured/finance.py` (`financial_transaction_generic_v1`) operate on `models.RawRecord` — a shape produced identically by the CSV, XLSX, and JSON-array parsers, so the same normalization code handles all three source formats. Header aliases are matched case- and separator-insensitively (`models.normalize_header`: `"Caller Number"`, `"caller-number"`, and `"caller_number"` all resolve to the canonical `caller_number` alias) against the explicit alias lists in `structured/profiles.py`.

- **Phone numbers** are normalized only when the result is an unambiguous 10-digit Indian mobile number after stripping a `+91`/leading-`0` prefix; anything else is kept exactly as written (`docs/decisions/ADR-002...md` explains why "keep the original when uncertain" beats a wrong normalization).
- **Timestamps** are parsed only against a fixed, documented format list (`cdr._TIMESTAMP_FORMATS`); a value matching none of them is `required_field_missing`, not a guess.
- **Money** is never passed through `float`. `finance._normalize_amount` uses `decimal.Decimal`, and the original source string is always preserved alongside the normalized value (`amount` next to `amount_raw`). Currency is required and never invented — a record without one fails safely.

## Deterministic confidence policy

Every confidence value is a fixed constant (`provenance.py`), never a model output:

| Value | Meaning |
|---|---|
| `1.00` | A complete structured record read directly from a validated row/object (`cdr_call_record`, `financial_transaction_record`, `tabular_record`, `json_scalar_value`, `amount_mention`). |
| `0.95` | An exact deterministic regex match in trustworthy embedded text (every `fir_report_text_v1` mention type). |
| `0.90` | A structured field read directly, but normalization was withheld as uncertain and the original value retained (CDR device/subscriber/tower mentions, financial account/reference mentions). |

`extraction_confidence` describes extraction quality only — never a probability of guilt or culpability, per `CLAUDE.md`. Confidence is never lowered or raised based on the *content* of a value (e.g. a suspicious-looking phone number) — only based on how directly/unambiguously it was read.

## Safe errors and logging

`errors.ProcessingError` is the only exception this module raises for an expected failure mode; `worker.process_job` catches exactly this type and converts it to `WorkerResultV1(status=FAILED, error=WorkerError(code=..., message=...))`. Error messages describe *what kind* of problem occurred (a field name, a row index, a byte-limit constant) — never the offending value itself. `tests/unit/structured_processing/test_safety.py` verifies this behaviorally: a CDR record with a real-looking phone number and an unparseable timestamp fails without that phone number appearing anywhere in the error. Anything that isn't a `ProcessingError` (a genuine bug) is deliberately allowed to propagate rather than being silently repackaged as a plausible-looking failure.

## Deferred to a later phase

Real OCR (Tesseract/cloud OCR/any OCR model), NER/NLP/LLM-based extraction, entity resolution, graph projection/cross-linking, actual MinIO reads (a real `SourceResolver` implementation), and any ML/embeddings work are all explicitly out of scope for this phase — see `CLAUDE.md` and `docs/qa/known-limitations.md`.
