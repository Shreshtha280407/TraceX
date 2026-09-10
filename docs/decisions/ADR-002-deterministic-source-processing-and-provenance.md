# ADR-002: Deterministic Source Processing and Provenance

- Status: Accepted (Jasraj Phase 1 document/structured processing)
- Owner: Jasraj
- Date: 2026-09-10

## Context

`app/modules/structured_processing/` turns FIR/report documents and CDR/financial/generic structured data into canonical `ObservationV1` objects. Several decisions had no single obviously-correct answer and are recorded here so later contributors (and whoever builds real OCR/NER/entity-resolution on top of this) know what was deliberate.

## Decision 1: Observation IDs are derived from the full canonical `SourceLocator`, not a synthetic counter

**Decision**: `provenance.observation_id` hashes `(case_id, evidence_id, profile.name, profile.version, observation_type, canonical_bytes(locator))` via the project's existing `deterministic_uuid` helper — every field of the `SourceLocator`, serialized deterministically, not just e.g. `row` or `page` alone.

**Why**: Reprocessing the same evidence (a re-run, a retry, an idempotent worker replay) must produce the exact same observation IDs, so a consumer can safely deduplicate. Using the *whole* locator (not a subset) means two observations at genuinely different source positions can never collide even if they share one locator field (e.g. two mentions on the same PDF page but different spans); using `canonical_bytes` (already built for exactly this kind of stable serialization, see `app/core/canonical.py`) rather than hand-rolling a hash keeps this consistent with how the rest of the project derives stable identifiers, instead of reinventing that logic here. Profile *version* is part of the input deliberately: bumping `cdr_generic_v1` from `1.0.0` to `1.1.0` because normalization logic changed should produce different IDs for the same locator, since the extracted *meaning* may have changed even though the source position didn't.

## Decision 2: `RawMention`/`RawRecord` are shared across every profile, not per-profile types

**Decision**: `models.RawMention` (one extracted fact: type, text, locator, confidence, optional entity hint, attributes) and `models.RawRecord` (one CSV/XLSX/JSON-array record: index, raw header→value map, a `locator_for(field_name)` closure) are used by `document/fir_report.py`, `structured/cdr.py`, `structured/finance.py`, and the generic tabular/JSON profiles alike, converted to `ObservationV1` by one shared `provenance.mention_to_observation`.

**Why**: Every profile's extraction logic ends at the same place — "here is a small piece of text/value, here is exactly where it came from, here is how confident we are it was read correctly" — regardless of whether the source was a PDF page, a CSV row, an XLSX cell, or a JSON path. Giving each profile its own conversion-to-`ObservationV1` logic would duplicate the deterministic-ID derivation, the `ExtractedEntityMention` construction, and the `Extractor` provenance block five times, with five chances to drift out of sync with the frozen `ObservationV1` contract. `RawRecord.locator_for` is a closure specifically so `structured/cdr.py`/`structured/finance.py` can stay completely unaware of which underlying file format (CSV/XLSX/JSON) produced the record — `csv_parser.py`/`xlsx_parser.py`/`json_parser.py` are the only three places that know how to build a `row`/`sheet`+`row`+`column`/`json_path` locator.

## Decision 3: Ambiguous normalization always keeps the original value, never a best guess

**Decision**: `cdr._normalize_phone` only rewrites a value when stripping separators/prefix yields an unambiguous 10-digit Indian mobile number; anything else is returned unchanged. `finance._normalize_amount` only produces a value when the cleaned string parses as a `Decimal`; a currency is only accepted, never invented, when explicitly present.

**Why**: A confidently-wrong normalization (e.g. mangling a landline number into something that looks like a mobile number) is worse than an unnormalized value, because it silently degrades evidence integrity — a human reviewer trusting the normalized field would be trusting a fabrication. "Keep the original when uncertain, normalize only when unambiguous" is the same posture as `extraction_confidence` describing quality rather than compensating for it with a guess. This is why `duration_seconds_raw`/`amount_raw`/`currency_raw` are *always* preserved alongside any normalized counterpart, even when normalization succeeds — the source value stays independently verifiable.

## Decision 4: Money is `Decimal`, never `float`, and the raw string always survives

**Decision**: `finance._normalize_amount` returns `decimal.Decimal | None`; the normalized value is stored as `str(Decimal(...))` (an exact decimal string) in `ObservationV1.attributes`, never as a Python `float` or a JSON number that would round-trip through binary floating point.

**Why**: `float` cannot represent most base-10 monetary values exactly (`0.1 + 0.2 != 0.3` in IEEE 754 binary). For evidence that may later support a financial claim in an investigation, an amount that silently drifted by a fraction of a currency unit through float conversion is unacceptable. `pydantic`'s `JsonValue` (used for `ObservationV1.attributes`) has no native `Decimal` support and would coerce one to `float` on JSON serialization regardless of what Python type is stored — so the fix has to be "store the decimal as an exact string," not "store a `Decimal` object and hope."

## Decision 5: A partially-scanned PDF is `DEFERRED` overall, even though some pages produced real observations

**Decision**: `worker._process_pdf` sets `status=DEFERRED` (not `SUCCEEDED`) whenever *any* page needs OCR, even if other pages on the same document were fully extracted and their observations are included in the same result.

**Why**: `WorkerResultV1.status` is a single value describing the whole job, and `SUCCEEDED` should mean "this evidence is fully processed" — not "we got some of it." A downstream orchestrator deciding whether a later-phase OCR worker still has work to do on this evidence needs to be able to trust `status != SUCCEEDED` as the signal, without having to separately inspect `checkpoint` on every nominally-successful result to check for missed pages. The extracted pages' observations are still returned in full (nothing is thrown away just because the overall job is incomplete) — `DEFERRED` with non-empty `observations` is a valid `WorkerResultV1` shape precisely for this case.

## Decision 6: Parser profile is selected explicitly by `job.processor_name`, cross-checked against content type — never inferred from data shape

**Decision**: `worker.process_job` never tries to guess "this CSV looks like CDR data" from column names. `job.processor_name` must name one of the profiles in `structured/profiles.py` exactly; `worker.process_job` then verifies `evidence.content_type` is one that profile accepts, failing safely as `unsupported_parser_profile` on any mismatch.

**Why**: Guessing the profile from data shape would mean the same CSV could be silently reinterpreted differently across two runs if its header naming happened to overlap two profiles' alias lists (e.g. a generic tabular export that happens to have a column named `amount`). An investigator needs `WorkerJobV1.processor_name` to be a reliable, auditable record of *which* normalization rules were actually applied to produce a given observation — not an implementation detail the worker decided for itself.

## Open questions for team review

- DOCX extraction concatenates paragraphs, then tables, in that order — not true interleaved visual document order (a table embedded mid-document appears at the end of the stream, not inline). This is fully deterministic but not layout-faithful; revisit if a later phase needs exact positional fidelity for legal chain-of-custody purposes.
- `fir_report_text_v1`'s `date_time_mention` captures raw matched text only, with no DD/MM vs MM/DD resolution — a value like `03/04/2026` is never resolved to a specific calendar date, only recorded as seen. A later phase that needs an actual parsed `event_time` for a FIR-mentioned date will need to add that resolution logic (and decide the ambiguity policy) itself.
- The Indian-mobile/Indian-vehicle-plate/curated-UPI-handle patterns in `document/fir_report.py` are intentionally narrow (documented in `docs/qa/known-limitations.md`). Broadening them to other formats (landlines, non-Indian plates, more UPI handles) is a straightforward but unscoped follow-up.
