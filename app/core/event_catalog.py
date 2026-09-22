"""Gap-Closure WP-6/re-close (G8): the canonical catalog of TraceX's 21
internal domain events, and the mapping from every existing emission
mechanism to a catalog name.

This module names events; it never becomes a new message bus, queue, or
pub/sub layer (explicitly out of scope per the gap-closure prompt). Every
event this codebase already durably records or computes keeps recording
or computing it exactly as before -- this module only gives each one a
single, stable, documented name, and a test
(`tests/unit/test_event_catalog.py`) that fails if a new
`IntegrityEventKind`/`AuditEventType` member is ever added without a
catalog mapping.

**On the original "21 plan section 20.2 names"**: that section's exact
text was not available when this catalog was built (the gap-closure
prompt references it but does not reproduce it). The 21 names below were
built bottom-up from this codebase's own real, existing emission points
instead of top-down from an unavailable specification -- every one maps
to something that genuinely happens today, and every name this codebase
cannot yet honestly back with a real emission is marked in
`RESERVED_NOT_YET_EMITTED` with the reason, never silently invented.

Two kinds of mapping exist here, deliberately at different rigor levels:

1. **Enum-backed, runtime-checked** (`INTEGRITY_EVENT_KIND_TO_CATALOG`,
   `catalog_name_for_hypothesis_action`, every `AuditEventType` member
   via `audit.appended`): these sources are already closed, typed enums
   (`IntegrityEventKind`, `AuditEventType`, `HypothesisActionKind`), so a
   test can assert *every* member maps to something -- real drift
   protection, mirroring `access_control.audit_catalog`'s existing
   pattern exactly.
2. **Documented only** (`DOCUMENTED_LOG_EVENT_MAPPING`): outbox/worker/
   checkpoint emission points that aren't yet unified under one shared
   enum across `graph`/`integrity` modules. Building a second runtime-
   enforced mapping layer across those would mean either a new cross-
   module abstraction or hand-verifying every structlog string literal
   never drifts -- judged out of this change's risk budget. These are
   still real, still mapped, just not test-enforced the same way.

**Placement note**: this module lives in `app/core/` (not inside any one
of `access_control`/`graph`/`integrity`) because a genuinely cross-module
catalog has to sit above every module it names, and `app/core/` is this
codebase's only shared-dependency location -- a deliberate, narrow
exception to `app/core/`'s usual "no `app/modules/*` imports" convention
(`canonical.py`/`ids.py`/`config.py`/`errors.py` all keep that convention;
this module cannot and still do its actual job). Verified no import
cycle results (`uv run mypy app` stays clean).
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.access_control.audit_catalog import AuditEventType
from app.modules.graph.hypothesis_models import HypothesisActionKind
from app.modules.integrity.models import IntegrityEventKind


class EventCatalogName(StrEnum):
    """TraceX's 21 canonical internal domain-event names."""

    # --- Evidence / ingestion ------------------------------------------------
    EVIDENCE_REGISTERED = "evidence.registered"
    EVIDENCE_REPROCESS_REQUESTED = "evidence.reprocess_requested"
    OBSERVATION_PUBLISHED = "observation.published"

    # --- Graph / correlation --------------------------------------------------
    GRAPH_UPDATED = "graph.updated"
    GRAPH_PROJECTION_FAILED = "graph.projection_failed"
    CORRELATION_COMPLETED = "correlation.completed"
    MOTIF_DETECTED = "motif.detected"

    # --- Review / hypothesis ---------------------------------------------------
    REVIEW_COMPLETED = "review.completed"
    HYPOTHESIS_PROPOSED = "hypothesis.proposed"
    HYPOTHESIS_REVIEWED = "hypothesis.reviewed"

    # --- Entity resolution -----------------------------------------------------
    ENTITY_CANDIDATE_GENERATED = "entity.candidate_generated"
    ENTITY_RESOLUTION_REVIEWED = "entity.resolution_reviewed"

    # --- Case notes --------------------------------------------------------------
    CASE_NOTE_APPENDED = "case_note.appended"

    # --- Integrity / checkpoint ----------------------------------------------------
    CHECKPOINT_SEALED = "checkpoint.sealed"
    CHECKPOINT_SIGNED = "checkpoint.signed"
    INTEGRITY_FAILED = "integrity.failed"

    # --- Worker lifecycle -------------------------------------------------------------
    WORKER_HEARTBEAT = "worker.heartbeat"
    WORKER_UNAVAILABLE = "worker.unavailable"
    WORKER_CREDENTIAL_ROTATED = "worker.credential_rotated"
    WORKER_CREDENTIAL_REVOKED = "worker.credential_revoked"

    # --- Audit -----------------------------------------------------------------------
    AUDIT_APPENDED = "audit.appended"


