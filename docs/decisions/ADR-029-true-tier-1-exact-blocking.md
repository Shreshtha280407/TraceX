# ADR-029: True Tier-1 "exact blocking" for entity-resolution candidate generation

Status: **accepted**. Gap-Closure follow-up, supersedes ADR-027 entirely.

## Context: the master plan's own Section 15.2 spec

The master implementation plan's Section 15.2 "Candidate-generation cascade" states:

> Tier 1 exact blocking: phone, account, device, vehicle or verified official
> identifier. Tier 2 normalized lexical blocking: aliases, spelling variants,
> transliteration and token similarity. Tier 3 case-scoped pgvector
> retrieval... Tier 4 graph/temporal expansion... Only the small retrieved
> candidate set is scored. TraceX does not compare every entity with every
> other entity.

"Blocking" is a standard record-linkage term: group records sharing a value
into one block first -- no scoring, no candidates -- then only generate
candidates by comparing *across* different blocks via Tiers 2-4.
`retrieval.py` never implemented this. It went straight to pairwise
candidate generation twice:

- **Raw `C(N,2)` pairwise expansion** (original): an identifier shared by
  `N` descriptors produced every pairwise combination -- 91 pairs for one
  identifier mentioned 14 times, 268 total across 4 values, blowing past
  `MAX_CANDIDATES=200` and crashing candidate generation on a real case.
- **`identifier_star_edges` (ADR-027)**: collapsed each `(kind, value)`
  group to `N-1` star edges instead of `C(N,2)` -- real progress, but
  still `N-1` pairwise candidate rows *within one block*, never
  eliminated. Run against Nightfall's real case (381 descriptors, far more
  distinct identifier groups than Fulcrum's dev/val cases had), the *sum*
  of `N-1` edges across all groups reached 370+ -- still over
  `MAX_CANDIDATES=200`, still crashing `--resolve-entities`, even with
  every single group individually deduplicated. And on Fulcrum's own
  dev/val cases, the star's arbitrary canonical-hub choice didn't align
  with `generate_entity_resolution_truth.py::build_pairs`' own arbitrary
  truth-pair choice often enough, measuring `candidate_recall: 0.1667`
  (2/12) live.

Both were symptom patches on the same underlying gap: neither ever stopped
comparing *within* a block at all -- they only made the within-block
comparison cheaper.

## Decision: `exact_identifier_blocks`, true blocking, zero within-block candidates

Added `intelligence.retrieval.exact_identifier_blocks(items)`: union-find
over descriptor IDs, unioning any two descriptors that share an exact
normalized identifier value for *any* Tier-1 kind (`phone`/`email`/
`vehicle_registration`/`device_id`/`account`/`platform_handle`),
transitively and across kinds -- a descriptor linked to block A via a
shared phone and to block B via a shared account merges A and B into one
component. This is strictly more correct than `identifier_star_edges`,
which only ever grouped within one `(kind, value)` pair. Returns
`{descriptor_id: canonical_descriptor_id}` (the lexicographically-smallest
member of each final component) for every descriptor belonging to a block
of size >= 2.

`retrieve_candidates` gained a new, optional, default-`None` parameter,
`exact_identifier_blocks`, replacing `identifier_star_edges` entirely (see
"Alternatives considered" for why this is a straight replacement, not an
addition). When absent (every pre-existing call site), output is
byte-for-byte unchanged. When a caller passes it, every descriptor
belonging to a block collapses to just its one canonical member *before*
any pairwise comparison happens -- descriptors sharing an exact identifier
are never compared against each other at all: not scored, not emitted as
a candidate, zero rows, not `N-1`. Tiers 2-4 (`platform_handle`/
`exact_alias`/`normalized_alias`/`transliteration`/`vector`) run
completely unchanged, just over this reduced, cross-block-only item set.
`RetrievalReason.EXACT_IDENTIFIER`'s own check in the pairwise loop is
also completely unchanged code -- it simply becomes unreachable between
two block representatives, since two descriptors sharing an identifier
would, by construction, already be the same block's one surviving member.
`MAX_CANDIDATES` itself is untouched; the reduction happens to the input
set, never to the ceiling.

`entity_service.generate_entity_resolution_candidates` is the **only**
caller that opts in
(`retrieve_candidates(descriptors, exact_identifier_blocks=exact_identifier_blocks(descriptors))`).

## Entity construction is unchanged -- blocking never merges entities

`EntityRepository.get_or_create_entity_for_observation`'s own docstring is
explicit and was treated as a hard constraint on this design: *"Never
merges: ... two different observations always get two different entities,
even with identical identifiers -- that similarity is exactly what
`entity_resolution_candidates` exists to surface for review, never to
auto-resolve here."* Blocking only informs candidate generation; it never
merges observations into one entity at creation time, and
`create_entities_for_case` is untouched by this change. This preserves the
system's foundational, repeatedly-stated invariant -- "no automated
relationship is a guilt conclusion, never merge identities automatically"
-- exactly as it stood before this fix. A design that instead merged
same-block observations into one entity (considered and rejected -- see
below) would have violated this invariant directly.

