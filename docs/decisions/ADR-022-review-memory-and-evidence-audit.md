# ADR-022: Review memory, projection-replay outbox, and evidence audit routes

## Status

Accepted (Phase 7 Closure — WP-4).

## Context

The gap register (G3, G7, G13) identified three related shortfalls after
Phase 6/7:

- **G13**: candidate-review and hypothesis decisions were projected into
  Neo4j best-effort only, with no durable retry path if the projection
  attempt failed or Neo4j was unavailable at decision time.
- **G3**: there was no case-level narrative/notes mechanism, and no
  aggregate "handoff" view summarizing a case's open review work for an
  incoming investigator.
- **G7 (partial)**: `list_candidates_for_review`/`list_hypotheses` always
  returned rejected items, contradicting the documented behavior that "a
  rejected candidate disappears from default analytical reads"; there was
  no way to re-verify a stored evidence object's integrity or to force a
  reprocess of already-uploaded evidence without re-uploading it; the
  per-case audit trail had no read endpoint.

## Decisions

### A third, separate durable outbox for review/hypothesis projection

`review_hypothesis_projection_events` (migration `3c4d5e6f7a8b`) is a new,
minimal outbox — **not** a generalization of either existing outbox
(`graph_projection_jobs` for evidence→observation projection,
`graph_update_events` for Phase 5 correlation projection). Genericizing
either existing table was judged riskier than adding a third: Phase 5's
correlation-projection path is Gate-C-adjacent, and `graph_projection_jobs`
already has a specific claim/lease/retry contract tuned for worker-result
ingestion. The new table reuses the same `queued`/`running`/`succeeded`/
`failed`/`deferred` status vocabulary and claim-batch pattern (see
`app/modules/graph/models.py`'s `GraphProjectionJobStatus`) for operational
consistency, without touching either existing table's schema or code path.

`review_service.py`'s three decision/creation paths (`submit_candidate_
review_decision`, `create_hypothesis`, `submit_hypothesis_review_decision`)
now follow an enqueue-then-attempt-then-mark pattern: the outbox row is
written durably in the same transaction as the decision, a best-effort
synchronous projection attempt follows, and the outbox row is marked
succeeded/retryable on the outcome. `intelligence_worker.py --replay-
review-once` (a new CLI mode, mirroring the existing worker's other
`--*-once` modes) drains any rows left in `queued`/retryable `failed`
state — the same at-least-once, idempotent-replay posture as the two
existing outboxes.

### Case notes are append-only, not editable

`case_notes` (migration `4d5e6f7a8b9c`) reuses the same append-only
pattern as `entity_review_decisions`/`candidate_review_decisions`/
`integrity_events`: a `BEFORE UPDATE OR DELETE` trigger on the shared
`tracex_reject_integrity_record_mutation()` function. An "edit" is a new
row with `supersedes_note_id` set. Visibility is role-based, not a
separate ACL table: any author always sees their own notes;
`CASE_OWNER`/`CASE_MANAGER`/`REVIEWER` additionally see every note via the
new `CaseAction.CASE_NOTE_READ_ALL`.

### Handoff summary is computed, not stored

`graph/handoff_service.py`'s `build_handoff_summary()` aggregates existing
data (open candidates, open hypotheses, accepted/rejected counts, recent
notes) at read time — no new table. This mirrors the existing "effective
status computed at read time" pattern already used by
`candidate_review_view`/`entity_resolution_review_view`, and avoids a
second, potentially-stale source of truth for review state.

### Evidence integrity re-check streams, never buffers

`EvidenceLifecycleService.verify_evidence_integrity` opens the stored
object via `ObjectStorage.open_stream` and re-hashes it chunk-by-chunk —
consistent with the existing bounded-memory contract the upload path
already holds. A storage read failure raises `EvidenceIntegrityUnavailable
Error`, surfaced as `503`, not `200` with `matches: false` — an unreadable
object is a different failure mode than a tampered one and must not be
conflated with it.

### Reprocess extends the existing idempotency-key mechanism, not a new one

`POST /evidence/{id}/reprocess` requires a caller-supplied `Idempotency-
Key` header (like upload) but builds a **distinct** job key —
`f"{case_id}:{evidence_id}:{processor}:{version}:reprocess:{idempotency_
key}"` — so a reprocess job's key can never collide with the original
upload job's row. `EvidenceLifecycleRepository.create_job`/`get_job_by_
idempotency_key` are new, minimal repository methods for this
standalone-job-row case (`create_evidence_with_job` remains unchanged and
is still used only by the original upload path).

### `include_rejected` query parameter closes the G7 read-filtering gap

`list_candidates_for_review`/`list_hypotheses` now default to excluding
`REJECTED_BY_REVIEWER` items, with `include_rejected=true` available for
callers that need the full history. This is a read-time filter only —
rejected decisions remain durably stored and unmodified.

## Deferred

- **Hypothesis "contradicting" evidence.** `HypothesisCreateSubmission`/
  `HypothesisRecord` (`app/modules/graph/hypothesis_models.py`) carry only
  `supporting_observation_ids`/`supporting_candidate_ids`. Phase 5's
  separate `CorrelationSubmission`/`CorrelationRecord`
  (`integration_models.py`) already has `contradictory_observation_ids`,
  but hypotheses are a distinct, later-phase (Phase 6) concept built over
  reviewed candidates, not correlations. Adding a symmetric "contradicting"
  field to the hypothesis models would require a new migration column, a
  service/validation change (a hypothesis with only contradicting evidence
  should probably not be creatable), and new API/test coverage — judged
  out of this WP's scope given the size of everything else in WP-4. See
  `docs/qa/known-limitations.md` ("WP-4") for the explicit deferral.
- **`CANDIDATE_ASSOCIATION` wiring into Phase 5's projector** remains
  deferred from WP-3 for the same Gate-C-adjacency reason (unchanged by
  WP-4).

## Consequences

- Three durable outboxes now exist in the codebase
  (`graph_projection_jobs`, `graph_update_events`,
  `review_hypothesis_projection_events`), each narrow and independently
  replayable. A future WP could consider unifying them once Phase 5's
  Gate C freeze lifts, but that is explicitly not this WP's call to make.
- `case_notes` visibility logic lives in `notes_service.py`, not the
  database — consistent with this codebase's existing default-deny
  authorization posture (`policy.py`), but it means a future direct-SQL
  reader of `case_notes` sees every row unfiltered; callers must always go
  through the service.
