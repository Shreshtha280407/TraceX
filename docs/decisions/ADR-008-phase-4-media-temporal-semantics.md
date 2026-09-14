# ADR-008: Phase 4 media uses existing temporal events and source-relative time

## Decision

Project canonical media and communication observations as case-scoped existing `TemporalEvent` nodes with Phase 4 mapping identities. Use `sighting`, `speech_segment`, `message`, and `meeting_candidate` event types rather than new labels. Retain source-relative media milliseconds and frame numbers as such; only canonical aware timestamps are instants. Treat meeting material as a candidate only when an upstream canonical observation explicitly says so.

## Consequences

The mapping reuses the durable canonical-observation outbox and the existing `(case_id, projection_id)` Neo4j uniqueness constraint. It can project a published partial chunk independently and safely retain chunk/manifest/artifact identifier lineage without exposing object references or payloads. Speaker labels, visual labels, names, and handles remain unresolved source claims.

This ADR does not alter frozen contracts, Nipun's publication/checkpoint schema, Aditya's worker authorization or retry policy, extraction workers, or Phase 5 correlation/scoring semantics.
