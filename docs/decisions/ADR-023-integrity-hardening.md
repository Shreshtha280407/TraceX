# ADR-023: Integrity hardening — reconciliation coverage, key registry, manifest archival, scheduled checkpointing

## Status

Accepted (Phase 7 Closure — WP-5).

## Context

The gap register's G4 ("integrity hardening") named four shortfalls left
after Phase 6 and the earlier gap-closure work packages:

- `IntegrityReconciliationService` covered evidence, correlations,
  structured/modality provenance, candidate-review decisions, and
  hypothesis actions -- but WP-2's entity-resolution decisions and WP-4's
  case notes had **zero** integrity coverage, not even a best-effort
  write-time attempt. A merged/split entity identity or a case note could
  vanish without any tamper-evident trace.
- Every checkpoint signature already stores its own signer's public key
  (`checkpoint_signatures.public_key_b64`), so verification already
  survives key rotation -- but there was no durable, independently
  auditable record of *which* `key_id`s have ever legitimately signed for
  this system, separate from trusting whatever `.env` says today.
- `export_verification_bundle` only ever returned bytes to a CLI caller
  (`export --out file.json`) or an HTTP response -- there was no durable,
  write-once archival path independent of PostgreSQL.
- Checkpoints were 100% manually triggered (`build-checkpoint --case-id
  ... --start-sequence ... --end-sequence ...`); nothing discovered which
  cases had un-sealed events or sealed them on a schedule.

## Decisions

### Entity-resolution decisions and case notes get the same integrity treatment as review/hypothesis decisions

`EntityReviewDecisionRecord.to_integrity_submission()` and `CaseNoteRecord.
to_integrity_submission()` are new methods, exactly mirroring `review_
models.CandidateReviewDecisionRecord.to_integrity_submission()`'s shape and
its "never the raw protected field" rule:

- An entity decision's `rationale` is reduced to `rationale_commitment_
  sha256` (reusing `entity_models.rationale_commitment`, itself a copy of
  `review_models.rationale_commitment`'s existing pattern) -- the raw
  rationale never reaches `canonical_metadata`.
- A case note's `text` is reduced to a `text_commitment_sha256` computed
  inline inside `to_integrity_submission()` from the in-memory record --
  no new persisted commitment column was needed, since the record already
  holds `.text` at the moment the submission is built (unlike rationale,
  which is cached as a column for a different, pre-existing reason).

Both are wired best-effort at their write path (`entity_api.py`'s
`submit_entity_resolution_review` route, `notes_service.create_note`),
using the same enqueue-after-durable-write, log-and-continue-on-failure
`_record_integrity_event_safely` pattern every other producer in this
codebase already uses -- copied locally into each module rather than
extracted into one shared helper, consistent with how `review_service.py`
and `evidence_lifecycle/service.py` each keep their own private copy
today (a deliberate non-abstraction: three near-identical five-line
functions cost less than a new cross-module coupling).

`IntegrityReconciliationService._durable_submissions` now additionally
scans `entity_review_decisions` and `case_notes` (bounded by the same
`remaining` budget as every other source), so a best-effort failure at
write time is always durably repairable later -- closing the coverage gap
completely, not just for the synchronous path.

New event kinds: `IntegrityEventKind.ENTITY_RESOLUTION_DECISION`,
`IntegrityEventKind.CASE_NOTE_ADDED`. Both are additive to a StrEnum that
is not a frozen `app/contracts/` V1 model, so this is a normal, safe
extension.

### `signing_keys_public` is an append-only registry, not a mutable "current key" table

Each `rotate-key` CLI invocation registers the *currently configured*
key's public identity as one new, immutable row (`key_id` is the primary
key). There is no `status`/`active` column and no UPDATE path at all: the
table carries the exact same `tracex_reject_integrity_record_mutation`
append-only trigger as `integrity_events`/`checkpoint_signatures`/
`case_notes`. Revocation is deliberately not modeled in this WP -- an
operator's only real action is "generate a new key, configure a new
`key_id`, register it"; a `key_id` reused for a genuinely different key
raises `SigningKeyConflictError` rather than silently overwriting, which
is the anomaly this table exists to catch. Rotation is two explicit CLI
steps on purpose (`generate-key` then `rotate-key`), matching `generate-
key`'s own existing "print, don't persist" contract -- the private key
never becomes something this module writes to a database.

### `ManifestSink` is write-once at the application level, not backed by real object-lock

`FilesystemManifestSink` uses `open(path, "xb")` (fails if the path
exists); `MinioManifestSink` checks-then-puts (a real TOCTOU race exists
under concurrent writers, acceptable for a rare, operator-invoked archive
action). Neither configures MinIO bucket versioning/object-lock/retention
-- that is an infrastructure-level bucket setting out of this module's
scope (see `docs/runbooks/local-development.md` for where that would be
documented if adopted). Both sinks store the *exact* bytes `export_
verification_bundle` already produces; `manifest_sink.py` never
constructs or validates that payload, only stores it, keeping "what is
safe to export" and "where it durably lives" as separate concerns.

### Scheduled checkpointing discovers pending work from the existing sequence counter, not a new table

`integrity_sequence_counters.last_sequence` (already maintained by every
`record_event` call, entirely pre-existing) compared against `MAX
(merkle_checkpoints.end_sequence)` per case is sufficient to answer "which
cases have events beyond their latest seal" -- no new column, no new
table. `IntegrityRepository.list_pending_checkpoint_ranges` computes this
with one query (`LEFT JOIN` + `GROUP BY`); `IntegrityService.build_
pending_checkpoints` seals each case in turn, propagating the first
failure rather than catching per-case (a signing-key failure would fail
identically for every case in the sweep, so failing fast and letting the
caller's backoff handle it is more useful than repeating the same error
N times). `cli.py checkpoint-loop` mirrors `graph.intelligence_worker.
replay_loop`'s shape exactly: bounded exponential backoff, a hard stop
after `_MAX_CONSECUTIVE_LOOP_FAILURES`, graceful SIGINT/SIGTERM shutdown
via an `asyncio.Event` -- a second, independent instance of a pattern this
codebase already trusts, not a shared abstraction (the two loops sweep
structurally different things: a durable outbox table vs. a computed
per-case range).

## Deferred / explicitly out of scope

- **Real MinIO object-lock/retention configuration.** `MinioManifestSink`'s
  write-once guarantee is application-level only (see above).
- **Signing-key revocation.** No `revoked_at`/`status` field; see above.
- **A shared `_record_integrity_event_safely` helper.** Left as four
  independent, module-local copies (evidence_lifecycle, graph/review_
  service, graph/entity_api, access_control/notes_service) --
  deliberately, per the non-abstraction reasoning above.

## Consequences

- Every write path that can affect the tamper-evident event log now has
  matching reconciliation coverage: evidence, correlations, structured/
  modality provenance, candidate-review decisions, hypothesis actions,
  entity-resolution decisions, and case notes.
- `signing_keys_public` gives an auditor a durable answer to "which keys
  has this deployment ever really used to sign checkpoints," independent
  of trusting the current `.env`.
- Verification bundles can now be durably archived outside PostgreSQL on
  an operator's own schedule (`archive`) or automatically as checkpoints
  are sealed (a future WP could wire `checkpoint-loop` to also archive
  each newly sealed checkpoint -- not done here, since G4 asked for the
  sink and the loop as separate capabilities, not their composition).
