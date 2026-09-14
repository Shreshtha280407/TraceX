# Phase 5 graph/correlation integration foundation

Owner: Nipun. Status: in progress.

## Durable boundary

```text
PostgreSQL transaction
  correlation_records + correlation_candidate_links + optional feature snapshot
  + graph_update_events
    -> asynchronous claim/lease/replay
    -> registered idempotent Neo4j projection handler
```

`graph_update_events` is intentionally separate from the existing
`graph_projection_jobs` observation outbox. The latter remains the frozen
canonical-observation pipeline; this additive event outbox carries only a
typed reference to a case-scoped correlation proposition. PostgreSQL is the
authority. There is no distributed transaction and no raw evidence content
in the event or Neo4j payload.

`CorrelationSubmission` is the producer-facing internal model. In one
PostgreSQL transaction it derives `EvidencePathSnapshot`s from canonical
`worker_observations`, persists the proposition/candidates/snapshot, and
creates exactly one `correlation.upserted.v1` event. Missing observations or
observations outside the requested case reject the whole transaction.

## Replay and status semantics

Event and resource IDs use deterministic UUIDs from the case and producer
idempotency key. `projection_key` is a canonical SHA-256 identity over the
case, correlation, mapping version, and config version. A projection handler
must use it in its Neo4j `MERGE` identity. Duplicate durable submissions
return the same receipt only for an identical payload; a changed payload with
the same idempotency key is rejected.

`queued` means durable but not claimed (or safely requeued after a Neo4j
connection failure); `running` means a leased delivery; `succeeded` means the
handler completed; `deferred` is reserved for a safe dependency wait; and
`failed` is a terminal integrity/payload failure. A Neo4j outage never rolls
back the PostgreSQL proposition and has no automatic retry ceiling: it stays
replayable until a handler can project it. A missing referenced correlation
fails with a fixed safe code, never a driver exception or stack trace.

## Candidate and provenance rules

A correlation and every candidate link are reviewable propositions with
statuses `candidate`, `needs_review`, or `rejected`; they are never verified
facts, identity merges, guilt conclusions, or direct entity-to-entity edges.
Candidate-link endpoints are canonical observations, and their persisted
evidence paths retain case/evidence/observation IDs, source locator,
extractor version/config/model version, and observed temporal metadata.
Supporting and contradictory observation references are explicit. Missing
provenance is not fabricated.

Feature snapshots are optional immutable reproducibility records. They may
hold bounded upstream feature values only; secret-shaped keys are rejected.
Raw evidence bytes, object URIs, credentials, DSNs, and stack traces are not
accepted as a feature snapshot or graph-update payload.

## APIs and authorization seam

Read-only case-scoped endpoints, all using existing `require_graph_read`, are:

- `GET /api/v1/cases/{case_id}/graph/correlations`
- `GET /api/v1/cases/{case_id}/graph/correlations/{correlation_id}`
- `GET /api/v1/cases/{case_id}/graph/candidates`
- `GET /api/v1/cases/{case_id}/graph/hypotheses`

They expose durable data and the event projection status separately. The
hypotheses endpoint only exposes an upstream supplied `hypothesis_reference`;
this layer creates no hypothesis. There is intentionally no public mutable
CRUD API: producers call the internal repository/service seam until Aditya
defines an authorization policy for writes and reviews.

Phase 5B additionally records safe `case_access_granted` and
`case_access_denied` decisions with the request ID, principal reference, case
ID, and `graph_read` action. Correlation repository reads always retain the
authorized case predicate, including an internal outbox-event lookup; an event
UUID alone is not a cross-case lookup capability. PostgreSQL read outages are
reported as a fixed service-unavailable response, not a database error.

## Ownership boundary

Shreshtha owns correlation creation/scoring/ranking, link reasons,
hypothesis-generation semantics, and the registered correlation Neo4j handler.
That handler consumes `CorrelationProjectionContext` and implements its own
idempotent Cypher using `projection_key` (`intelligence/projection.py`).
Nipun owns the durable schema, idempotency, case/provenance validation,
outbox mechanics, and safe read surface. Aditya owns any new write/review
authorization policy; this work only reuses the existing graph-read
dependency.

**Resolved (Phase 5A reconciliation)**: the handler above now has a real,
reachable registration path -- `app/modules/graph/intelligence_worker.py
--replay-once`/`--replay-loop` compose it with `replay_graph_updates` in a
real process, mirroring `graph.worker`'s existing `--once`/`--loop` CLI
shape for the separate `graph_projection_jobs` outbox. Before this, the
handler existed and was unit-tested but nothing in `app/` ever invoked it
outside a test -- see `docs/qa/known-limitations.md` for the corrected
record of that gap.
