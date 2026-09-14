# ADR-007: Phase 4 media checkpointing uses the canonical batch transaction

## Decision

Media chunk publication extends `EvidenceLifecycleRepository.submit_observation_batch`
rather than creating a parallel worker persistence path. Manifest creation is
separate and immutable; chunk completion, artifact registration, checkpointing,
canonical observations, progress, and the existing graph projection outbox are
committed together.

## Consequences

Workers may restart and replay an acknowledged-or-unknown chunk safely. A
checkpoint only describes committed work. Neo4j unavailability cannot roll back
an accepted chunk because projection is asynchronous from PostgreSQL. The route
uses the pre-existing internal worker claim/identity seam; this ADR does not
define new security policy or media decoding.
