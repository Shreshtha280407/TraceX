# ADR-010: conservative OCR post-processing remains a pure provenance utility

## Decision

TraceX uses a new typed, pure OCR-fragment post-processing package rather than
adding an OCR engine, a database model, or graph projection vocabulary. Raw,
cleaned-display, and matching-normalized text are distinct values. Identifier
rules are deterministic and conservative; candidate output is unresolved and
case/evidence scoped.

## Consequences

- NFC/control/whitespace transformations are explicit, ordered, versioned, and
  canonically hashed. Matching cleanup is never presented as source text.
- Source spans map to the raw OCR string and preserve supplied locator,
  token/line-box, frame/time, and chunk/manifest/artifact lineage.
- Plate-like values are structural only and OCR ambiguity is warned about, not
  silently repaired. Handles require upstream platform context.
- Existing Phase 3 document and chat normalizers remain unchanged because they
  have separate source-specific profile semantics. The new package is the
  opt-in common layer for future OCR producers.
- The utility has no worker credentials, direct persistence, network, model,
  or graph dependency. Raw OCR source content must not be placed in Neo4j or
  transformation safe metadata.

## Alternatives rejected

Using a language model, generic NER, fuzzy correction, or automatic entity
resolution would make extraction non-reproducible and create unsupported
identity conclusions. Duplicating Nipun publication or Shreshtha graph mapping
would create a competing lifecycle and was rejected.
