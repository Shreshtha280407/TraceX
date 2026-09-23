# ADR-030: True Tier-2 lexical blocking and Tier-3 pgvector retrieval

Status: **accepted**. Gap-Closure follow-up, extends ADR-029's Tier-1 fix to
the remaining tiers of the same cascade.

## Context

ADR-029 fixed Tier 1 (exact identifier blocking) but was explicitly scoped
to Tier 1 only. A follow-up audit (this session) read every module in the
candidate-generation cascade (`retrieval.py`, `pipeline.py`, `correlation.py`,
`vector_store.py`, `analytics.py`) to check whether Tiers 2-4 build their
comparison set via blocking or via raw pairwise `combinations()`, per the
master plan's Section 15.2:

- **Tier 2** (`PLATFORM_HANDLE`/`EXACT_ALIAS`/`NORMALIZED_ALIAS`/
  `TRANSLITERATION`): computed inside the *same* `combinations(items, 2)`
  loop as everything else -- no grouping step at all. Live-confirmed against
  Nightfall's real (corpus-bug-fixed) data: 10 alias values repeated 6 times
  each produced 150 `EXACT_ALIAS` pairs -- the identical unbounded-repetition
  pattern Tier 1 had before ADR-029, just one tier over.
- **Tier 3** (vector): two things exist under this name. The spec-correct
  implementation, `PgvectorCandidateStore.search()` (`vector_store.py`), is a
  genuine bounded (`limit` 1-200) indexed nearest-neighbor query -- but has
  *zero* live callers anywhere in the app (`grep -rln "PgvectorCandidateStore"
  app/` outside its own module returns nothing). What actually runs is the
  "local hashed token vector" cosine check, inline in the same raw
  `combinations()` loop as Tier 2 -- same O(N^2) exposure.
- **Tier 4** (temporal/graph): `add_temporal_hot_window_reason` only
  *annotates* candidates `retrieve_candidates` already returned -- it can't
  independently explode, and is left entirely untouched by this ADR. It is
  wired only into Phase 5's frozen correlation pipeline, never into WP-2's
  entity-resolution cascade -- a real gap, but a "never wired" gap, not a
  "wired and buggy" one, and out of this ADR's scope. Graph expansion does
  not exist as a candidate-generation step anywhere; `analytics.py`'s
  Leiden/betweenness/PageRank analysis is a separate downstream feature over
  the already-persisted candidate graph, not part of generation.

## Decision: `lexical_blocks` (Tier 2) and `vector_linked_candidates` (Tier 3)

### Tier 2: `intelligence.retrieval.lexical_blocks(items)`

Same shape as `exact_identifier_blocks` (ADR-029): union-find over
descriptor IDs, but on a combined normalized-token space instead of
per-identifier-kind values -- `casefold(alias) | casefold(transliteration)`
as one token set per descriptor, plus `(platform, handle)` pairs
(namespaced with a NUL separator so a handle string can never collide with
an unrelated alias string). One combined space, not four separate
mechanisms: `EXACT_ALIAS` is exactly `NORMALIZED_ALIAS` restricted to
un-casefolded equality, and `TRANSLITERATION` already compares
transliterations against the *other* side's aliases-or-transliterations --
blocking on the union subsumes all three lexical-name reasons at once, the
same way `exact_identifier_blocks` unions across identifier *kinds* rather
than keeping phone/account/vehicle separate.

`retrieve_candidates` gained a second optional, default-`None` parameter,
`lexical_blocks`, applied *after* `exact_identifier_blocks` (Tier 1 reduces
first; Tier 2 reduces whatever Tier 1 left). `PLATFORM_HANDLE`/`EXACT_ALIAS`/
`NORMALIZED_ALIAS`/`TRANSLITERATION`'s own checks are unchanged code --
they become unreachable between two block representatives by the same
construction argument as `EXACT_IDENTIFIER` under Tier 1.

### Tier 3: `intelligence.vector_store.vector_linked_candidates(store, items)`