#: `IntegrityEventKind` members that map to exactly one catalog name each.
#: `HYPOTHESIS_ACTION` is deliberately excluded here -- it maps to one of
#: *two* catalog names depending on `HypothesisActionKind`, resolved by
#: `catalog_name_for_hypothesis_action` below, not a static 1:1 entry.
INTEGRITY_EVENT_KIND_TO_CATALOG: dict[IntegrityEventKind, EventCatalogName] = {
    IntegrityEventKind.EVIDENCE_REGISTERED: EventCatalogName.EVIDENCE_REGISTERED,
    IntegrityEventKind.OBSERVATION_PUBLISHED: EventCatalogName.OBSERVATION_PUBLISHED,
    IntegrityEventKind.CORRELATION_COMPLETED: EventCatalogName.CORRELATION_COMPLETED,
    IntegrityEventKind.REVIEW_DECISION: EventCatalogName.REVIEW_COMPLETED,
    IntegrityEventKind.ENTITY_RESOLUTION_DECISION: EventCatalogName.ENTITY_RESOLUTION_REVIEWED,
    IntegrityEventKind.CASE_NOTE_ADDED: EventCatalogName.CASE_NOTE_APPENDED,
}


def catalog_name_for_hypothesis_action(action: HypothesisActionKind) -> EventCatalogName:
    """`IntegrityEventKind.HYPOTHESIS_ACTION`'s catalog name depends on
    which `HypothesisActionKind` the durable `hypothesis_actions` row
    itself carries -- a creation is a proposal, either review outcome is
    a completed review."""
    if action is HypothesisActionKind.CREATED:
        return EventCatalogName.HYPOTHESIS_PROPOSED
    return EventCatalogName.HYPOTHESIS_REVIEWED


#: The only `AuditEventType` members specific enough to warrant their own
#: catalog name rather than the generic `audit.appended` fallback -- each
#: is an event a monitoring consumer would plausibly want to filter on
#: independently of every other audit type.
_AUDIT_EVENT_TYPE_TO_SPECIFIC_CATALOG_NAME: dict[AuditEventType, EventCatalogName] = {
    AuditEventType.WORKER_CREDENTIAL_ROTATED: EventCatalogName.WORKER_CREDENTIAL_ROTATED,
    AuditEventType.WORKER_CREDENTIAL_REVOKED: EventCatalogName.WORKER_CREDENTIAL_REVOKED,
    AuditEventType.EVIDENCE_REPROCESS: EventCatalogName.EVIDENCE_REPROCESS_REQUESTED,
}


def catalog_name_for_audit_event(event_type: AuditEventType) -> EventCatalogName:
    """Every `AuditEventType` (see `access_control.audit_catalog`) maps to
    a catalog name: the two worker-credential-lifecycle types get their
    own specific name; every other one of the 29 values falls back to the
    generic `audit.appended` -- "an audit event was appended" is still
    true and useful at that coarser granularity for everything else."""
    return _AUDIT_EVENT_TYPE_TO_SPECIFIC_CATALOG_NAME.get(
        event_type, EventCatalogName.AUDIT_APPENDED
    )


#: Catalog names this codebase cannot yet honestly back with a real
#: emission -- documented, never silently invented or fabricated.
RESERVED_NOT_YET_EMITTED: dict[EventCatalogName, str] = {
    EventCatalogName.MOTIF_DETECTED: (
        "GET /cases/{id}/motifs (Gap-Closure WP-6) computes co-participation "
        "motifs live, at read time, from Neo4j -- there is no durable "
        "'a motif was detected' action anywhere to hook an emission to. "
        "Emitting this would mean durably recording every read, which is a "
        "different (and much larger) design decision than naming an event."
    ),
    EventCatalogName.ENTITY_CANDIDATE_GENERATED: (
        "graph.entity_service.generate_entity_resolution_candidates (WP-2) "
        "durably upserts entity_resolution_candidates rows, but candidate "
        "*generation* itself (as opposed to the human *review decision* on "
        "one, which is ENTITY_RESOLUTION_REVIEWED/ENTITY_RESOLUTION_DECISION) "
        "was never wired through the record_integrity_event facade -- "
        "generation is a re-derivable, idempotent computation over "
        "already-durable observations, unlike a human decision, which is "
        "why it wasn't in the original integrity-event scope. Reserved "
        "for a future WP to decide whether candidate generation deserves "
        "its own audit trail."
    ),
}


