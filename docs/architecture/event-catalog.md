# Event catalog

Gap-Closure re-close (G8): "one typed module with the 21 plan §20.2
names; map existing emitters (outbox/progress/integrity/audit events) to
catalog names; emit missing ones where an underlying action already
exists." Defined in `app/core/event_catalog.py` as `EventCatalogName`.

**On the original "21 plan §20.2 names"**: that section's exact text was
not available when this catalog was built — the gap-closure prompt
references it (`Event-name catalog (§20.2) mostly absent (only ~5 of 21
names exist)`, and separately lists 8 example names: `graph.updated`,
`motif.detected`, `checkpoint.signed`, `integrity.failed`, `review.
completed`, `audit.appended`, `worker.heartbeat`, `worker.unavailable`,
`entity.*`) but does not reproduce the full list. The 21 names below were
built bottom-up from this codebase's own real, existing emission points
instead of top-down from an unavailable specification. All 8 named
examples from the prompt are present; the remaining 13 were chosen to
cover every other genuinely distinct internal action already durably
recorded or computed in this codebase.

This is a **naming** exercise, not a new message bus, queue, or pub/sub
layer — explicitly out of scope. Every event this codebase already
durably records or computes keeps recording/computing it exactly as
before; this catalog only gives each one a single, stable name.

## The 21 names

| Catalog name | Category | Real emission |
|---|---|---|
| `evidence.registered` | Evidence | `IntegrityEventKind.EVIDENCE_REGISTERED` |
| `evidence.reprocess_requested` | Evidence | `AuditEventType.EVIDENCE_REPROCESS` |
| `observation.published` | Evidence | `IntegrityEventKind.OBSERVATION_PUBLISHED` |
| `graph.updated` | Graph | A successful Neo4j projection write via `graph_update_events` or `review_hypothesis_projection_events` |
| `graph.projection_failed` | Graph | A projection attempt failed, left queued for durable retry |
| `correlation.completed` | Graph | `IntegrityEventKind.CORRELATION_COMPLETED` |
| `motif.detected` | Graph | **Reserved, not yet emitted** — see below |
| `review.completed` | Review | `IntegrityEventKind.REVIEW_DECISION` |
| `hypothesis.proposed` | Review | `IntegrityEventKind.HYPOTHESIS_ACTION` where `HypothesisActionKind.CREATED` |
| `hypothesis.reviewed` | Review | `IntegrityEventKind.HYPOTHESIS_ACTION` where accepted/rejected |
| `entity.candidate_generated` | Entity | **Reserved, not yet emitted** — see below |
| `entity.resolution_reviewed` | Entity | `IntegrityEventKind.ENTITY_RESOLUTION_DECISION` |
| `case_note.appended` | Case notes | `IntegrityEventKind.CASE_NOTE_ADDED` |
| `checkpoint.sealed` | Integrity | `integrity/service.py::build_checkpoint`'s `logger.info("checkpoint.sealed", ...)` on a genuinely new (non-replayed) checkpoint |
| `checkpoint.signed` | Integrity | Same emission as `checkpoint.sealed` (checkpoint + signature are always created together) |
| `integrity.failed` | Integrity | `logger.warning("integrity.event_record_failed", ...)`, several module-local `_record_integrity_event_safely` helpers |
| `worker.heartbeat` | Worker | A state change, not a log line: `worker_credentials.last_seen_at` touched on every successful worker authentication |
| `worker.unavailable` | Worker | A live classification, not a log line: `worker_liveness_status` computed as `stale`/`never_seen` |
| `worker.credential_rotated` | Worker | `AuditEventType.WORKER_CREDENTIAL_ROTATED` |
| `worker.credential_revoked` | Worker | `AuditEventType.WORKER_CREDENTIAL_REVOKED` |
| `audit.appended` | Audit | Every other `AuditEventType` member (26 of the 29 — the generic fallback) |

## Enforcement

`tests/unit/test_event_catalog.py` is this catalog's real guarantee, not
just its documentation:

- Every `IntegrityEventKind` member (except `HYPOTHESIS_ACTION`, resolved
  separately by `HypothesisActionKind`) has a static 1:1 mapping — a new
  `IntegrityEventKind` member added without updating the mapping fails
  the test immediately.
- Every `AuditEventType` member resolves to a real catalog name (specific
  or the generic fallback) — same drift protection.
- Every one of the 21 catalog names is accounted for somewhere: the
  enum-backed mappings, the documented log-event mapping, the
  shared/state-based emissions registry, or the explicit
  not-yet-emitted reservation. None can be silently orphaned.

## Reserved, not yet emitted

Two names this codebase cannot yet honestly back with a real emission —
documented in `RESERVED_NOT_YET_EMITTED`, never fabricated:

- **`motif.detected`** — `GET /cases/{id}/motifs` computes co-participation
  motifs live, at read time, from Neo4j. There is no durable "a motif was
  detected" action to hook an emission to; emitting this would mean
  durably recording every read, a different and much larger design
  decision than naming an event.
- **`entity.candidate_generated`** — candidate *generation* (as opposed
  to the human *review decision* on one, which is `entity.
  resolution_reviewed`) was never wired through the `record_integrity_
  event` facade. Generation is a re-derivable, idempotent computation
  over already-durable observations, unlike a human decision — reserved
  for a future WP to decide whether it deserves its own audit trail.

## Adding a new catalog name

1. Confirm a real underlying action exists (or is being added in the
   same change) — never add a name with nothing behind it.
2. Add the member to `EventCatalogName` in `app/core/event_catalog.py`.
3. Wire it into one of: `INTEGRITY_EVENT_KIND_TO_CATALOG` (if it's a new
   `IntegrityEventKind`), `_AUDIT_EVENT_TYPE_TO_SPECIFIC_CATALOG_NAME`
   (if it's a new specific `AuditEventType`), `DOCUMENTED_LOG_EVENT_
   MAPPING` (if it's a real structlog `event=` string), or
   `SHARED_OR_STATE_BASED_EMISSIONS` (if it's a state change with no
   dedicated log line).
4. `uv run pytest tests/unit/test_event_catalog.py` fails until every
   name is accounted for and every enum member is mapped — treat it as
   the checklist enforcer.
