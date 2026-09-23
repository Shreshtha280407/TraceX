# ADR-032: Real Tier-3 signal -- character n-gram TF-IDF + SVD, replacing the single-token hash

Status: **accepted**. Gap-Closure follow-up, extends ADR-030's Tier-3
retrieval-shape fix to the signal itself.

## Context

ADR-030 wired `PgvectorCandidateStore` into real use -- a genuine bounded
nearest-neighbor query per descriptor, replacing an all-pairs cosine scan.
But every vector it persisted and queried was still `retrieval.
hashed_token_vector`: a deterministic hash of casefolded alias/
transliteration tokens into `VECTOR_DIMENSIONS` (32) buckets via
`sha256(token) % 32`. Live-confirmed this session (real, corpus-bug-fixed
Nightfall data, post-ADR-030/ADR-031 re-ingestion): 10 distinct `SYN-PER-
NF-*` names, structurally near-identical (differing only in a trailing
two-digit sequence number), produced a **43-of-45-possible-edge clique**
under this signal -- not two distinguishable communities with a bridge,
the shape the master plan's own Phase 5 exit gate (Section 10) expects.

Root cause: a 32-bucket modular hash of a *single token* has no notion of
"these two strings are almost the same" versus "these two strings are
almost the same" -- `hashed_token_vector` scatters even a one-character
difference (`"...NF-01"` vs. `"...NF-02"`) into unrelated buckets, but with
only ~10-30 distinct alias values in a whole case, the pigeonhole effect
across 32 buckets makes many *unrelated* names collide into overlapping
bucket sets by chance -- coarse, not discriminating, exactly what
`retrieval.py`'s own module docstring already warned it is ("a weak
retrieval aid, not a semantic model").

## Decision: `case_tfidf_vectors` -- character n-gram TF-IDF + TruncatedSVD, case-scoped

New `vector_store.case_tfidf_vectors(items)`: fits `sklearn.feature_
extraction.text.TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4))`
fresh over exactly one case's own descriptor text (never cross-case, never
persisted or reused across calls -- a new fit every invocation), then
reduces the sparse TF-IDF matrix via `sklearn.decomposition.TruncatedSVD`
to `VECTOR_DIMENSIONS` (32) so the output slots into the existing
`graph_intelligence_vectors.embedding vector(32)` column unchanged -- **no
migration needed**. `random_state=0` for determinism, matching this
codebase's existing fixed-seed convention (`analytics.py`'s own Leiden
modularity).

Character n-grams specifically target the failure mode found: `char_wb`
2-4-grams of `"SYN-PER-NF-01"` and `"SYN-PER-NF-02"` share every n-gram
except the ones touching the final two characters -- exactly the
discriminating signal a coarse hash-into-32-buckets scheme throws away,
and exactly why this approach was chosen over further tuning the existing
hash function.

### What text a descriptor contributes

`vector_store._descriptor_text(item)`: aliases + transliterations +
`identifiers.values()` + handles + platform, space-joined -- the full text
context a descriptor carries, not only aliases/transliterations
(`retrieval._tokens`'s narrower scope, still used unchanged by
`hashed_token_vector` itself). `participant_role` (`"caller"`/`"callee"`)
is deliberately excluded: it is evidence-local role metadata, not identity
text, and including it would spuriously inflate similarity between two
unrelated people's same-role descriptors.

## Two new provider-identity constants, never conflated with the frozen inline path

