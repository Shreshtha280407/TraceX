# Audit event catalog

Gap-Closure WP-6 (G8). The canonical enumeration of every `event_type`
value `app.modules.access_control.audit.record_audit_event`/
`record_audit_event_safely` are ever called with, defined in
`app/modules/access_control/audit_catalog.py` as `AuditEventType`.

Before this WP, `event_type` was an untyped free-text string with no
central registry — this table (and the enum backing it) is that registry.
`tests/unit/access_control/test_audit_event_catalog.py` statically greps
every `event_type` literal used across `app/` and fails if any call site
uses a value not listed below, or if any listed value has no real call
site — this table can never silently drift from the code.

**Correction**: an earlier version of this document guessed that the gap
register's "21-name event catalog" (G8) referred to this table. Having
since re-read the original gap-closure prompt in full, G8's "21 plan
§20.2 names" is a **different, broader catalog** — TraceX's internal
*domain* events (`graph.updated`, `checkpoint.sealed`, `review.completed`,
`worker.heartbeat`, etc.), not this table of *security-audit* event
types. See `docs/architecture/event-catalog.md` for that catalog. This
table (29 security-audit event types, `AuditEventType`) remains real and
still independently useful — a security-audit trail is a different,
narrower concern than the domain-event catalog — just not what G8 asked
for.

## Security audit events (`SecurityAuditEventRecord.event_type`)

| `event_type` | Module | Fires when |
|---|---|---|
| `admin.access_denied` | access_control | A non-admin caller attempts an admin-only action. |
| `admin.bootstrap_create_admin` | access_control | The `create-admin` CLI provisions the first/an additional system admin. |
| `admin.provision_user` | access_control | `POST /api/v1/admin/users` provisions a new user. |
| `auth.login.failure` | access_control | A login attempt fails (wrong password, unknown/inactive user). |
| `auth.login.rate_limited` | access_control | A login attempt is rejected by the login rate limiter. |
| `auth.login.success` | access_control | A login attempt succeeds. |
| `auth.logout` | access_control | A session is revoked via `POST /api/v1/auth/logout`. |
| `auth.refresh.denied` | access_control | A refresh-token exchange is rejected (invalid/expired/revoked). |
| `auth.refresh.rate_limited` | access_control | A refresh attempt is rejected by the refresh rate limiter. |
| `auth.refresh.reuse_detected` | access_control | A previously-rotated-out refresh token is replayed (possible theft) — revokes the whole token family. |
| `auth.refresh.success` | access_control | A refresh-token exchange succeeds and issues a new token pair. |
| `case.create` | access_control | `POST /api/v1/cases` creates a case. |
| `case.member_add` | access_control | `POST /api/v1/cases/{id}/members` adds a case member. |
| `case_access_denied` | access_control | `authorize_case_action` denies a case-scoped request (any module). |
| `case_access_granted` | access_control | (Reserved companion to `case_access_denied`; see call sites for current usage.) |
| `evidence.reprocess` | evidence_lifecycle | `POST .../evidence/{id}/reprocess` enqueues a reprocess job. |
| `evidence.upload` | evidence_lifecycle | `POST .../evidence` accepts an upload. |
| `integrity_operation` | integrity | A protected integrity verify/export/checkpoint operation runs via `integrity/api.py`. |
| `worker.media_chunk.accepted` | evidence_lifecycle | A worker's media-chunk publication is accepted. |
| `worker.media_manifest.created` | evidence_lifecycle | A worker's media manifest is created. |
| `worker.observation_batch.accepted` | evidence_lifecycle | A worker's observation batch submission is accepted. |
| `worker_authentication_denied` | evidence_lifecycle | `require_worker_principal` rejects a worker credential (missing/malformed/unknown/revoked). |
| `worker_credential_revoked` | access_control | An operator revokes a worker credential (CLI). |
| `worker_credential_rotated` | access_control | An operator rotates a worker credential (CLI). |
| `worker_job_access_denied` | evidence_lifecycle | An authenticated worker attempts a job it doesn't own or isn't scoped for. |
| `worker_job_lease_renewed` | evidence_lifecycle | A worker successfully renews its job lease (heartbeat). |
| `worker_job_reclaimed` | evidence_lifecycle | A job with an expired lease is reclaimed by another worker. |
| `worker_job_retry_exhausted` | evidence_lifecycle | A job's retry budget is exhausted and it moves to a terminal failed state. |
| `worker_processor_scope_denied` | evidence_lifecycle | A worker attempts to claim a job for a processor outside its `allowed_processor_names`. |

## Domain event types (`EventV1.event_type`, not audit events)

A **different concept**: the real-world event kind a projected `Event`
graph node represents (see `app/modules/graph/mapping.py`, `MEDIA_
MAPPING_VERSION`/`MAPPING_REGISTRY_VERSION`). Not security telemetry, and
deliberately excluded from `AuditEventType` and the drift test above.

| `event_type` | Produced from |
|---|---|
| `cdr_call` | A `cdr_call_record` observation. |
| `financial_transaction` | A `financial_transaction_record` observation. |
| `meeting_candidate` | A `meeting_candidate` observation (media/visual). |
| `message` | A `chat_message` observation. |
| `sighting` | An `object_detection`/`text_region_detection`/`anonymous_track_segment` observation. |
| `speech_segment` | A `transcript_segment`/`diarization_speaker_turn` observation. |

## Adding a new event type

1. Add the literal at its real call site (`record_audit_event(event_type="...", ...)`).
2. Add the matching member to `AuditEventType` in `audit_catalog.py`, with
   a one-line comment on when it fires.
3. Add a row to the table above.
4. `uv run pytest tests/unit/access_control/test_audit_event_catalog.py`
   fails until both are done — treat it as the checklist enforcer, not
   optional follow-up.
