# ADR-006: Phase 5 preliminary transparent rules baseline

Status: **frozen** (see "Measurement and freeze" addendum below). The
`phase5_preliminary_rules_v1` weights are the accepted Phase 5 baseline.

Named deterministic retrieval and score contributions use Nipun's correlation/feature-snapshot transaction.
Stable identifiers block candidates only within a case. Alias, transliteration, handle, temporal, and vector
signals are weak review reasons; none proves identity or causes a merge.

The preliminary `phase5_preliminary_rules_v1` configuration is canonically hashed.
Semantic changes require a new configuration version and correlation idempotency key; historical records are
not silently mutated. The handler uses durable provenance and Nipun's projection key and creates a
review-only correlation node, never a direct entity edge.

Operation Nightfall truth evaluation, P99 bridge validation, full real-dataset validation, and LAN
end-to-end validation remain separate, later release-validation activities -- see "What remains
deliberately deferred" below. Measured Precision@K/Recall@K/false-link rate and the final rules-weight
freeze are recorded in the "Measurement and freeze" addendum.

## Addendum (Phase 5A reconciliation)

This baseline's rules/scoring engine was already fully implemented and unit-tested when a reconciliation
audit reviewed this module, but nothing yet chained it to real persisted observations or Nipun's durable
seam in a reachable process. That wiring (`intelligence/sourcing.py`, `intelligence/pipeline.py`,
`app/modules/graph/intelligence_worker.py`) is now in place and covered by a live end-to-end test
(`tests/integration/graph/test_intelligence_pipeline_live.py`) that submits a real correlation, replays it,
and confirms a `Correlation` node is projected exactly once. This does not change the rules baseline itself
-- weights, hashes, and status semantics are unchanged from the paragraphs above -- it only makes them
genuinely reachable, which this ADR's own text already assumed.

## Addendum (Phase 5 final integration: measurement and freeze)

All Phase 5A/5B contributors merged (`sarthak/5B`, `gaurav/5B`, `jasraj/5B`, `aditya/5B`,
`shreshtha/5A`), closing the condition this ADR's original text deferred measurement on. This addendum
records the deferred experiment, run against a versioned, controlled, synthetic, non-sensitive benchmark
-- `tests/fixtures/graph/phase5_rules_benchmark.py` (`phase5_rules_benchmark_v1`) -- never private police
data or Operation Nightfall data. The full reproducible experiment is
`tests/unit/graph/test_phase5_rules_benchmark.py`; run it directly with
`uv run pytest tests/unit/graph/test_phase5_rules_benchmark.py -v` to reproduce every number below.

### Benchmark design

One case, nine synthetic observations, covering every scenario this measurement needed: two-party CDR
cross-blocking (a call's callee reappears as a different call's caller), two-party finance cross-blocking
(a transfer's receiver reappears as a different transfer's sender), a cross-modal exact match (a CDR
caller number also appears in a FIR-style document mention), a weak transliteration/alias match (two chat
messages, one Devanagari-transliterated), two clean negatives with no relationship to anything else, and
the two-party records' own same-event pairs (a call's caller and callee, a transfer's sender and
receiver) which must never become a candidate of each other. A separate direct-construction probe
exercises match-plus-contradiction co-occurrence (see the probe's own docstring for why it cannot be
built from a real producer's observation shape yet), and a separate cross-case probe exercises case
isolation.

### Results (baseline profile `phase5_preliminary_rules_v1`)

| Metric | Result |
| --- | --- |
| Precision@K (K = 4 labeled positives) | **1.0** (4/4 retrieved candidates are true positives) |
| Recall@K | **1.0** (4/4 labeled positives were retrieved) |
| False-positive / false-link count | **0** |
| Same-event (two-party) self-pairing | **0** -- correctly suppressed in every case |
| Contradiction handling | Recorded (`conflicting_phone_claim`) alongside the genuine alias match; candidate downgraded to `needs_review`, not discarded |
| Case isolation | Enforced -- a cross-case comparison attempt raises before any scoring runs |
| Reason-code / score determinism | Identical `RetrievedCandidate`/`ScoredCandidate` output (including `candidate_key`) across repeated runs |

### Configuration variant tested

One explicit alternative (`phase5_rules_benchmark_alternative_v1`): every positive weight roughly halved,
the local hashed-token vector weight set to `0.0`, and the contradiction penalty reduced in magnitude.
Retrieval itself does not depend on scoring weights, so the alternative profile retrieves the *identical*
candidate set as the baseline; scoring it changes only the numeric scores, and -- because no two reasons
interact non-linearly on this benchmark -- the relative ranking of candidates is unchanged too.

### Freeze decision

**The existing `phase5_preliminary_rules_v1` weights are retained and frozen.** The tested alternative
is not a safety regression, but it is also not a measurable improvement: it does not change which
candidates are found, does not change their ranking, and the baseline already achieves perfect
Precision@K/Recall@K with zero false links on this benchmark. Per this task's own explicit guidance --
"If no tested alternative improves the baseline safely, retain the existing profile and document that
decision" -- that is exactly this outcome. `app/modules/graph/intelligence/scoring.py`'s
`RulesProfile`/`BASELINE_RULES_PROFILE` now make this an explicit, typed, comparable configuration object
(previously three bare module constants) so a *future* candidate variant can be evaluated the same way
without editing the scoring function itself; `score_candidates(candidates, profile=...)` accepts any
`RulesProfile`, defaulting to the frozen baseline.

### What this measurement does and does not establish

A synthetic, nine-observation, single-case benchmark proves the rules engine's *mechanics* are correct
(blocking, same-event suppression, contradiction handling, determinism, case isolation) and that the
frozen weights do not obviously underperform a reasonable alternative on those mechanics. It is not, and
is not presented as, a claim about real-world precision/recall against genuine investigative data,
adversarial name variation at scale, or the full Operation Nightfall benchmark -- those remain the
separate, later evaluation activities named above and in `docs/qa/known-limitations.md`. The frozen score
remains a transparent investigation-priority signal only -- never a probability of guilt, identity truth,
or criminal association, exactly as the paragraphs above already state.

### What remains deliberately deferred

Operation Nightfall truth evaluation, ML training/model comparison (explicitly out of scope for every
phase through Phase 7), P99 bridge/throughput validation, full real-dataset validation, and final
multi-machine LAN release validation. None of these were attempted here, and none are claimed as done.