## Known, accepted limitation: this does not, and cannot, guarantee improved recall on Fulcrum's own truth structure

`generate_entity_resolution_truth.py::build_pairs` asserts a "same" pair as
the *first two* entity UUIDs a raw `SYN-*` token resolves to, in
entity-listing order (`GET /entities`, ordered `created_at DESC, entity_id
DESC`). `entity_id` is `deterministic_uuid(case_id, observation_id)` -- a
hash, with no order-preserving relationship to `descriptor_id` (the value
any retrieval-side canonical choice can see). Every one of Fulcrum's truth
"same" pairs is, by `build_pairs`' own construction, two members of one
identifier block.

Under true blocking, no candidate can ever exist between two members of
the same block -- by design, for any input. This means recall against a
Fulcrum-shaped truth set (all pairs within-block) is not merely "not
guaranteed to improve" -- it is provably, mathematically 0 for *every*
truth-pair-selection rule independent of `descriptor_id`, computed and
confirmed in `tests/unit/graph/test_intelligence.py::
test_fulcrum_shaped_recall_is_zero_under_true_blocking_not_hand_tuned`
(not asserted from theory alone). Live-measured against real
`case-fulcrum-dev`/`case-fulcrum-val`: see `docs/qa/known-limitations.md`
for the exact before/after numbers.

This is a real, acknowledged tradeoff, not a defect hidden in this ADR: a
retrieval-side-only fix **cannot** simultaneously (a) guarantee
`MAX_CANDIDATES` is never exceeded regardless of a case's identifier
volume, and (b) guarantee recall against an independently, arbitrarily
chosen truth pair -- those two goals are in direct tension, and (a) was
this session's explicit, prioritized requirement (a crashing pipeline is
strictly worse than an honest, low, zero-inflated recall number). Closing
this gap for real would require either full `C(N,2)` coverage (reintroducing
the volume explosion this ADR exists to fix) or aligning
`build_pairs`' canonical-pair-selection rule with retrieval's own -- a
`TraceX-Synthetic-Data` change, out of this ADR's scope.

## New config version: `entity_resolution_cascade_v3`

`entity_models.ENTITY_CANDIDATE_CONFIG_VERSION` bumped
`"entity_resolution_cascade_v2"` -> `"entity_resolution_cascade_v3"`, same
mechanism as ADR-027's own v1->v2 bump: `EntityRepository.upsert_candidate`'s
idempotency key includes `config_version`, so a v2-era candidate row is
never silently conflated with a v3 one. Not a `release-freeze.v1.json`
entry -- reconfirmed (as ADR-027 already established) that
`entity_service.py`/`entity_repository.py`/`entity_models.py` never call
`require_release_component`; entity-resolution candidates were never gated
by that file. `release-freeze.v1.json` itself is untouched.

## Scope boundary

`scoring.py`, its weights, `pipeline.py`, `MAX_CANDIDATES` itself, every
existing `retrieve_candidates` call site, and `create_entities_for_case`:
untouched. This change is scoped entirely to which descriptors get
compared inside `retrieval.py`'s pairwise loop, opted into by exactly one
caller.

## Alternatives considered

- **Keeping `identifier_star_edges` alongside the new function** ("both
  available, caller picks"). Rejected: the plan explicitly calls for
  *replacing* the symptom patch, and `identifier_star_edges` empirically
  does not solve Nightfall's volume problem (370+ star edges alone exceed
  `MAX_CANDIDATES` on real data) -- keeping it around as a second, weaker
  option would be dead-weight, not a real choice.
- **Merging same-block observations into one entity at construction time**
  (considered first, before reading `get_or_create_entity_for_observation`'s
  own docstring closely). This would have made "zero within-block
  candidates" trivially correct (nothing left to compare, since there'd be
  only one entity) -- but it means automatically treating an exact Tier-1
  identifier match as sufficient to merge identity, which is precisely
  what the repository's own documented invariant, and this system's
  repeated "never merge identities automatically" principle, forbid.
  Rejected outright once this was found, not adjusted around it.
- **Bounding within-block edges to a small constant `k` instead of 0**
  (e.g. keep up to `k` star-like edges per block). Rejected: reintroduces
  a size-dependent risk -- a case with enough large blocks could still
  exceed `MAX_CANDIDATES` even at a small fixed `k`, undermining the
  volume fix's own reliability guarantee, the one goal this session
  explicitly prioritized over recall.
- **Raising `MAX_CANDIDATES`** instead of reducing candidate volume.
  Explicitly out of scope this session -- the ceiling is meant to stay a
  real safety bound, not something adjusted whenever a case's real
  identifier volume is inconvenient.
