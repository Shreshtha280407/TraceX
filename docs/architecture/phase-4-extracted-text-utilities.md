# Phase 4 extracted-text utilities

Owner: Jasraj. Status: **in progress**.

## Boundary

`app.modules.extracted_text.ocr_postprocessing` is a pure, typed utility for
one OCR fragment already produced from a full frame or selected ROI. It does
not run OCR, decode media, select a region, contact a service, or write to
PostgreSQL, Neo4j, Redis, MinIO, an outbox, or an API.

The input carries case/evidence scope, OCR engine/version/configuration,
optional confidence, `SourceLocator`, optional observation/chunk/manifest/
artifact UUIDs, and optional upstream token/line box ranges. It deliberately
accepts no object URI, media bytes, credentials, or raw engine payload.

## Text semantics

The result retains three deliberately different values:

- `raw_text` is unchanged input and remains source content. It is never graph
  property or safe transformation metadata.
- `cleaned_display_text` applies documented Unicode NFC, unsafe/invisible
  control removal, line-preserving horizontal whitespace cleanup, and optional
  line-edge trimming. It never joins OCR lines or performs a linguistic edit.
- `normalized_matching_text` is case-folded, line-collapsed matching material;
  optional punctuation separator cleanup is marked matching-only. It is not an
  exact transcription.

Each output declares ordered transformation names/versions and a canonical
configuration hash over the profile and utility version. The same input/profile
always produces the same text, IDs, warnings, and hash. Surrogates/control
characters are safely removed from cleaned/matching material with a warning;
the raw input is retained unchanged.

## Identifier candidates

Supported deterministic rules are intentionally narrow: India-style
plate-like structure (not registration validation), Indian mobile numbers,
email addresses, explicitly labelled account/reference numbers, and `@handle`
mentions only when the producer supplied a platform context. A candidate has
the exact raw span, untouched raw matched source slice, matching form, rule
version, deterministic UUID, same-case evidence scope, supplied locator and
line/token-box overlap, optional frame/time/bbox through that locator, and
chunk/manifest/artifact IDs where supplied.

Candidates are unresolved extracted claims, not entities, identities,
relationships, or graph writes. OCR-confusable plate characters are flagged;
they are never corrected. Duplicate output uses type plus exact raw source span
as its key, so replay is deterministic while distinct repeated source spans
remain distinct evidence.

## Gaurav integration

After OCR yields an `OcrBoxResult`, Gaurav builds an `OcrFragment` in memory,
copying the existing normalized bbox and image/video `SourceLocator` (including
frame/time where available), engine/configuration metadata, and Nipun lineage
UUIDs when processing a staged chunk. The worker may create one or more normal
canonical `ObservationV1` records and a `TransformationProvenanceV1` using the
utility configuration hash; it must never put `raw_text` in graph-facing
attributes or `safe_metadata`. It submits only through Nipun's ordinary
idempotent batch/chunk publication path. Shreshtha's mapper then sees only the
canonically persisted observation/locator/provenance and remains unchanged.

No timestamp, bounding box, frame, identity, identifier correction, or
artifact lineage is invented here. Actual OCR, GPU/media, LAN, and real-dataset
validation are intentionally deferred.