Wires in the previously-unused `PgvectorCandidateStore` for real: upserts
every descriptor's vector (idempotent), then queries each one's bounded
nearest-same-case-neighbors via `.search()` -- never an all-pairs scan.
Lives in `vector_store.py`, not `retrieval.py`: this is genuinely async,
DB-backed I/O, and `retrieval.py`'s own docstring commits it to being
"case-scoped, bounded **deterministic**" -- pure, synchronous, no I/O.
Mixing an async DB call into that function would have broken the purity
guarantee `pipeline.build_case_correlation_submission` (a function
explicitly documented as "Pure: real observations for one case -> at most
one `CorrelationSubmission`") depends on.

Instead, `retrieve_candidates` gained a third parameter,
`include_inline_vector: bool = True` -- `False` retires the legacy inline
cosine scan for that call entirely. A caller that wants real Tier-3
retrieval calls `retrieve_candidates(..., include_inline_vector=False)` for
Tiers 1-2, separately awaits `vector_linked_candidates(store, descriptors)`
for Tier 3, and combines the two via the new `retrieval.merge_candidates`
(unions reasons/identifier_types/contradiction_reasons by observation pair,
takes the max `vector_score`) -- never a second, competing scan running
alongside the bounded query.

`entity_service.generate_entity_resolution_candidates` gained an optional
`vector_store: PgvectorCandidateStore | None = None` parameter. `None`
(every existing test, which has no real Postgres connection to spare) keeps
the legacy inline vector behavior (`include_inline_vector=True`) and skips
Tier 3 entirely -- unaffected. `intelligence_worker.py`'s `--resolve-entities`
is the one real caller that supplies one.

## Two-party vector-upsert collision -- a known, pre-existing store
## characteristic, not something this ADR introduces

`graph_intelligence_vectors`' uniqueness constraint is `(case_id,
observation_id)`, but two-party descriptors (a CDR/finance record's caller
and callee) share one `observation_id` while being two distinct
descriptors -- upserting both overwrites one with the other. In practice
this doesn't matter for what's live today: two-party descriptors never
carry aliases/transliterations (`sourcing.py`'s two-party fields are
identifier-only), so their `hashed_token_vector` is the same all-zero
vector regardless of which one's upsert "wins." Noted here, not fixed --
the store's schema predates this ADR and is used as designed, not
redesigned.

## Scope boundary

Tier 4, `scoring.py`, `MAX_CANDIDATES`, `pipeline.py`'s frozen call site,
and `create_entities_for_case` (entity construction): untouched. Confirmed
via `test_pipeline_call_site_never_opts_into_exact_identifier_blocking`
(extended this ADR to also assert `"lexical_blocks"`/
`"include_inline_vector"` never appear in `build_case_correlation_submission`'s
source).

## New config version: `entity_resolution_cascade_v4`

Same mechanism as ADR-029's v2->v3 bump: `EntityRepository.upsert_candidate`'s
idempotency key includes `config_version`, so a v3-era candidate row is
never silently conflated with a v4 one. Not a `release-freeze.v1.json`
entry, for the same reason ADR-027/ADR-029 already established.

## Alternatives considered

- **Four separate Tier-2 blocking mechanisms** (one each for handle/exact-
  alias/normalized-alias/transliteration). Rejected: `EXACT_ALIAS` and
  `NORMALIZED_ALIAS` are the same signal at two strictness levels, and
  `TRANSLITERATION` already cross-compares against aliases by design --
  four mechanisms would just be one union-find's work done four times with
  more surface area to drift out of sync.
- **Making `retrieve_candidates` itself async** to call `PgvectorCandidateStore`
  directly. Rejected: breaks `retrieval.py`'s stated purity contract and
  `pipeline.build_case_correlation_submission`'s explicit "Pure" guarantee
  for every existing caller, for the sake of the one new caller that
  actually wants DB-backed retrieval.
- **Redesigning `graph_intelligence_vectors`' schema to be descriptor-scoped
  instead of observation-scoped**, to close the two-party collision noted
  above. Rejected as out of scope: the store's schema is pre-existing,
  never exercised, and the collision is a documented no-op for every
  descriptor type that currently carries lexical signal.