`TFIDF_VECTOR_PROVIDER = "case_tfidf_char_ngram_svd"` / `TFIDF_VECTOR_
PROVIDER_VERSION = "v1"`, alongside a new `tfidf_snapshot_hash()` function
-- kept fully separate from `VECTOR_PROVIDER`/`VECTOR_PROVIDER_VERSION`/
`source_snapshot_hash()`, which `hashed_token_vector` and every one of its
existing callers (Tiers 1-2's own `include_inline_vector=True` fallback,
Phase 5's frozen correlation pipeline) keep using completely unchanged.
`PgvectorCandidateStore.upsert()`/`.search()` both gained optional
`embedding`/`provider`/`provider_version`/`snapshot_hash` parameters,
defaulting to the original `hashed_token_vector`-based values -- every
pre-existing caller's behavior is byte-for-byte unchanged; only `vector_
linked_candidates` (ADR-030) passes the new values explicitly, computing
`case_tfidf_vectors` once and reusing the same vectors for both the
upsert and the search query, so the two always share one embedding space.

## New config version: `entity_resolution_cascade_v5`

Same mechanism as every prior version bump in this cascade: `upsert_
candidate`'s idempotency key includes `config_version`, so a v4-era
candidate row (computed against the old hash signal) is never silently
conflated with a v5 one.

## Live result: honestly reported, not hand-tuned

See `docs/qa/known-limitations.md` for the full live-measured before/
after -- Fulcrum dev/val candidate counts and metrics with the new signal,
and Nightfall's real post-fix candidate graph, reported exactly as
computed. This ADR does not claim the fix produces a "two communities +
bridge" result by construction; it claims (and the live proof verifies)
that the signal now genuinely discriminates between different descriptor
texts, which is what was actually broken.

## Dependency: `scikit-learn` promoted from a transitive, benchmark-only extra to a core dependency

Investigated before implementing, per this session's own directive not to
reach for a new dependency without checking first: `scikit-learn` was
**not** actually installed in the running deployment, contrary to the
initial assumption that it was already part of the core stack. It existed
in `uv.lock` only as a transitive dependency of `pyannote-audio` (pulled
in via the `audio-social-benchmark` *optional* extra), and the production
`Dockerfile` never installs any extra (`uv sync --locked --no-install-
project --no-dev`, no `--extra` flag) -- confirmed by direct import
(`ModuleNotFoundError` for `sklearn`/`scipy`/`joblib`/`threadpoolctl` in
the real environment before this change).

Promoted to a genuine core dependency (`pyproject.toml`'s `dependencies`
list, `scikit-learn>=1.9.1`) rather than left as an implicit side effect
of an unrelated extra. Low-risk: `uv lock` resolved in under 100ms (no new
network resolution -- the exact version was already fully pinned in the
lock graph), pure numerical package (no CUDA, no model-weight download at
build or run time), same risk tier as `igraph`/`leidenalg` (already core
deps for `analytics.py`'s Leiden modularity). `sklearn.*` added to
`pyproject.toml`'s `ignore_missing_imports` mypy overrides, matching the
existing precedent for every other untyped scientific-Python dependency
in this project (`igraph.*`, `leidenalg.*`, `pyarrow.*`, ...).

## Scope boundary

`scoring.py`, `MAX_CANDIDATES`, `pipeline.py`'s frozen call site,
`release-freeze.v1.json`, Tier 1 (`exact_identifier_blocks`), and Tier 2
(`lexical_blocks`): untouched. This ADR is scoped entirely to what vector
Tier 3 computes and persists, not to the retrieval shape ADR-030 already
fixed, and not to any other tier.

## Alternatives considered

- **A heavier embedding model** (e.g. `sentence-transformers`). Rejected
  per this session's explicit instruction: a new model dependency needing
  weight downloads at build/runtime reintroduces exactly the network-
  dependent Docker fragility risk this codebase has hit before, for a
  problem (short, near-identical synthetic name discrimination) that
  character-level lexical similarity already solves without one.
- **Tuning `hashed_token_vector`'s own bucket count or hash function**
  instead of a real TF-IDF signal. Rejected: the failure mode is
  structural (a coarse hash has no notion of partial string similarity at
  any bucket count small enough to stay a useful retrieval aid); more
  buckets would reduce collisions somewhat but never actually captures
  "these strings share most of their characters," which char n-gram
  TF-IDF does directly.
- **Fitting one global vectorizer across all cases** instead of per-case.
  Rejected outright: cross-case fitting would leak one case's vocabulary
  into another's similarity computation, a real case-isolation violation
  this codebase treats as a hard boundary everywhere else (`retrieval.
  _single_case`, `PgvectorCandidateStore`'s own case-scoped queries).
