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
