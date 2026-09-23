# ADR-027: Entity-resolution candidate generation -- identifier star-edge deduplication

Status: **superseded by ADR-029** (true Tier-1 "exact blocking" per the
master plan's Section 15.2). `identifier_star_edges` reduced a repeated
identifier's `C(N,2)` pairwise explosion to `N-1` star edges, but still
emitted `N-1` candidate rows *within* one identifier's group -- found
insufficient on a real case with many distinct identifier groups (Nightfall:
381 descriptors, 370+ star-edged pairs summed across groups, still over
`MAX_CANDIDATES=200`), and its arbitrary canonical-hub choice measured
`candidate_recall: 0.1667` live against Fulcrum's own truth structure. See
ADR-029 for the replacement (`exact_identifier_blocks`, zero within-block
candidates) and its own honest accounting of what that replacement does and
does not fix. This document is kept as-is below for historical context --
the design record of the star-edge approach that was tried, and why it
wasn't enough -- not rewritten.

Status (as originally accepted, historical): **accepted**. Gap-Closure
follow-up, real Phase 5 candidate-generation change (not cosmetic) scoped to
WP-2's entity-resolution cascade.

## Context

`entity_service.generate_entity_resolution_candidates` (WP-2, ADR-020)
reuses `intelligence.retrieval.retrieve_candidates` unchanged, exactly as
ADR-020 Decision 2 specified: "no new matching logic was written." That
function's exact-identifier tier does raw pairwise expansion -- for any
identifier value shared by `N` descriptors, it produces `C(N,2)` candidate
pairs, each independently restating the identical fact ("these two
mentions share identifier X").

This was never exercised against real, evidence-derived data before
`--resolve-entities` (a separate Gap-Closure follow-up) gave it a live
caller for the first time. Run against a real case
(`case-fulcrum-dev`, 222 observations, 80 identity-bearing descriptors):
an account identifier mentioned 14 times produced `C(14,2)=91` pairs
encoding one fact. Across only 4 distinct account values (10-14 mentions
each), this totalled 268 pairs, blowing past `retrieval.MAX_CANDIDATES`
(200) and crashing candidate generation outright -- entities still got
created (that half of the pipeline is unaffected), but zero candidates
persisted for the case.

Manual verification against Fulcrum's authored `communities.json`/
`entities.json` ground truth confirmed all 268 pairs were genuinely
correct same-account matches -- zero false positives, zero cross-token
collisions. This is not a data-quality or judgment problem; it is pure
combinatorial waste from a retrieval algorithm with no notion of "these
`N` mentions all already agree," which will recur in any case where an
identifier repeats more than a handful of times, not only Fulcrum.

Found via live testing against real Fulcrum data, not by this repository's
own test suite -- no existing test exercised `retrieve_candidates` against
more than a handful of descriptors sharing one identifier value.

## Decision: identifier star edges, opt-in, entity-resolution only

Added `intelligence.retrieval.identifier_star_edges(items)`: for each
`(kind, normalized_value)` group of descriptors sharing an identifier,
sorts the group deterministically (by `descriptor_id`), designates the
first as canonical, and returns exactly `N-1` "star" edges (canonical to
each other member) instead of the full `C(N,2)` set. Every descriptor
sharing that value is still directly or transitively linked through the
canonical hub -- reviewing the `N-1` star edges still surfaces every
member of the group -- without redundant pairwise restatement.

`retrieve_candidates` gained one new, optional, default-`None` parameter:
`identifier_star_edges`. When absent (every pre-existing call site),
output is byte-for-byte unchanged. When a caller passes it, an
exact-identifier match on a pair only counts as a reason if that pair is
one of the precomputed star edges for that kind -- every other reason
(`platform_handle`/`exact_alias`/`normalized_alias`/`transliteration`/
`vector`) and `MAX_CANDIDATES` itself are untouched.

`entity_service.generate_entity_resolution_candidates` is the **only**
caller that opts in (`retrieve_candidates(descriptors,
identifier_star_edges=identifier_star_edges(descriptors))`).

## Why not modify `retrieve_candidates` in place

`retrieve_candidates` is shared: `entity_service.py` (WP-2, not
release-freeze-gated) **and** `intelligence.pipeline.
build_case_correlation_submission` (Phase 5's correlation pipeline,
`--generate`, gated by `require_release_component`/`RULES_CONFIG_HASH`).
`RULES_CONFIG_HASH` hashes `scoring.py`'s rule *weights* -- completely
disconnected from `retrieval.py`'s code. Modifying `retrieve_candidates`
in place would have silently changed the frozen correlation pipeline's
candidate output while its frozen hash stayed identical -- exactly the
"silently reusing the frozen config without a version bump" failure this
change is required not to cause, just for the other consumer. The
optional-parameter design keeps `pipeline.py`'s call site (which never
passes it) provably unaffected; a static test
(`test_pipeline_call_site_never_opts_into_identifier_star_edges`) asserts
`build_case_correlation_submission`'s source never references
`identifier_star_edges`, the same "a rule stated only in a docstring is
not a guarantee" precedent this codebase already applies elsewhere.

## New config version: `entity_resolution_cascade_v2`

`entity_models.ENTITY_CANDIDATE_CONFIG_VERSION` bumped
`"entity_resolution_cascade_v1"` -> `"entity_resolution_cascade_v2"`.
`EntityRepository.upsert_candidate`'s idempotency key includes
`config_version`, so a v1-era candidate row (if one existed) is never
silently conflated with a v2 one -- the version change is real, not
cosmetic, and is threaded through every candidate this cascade produces
from here on.

Not a `release-freeze.v1.json` entry: confirmed
`entity_service.py`/`entity_repository.py`/`entity_models.py` never call
`require_release_component` at all -- entity-resolution candidates were
never gated by that file, only Phase 5 correlation is (see ADR-020's own
"Alternatives considered": entity-resolution deliberately never imports
`scoring.py`). `release-freeze.v1.json` itself is untouched by this
change.

## Scope boundary

`scoring.py`, its weights, `pipeline.py`, and every existing
`retrieve_candidates` call site: untouched. This change is scoped
entirely to the exact-identifier tier's pair-emission inside
`retrieval.py`, opted into by exactly one caller.

## Alternatives considered

- **A wholly separate, duplicated function** instead of an optional
  parameter on the shared one. Rejected: would duplicate ~80 lines of
  reason-computation logic (platform/alias/transliteration/vector
  checks, contradiction tracking, observation-pair merging) that has
  nothing to do with the fix, doubling the surface area that could drift
  out of sync for no benefit over a default-`None` parameter that
  provably preserves every existing caller's behavior.
- **Lowering or removing `MAX_CANDIDATES`** instead of deduplicating.
  Rejected: the bound is a legitimate safety net for reasons this fix
  does not address (a widely shared alias or platform handle could still
  combinatorially explode); raising it without fixing the actual
  pairwise-explosion mechanism would just move the failure point further
  out, not close the real gap.
- **Global (not per-kind) star grouping** -- collapsing a descriptor's
  matches across *all* its shared identifiers into one star. Rejected:
  would conflate independent identifier kinds (e.g. a person who is both
  an account co-signer and a phone contact) into a single grouping
  decision the retrieval layer has no basis for making; per-kind grouping
  keeps each identifier type's dedup independent and auditable.
