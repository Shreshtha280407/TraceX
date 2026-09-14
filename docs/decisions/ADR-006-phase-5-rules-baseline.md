# ADR-006: Phase 5 preliminary transparent rules baseline

Status: accepted for implementation; measurement and final freeze deferred.

Named deterministic retrieval and score contributions use Nipun's correlation/feature-snapshot transaction.
Stable identifiers block candidates only within a case. Alias, transliteration, handle, temporal, and vector
signals are weak review reasons; none proves identity or causes a merge.

The preliminary `phase5_preliminary_rules_v1` configuration is canonically hashed.
Semantic changes require a new configuration version and correlation idempotency key; historical records are
not silently mutated. The handler uses durable provenance and Nipun's projection key and creates a
review-only correlation node, never a direct entity edge.

Operation Nightfall truth evaluation, measured Precision@K/Recall@K/false-link rate, P99 bridge validation,
final rules-weight freeze, full real-dataset validation, and LAN end-to-end validation will run after all
Phase 5 contributors have merged their work.

## Addendum (Phase 5A reconciliation)

This baseline's rules/scoring engine was already fully implemented and unit-tested when a reconciliation
audit reviewed this module, but nothing yet chained it to real persisted observations or Nipun's durable
seam in a reachable process. That wiring (`intelligence/sourcing.py`, `intelligence/pipeline.py`,
`app/modules/graph/intelligence_worker.py`) is now in place and covered by a live end-to-end test
(`tests/integration/graph/test_intelligence_pipeline_live.py`) that submits a real correlation, replays it,
and confirms a `Correlation` node is projected exactly once. This does not change the rules baseline itself
-- weights, hashes, and status semantics are unchanged from the paragraphs above -- it only makes them
genuinely reachable, which this ADR's own text already assumed.