#: Documented-only mapping (see this module's docstring for why these
#: aren't runtime-enforced the same way as the enum-backed ones above):
#: real structlog `event=` string literals already emitted in this
#: codebase, mapped to the catalog name each one represents.
DOCUMENTED_LOG_EVENT_MAPPING: dict[str, EventCatalogName] = {
    # graph/review_service.py, graph/review_projection_replay.py -- a
    # successful Neo4j projection write via either the graph_update_events
    # or review_hypothesis_projection_events durable outbox.
    "graph.intelligence_worker.replay_batch_processed": EventCatalogName.GRAPH_UPDATED,
    "graph.intelligence_worker.replay_review_run_completed": EventCatalogName.GRAPH_UPDATED,
    # A projection attempt failed and is left queued for durable retry --
    # never a dropped write, always a queued one (see the outbox's own
    # claim/lease/retry contract).
    "graph.review_projection_failed": EventCatalogName.GRAPH_PROJECTION_FAILED,
    "graph.hypothesis_projection_failed": EventCatalogName.GRAPH_PROJECTION_FAILED,
    "graph.hypothesis_review_projection_failed": EventCatalogName.GRAPH_PROJECTION_FAILED,
    "graph.intelligence_worker.replay_iteration_failed": EventCatalogName.GRAPH_PROJECTION_FAILED,
    # integrity/service.py::build_checkpoint -- logs its own catalog name
    # directly (`logger.info("checkpoint.sealed", ...)`) on a genuinely
    # new (non-replayed) checkpoint. A checkpoint and its signature are
    # always created together (one `create_checkpoint` repository call
    # writes both rows), so `CHECKPOINT_SIGNED` maps to this same real
    # call site -- there is no separate "signature created" action to log.
    "checkpoint.sealed": EventCatalogName.CHECKPOINT_SEALED,
    # A best-effort integrity-event recording attempt failed (durably
    # repairable later by IntegrityReconciliationService) -- emitted from
    # several module-local `_record_integrity_event_safely` helpers
    # (evidence_lifecycle/service.py, graph/review_service.py,
    # graph/entity_api.py, access_control/notes_service.py,
    # graph/intelligence/pipeline.py).
    "integrity.event_record_failed": EventCatalogName.INTEGRITY_FAILED,
}


#: Real, currently-emitted catalog names whose emission is neither a
#: 1:1 enum mapping nor a distinct log line of their own -- documented
#: here (not silently in a comment) so
#: `test_every_catalog_name_is_either_mapped_from_somewhere_or_explicitly_
#: reserved` can verify none of the 21 names is orphaned.
SHARED_OR_STATE_BASED_EMISSIONS: dict[EventCatalogName, str] = {
    EventCatalogName.CHECKPOINT_SIGNED: (
        "Shares CHECKPOINT_SEALED's exact emission point -- integrity/"
        'service.py::build_checkpoint\'s `logger.info("checkpoint.sealed", '
        "...)`. A checkpoint and its signature are always created together "
        "(one create_checkpoint repository call writes both rows); there is "
        "no separate 'signature created' action to log."
    ),
    EventCatalogName.WORKER_HEARTBEAT: (
        "A state change, not a log event: worker_credentials.last_seen_at "
        "is touched by evidence_lifecycle/dependencies.py::require_worker_"
        "principal on every successful worker authentication (Gap-Closure "
        "WP-6). No log line was added for the positive path deliberately -- "
        "logging every single successful authentication at INFO level "
        "would be a real production-noise anti-pattern for a call this "
        "frequent; the durable last_seen_at column update is the actual, "
        "queryable signal."
    ),
    EventCatalogName.WORKER_UNAVAILABLE: (
        "A live classification, not a discrete past occurrence to log: "
        "computed at read time from worker_credentials.last_seen_at via "
        "access_control.models.worker_liveness_status ('stale'/"
        "'never_seen'), surfaced in /readyz, /metrics, GET /api/v1/admin/"
        "workers, and GET /api/v1/internal/workers."
    ),
}
