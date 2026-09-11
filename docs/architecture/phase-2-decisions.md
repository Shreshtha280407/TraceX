# Phase 2 Decisions — Nipun Evidence Lifecycle Foundation

Record of the concrete choices made building the evidence-lifecycle foundation, and the reasoning behind each. Mirrors `docs/architecture/phase-1-decisions.md`'s format.

## Reused, not duplicated

Before writing any code, the existing Phase 1 foundation was inspected and reused directly rather than re-invented:

- **Case/membership model, authentication, and authorization**: `app.modules.access_control.dependencies.require_case_action`/`require_evidence_read` (already exported, already documented as "the integration points Nipun's later case/evidence endpoints ... depend on") gate every endpoint. No new case CRUD, no new auth flow, no new RBAC/ABAC policy.
- **Audit logging**: `app.modules.access_control.audit.record_audit_event` is called directly from `api.py` on a successful upload. Denied attempts are *not* separately audit-logged by this module, because `require_case_action`'s dependency itself (Aditya's code) doesn't call `record_audit_event` on denial — that's a pre-existing architectural gap common to every case-scoped endpoint, not something introduced or fixable here without editing another contributor's module. Flagged in `docs/qa/known-limitations.md` for team review.
- **Error envelope, correlation IDs, security headers**: every raised error is a plain `HTTPException`; `app/core/errors.py`'s existing handlers build the safe envelope. No custom error response shape was introduced.
- **SQLAlchemy Core / Alembic conventions**: `app/modules/evidence_lifecycle/repository.py` hand-writes `sa.Table` objects matching the migration exactly, the identical pattern `app/modules/access_control/repository.py` already established (`migrations/env.py`'s `target_metadata = None` is unchanged, deliberately).
- **MinIO client construction**: `MinioObjectStorage` constructs its `Minio(...)` client and offloads every sync call via `asyncio.to_thread`, identical to `app.dependencies.services.check_minio`.
- **`SourceResolver` protocol**: `MinioSourceResolver` implements `structured_processing.models.SourceResolver`'s shape (`read_bytes(object_uri) -> bytes`) structurally — no import of `structured_processing` (or any sibling module) was added, preserving the existing module-boundary rule.

## Genuinely new (this module's own responsibility)

- `app/modules/evidence_lifecycle/` — `models.py`, `errors.py`, `routing.py`, `schemas.py`, `storage.py`, `jobs.py`, `repository.py`, `service.py`, `dependencies.py`, `api.py`.
- `migrations/versions/f2086e1e89f6_evidence_lifecycle_foundation.py` — `evidence_records`, `worker_jobs`.
- `MAX_EVIDENCE_BYTES` setting.
- `python-multipart` (new dependency — required by FastAPI for `File`/`Form` multipart parsing; genuinely absent, confirmed via `uv run python -c "import multipart"` failing before this task).

## `source_type` is client-declared, never inferred from content

Every other extraction module in this codebase (`structured_processing`, `communication_processing`) already treats "never guess ambiguous input" as a hard rule for its own domain. Evidence lifecycle applies the same rule one layer up: a CSV file could legitimately be a CDR export, a financial export, or unrelated tabular data, and nothing about its bytes alone disambiguates that safely. The client (an authorized case member who knows what they're uploading) declares `source_type` explicitly as a required form field; the server only validates it against a small allow-list of accepted content types per source type (`routing.py`), never infers it.

## One canonical processor per `source_type`, not per-profile classification

`routing.ROUTING` maps each `source_type` to exactly one `(processor_name, processor_version)` — the same "default/generic" profile name each downstream module's own worker already recognizes (`fir_report_text_v1`, `cdr_generic_v1`, `financial_transaction_generic_v1`, `audio_metadata_v1`, `generic_social_json_v1`, `media_metadata_v1`). Choosing *between* multiple valid profiles for the same source type (e.g. a specific WhatsApp/Telegram/Instagram chat-export shape, or a FIR-report vs. a fallback tabular document) requires reading the actual bytes and classifying their shape — logic that belongs to, and already exists in, the owning processing module. Evidence lifecycle's job is coarse routing at ingestion time, not content classification.

## `source_type=other` has no registered processor and is rejected at upload

`SourceType.OTHER` is the frozen contract's deliberate escape hatch for an unanticipated modality (see `docs/architecture/phase-1-decisions.md`). No processing module in this repository handles it. Rather than inventing a fictitious processor assignment, uploads declaring `source_type=other` are rejected (`422`, `UnsupportedSourceTypeError`) until a later phase defines a real processor for it. This is a documented, honest limitation, not a silent gap.

## Evidence and its job are created in one PostgreSQL transaction

`EvidenceLifecycleRepository.create_evidence_with_job` inserts both rows inside a single `engine.begin()` block. An `evidence_records` row without a corresponding `worker_jobs` row (or vice versa) is therefore structurally impossible — a mid-transaction failure rolls back both, and the caller's cleanup logic only ever has to reason about "did the transaction commit," not "which of the two rows landed." A direct consequence: `EvidenceRecordV1.processing_status` starts at `queued`, never `uploaded` — there is no distinct intermediate state to observe when both rows are created together.

## Idempotency-Key conflict detection compares `(source_type, content_type, sha256)`, not the whole record

A replayed upload is recognized as "the same logical request" if these three values match the row already stored under that `(case_id, upload_idempotency_key)`. `sha256` alone would be nearly sufficient (content identity), but a client re-sending the same bytes with a different declared `source_type`/`content_type` is a meaningfully different request (it would route to a different processor) and must not be silently treated as a cache hit.

## Race-losing uploads are cleaned up by content comparison, not by transaction order

When two requests with the same `Idempotency-Key` race past the read-then-check pre-check simultaneously, the loser's `INSERT` fails with `IntegrityError` inside its own transaction (which also rolls back its `worker_jobs` insert). The service re-reads whichever row actually won, deletes only the *loser's* just-written MinIO object (identified by the loser's own locally-known object key, never guessed), and compares content to decide replay vs. conflict — the same logic the non-race path already uses.

## `WorkerJobV1` publication failure never fails the upload request

Redis is treated as a best-effort notification layer on top of the durable `worker_jobs` row, not a required dependency for upload success. If `RedisJobProducer.publish` raises, the service logs a structured `evidence.job.dispatch_deferred` warning and returns the successful upload result anyway — the row's `dispatched_at IS NULL` is itself the durable "not yet confirmed dispatched" signal a future redrive mechanism (out of scope for this phase) would query for. This mirrors the same fail-open-on-non-critical-path philosophy `check_minio`/`check_redis` already apply to `/readyz` (report unavailability; don't crash the request path that depends on it only loosely).

## `_dispatch` returns the (possibly updated) job record, not the one built before publishing

Caught during live-Compose verification: the first implementation returned the in-memory `WorkerJobRecord` built *before* calling `_dispatch`, so a same-request successful publish never showed up in the API response's `dispatched_at` field (it would only appear on a later `GET /jobs/{job_id}`, which re-reads from PostgreSQL). Fixed by having `_dispatch` return an updated copy (`model_copy(update={"dispatched_at": ...})`) on success, used to build the returned `UploadOutcome`. See `docs/qa/test-results.md` for how this was caught.

## `object_uri` is deliberately excluded from every API response

Even though it carries no credential (it's just `cases/{case_id}/evidence/{evidence_id}/original`, a MinIO object key that requires real credentials to resolve), it is the kind of identifier a caller could try to use directly against MinIO. Excluding it entirely from `EvidenceView` is the more conservative reading of "do NOT add unrestricted raw-evidence-download APIs" than "presigned URLs are the only thing to avoid."

## Open questions for team review

- Denied-access-attempt auditing for case-scoped endpoints is a pre-existing gap in `require_case_action` (Aditya's module), not something this task fixed — see `docs/qa/known-limitations.md`.
- No automatic redrive of `worker_jobs` rows with `dispatched_at IS NULL` exists yet; a later phase should decide whether that's a scheduled sweep, a manual admin action, or built into the eventual consumer's startup.
- `routing.ROUTING`'s one-processor-per-source-type mapping will need revisiting once a real orchestrator needs to choose between multiple valid profiles for the same source type (e.g. a specific chat-export platform) at ingestion time rather than at processing time.

---

# Phase 2.1 Decisions — Nipun Worker Claim and Result-Submission Integration

Full design reasoning lives in `docs/architecture/worker-job-lifecycle.md` (the dedicated document this phase's task brief required); this section records the handful of decisions worth cross-referencing from here.

## Reused, not duplicated (Phase 2.1)

- **Claim-token generation/hashing**: `secrets.token_urlsafe(32)` + SHA-256 hex, the exact primitives and reasoning `access_control.tokens.generate_refresh_token`/`hash_refresh_token` already established for "a high-entropy secret shown once, only its hash persisted." Not imported directly (would cross a module boundary for a two-line stdlib pattern) — duplicated as a documented pattern, the same precedent `_dump_for_insert` already set between `access_control.repository` and `evidence_lifecycle.repository`.
- **Race-safe duplicate handling**: `worker_results.job_id`'s unique constraint + catching `sqlalchemy.exc.IntegrityError` is the identical technique `create_evidence_with_job`'s `Idempotency-Key` race already uses — not a new pattern.
- **Canonical hashing for idempotency comparison**: `app.core.canonical.canonical_sha256`, Phase 1's own utility, used exactly as its docstring anticipated ("a utility for future integrity work").
- **Audit logging**: `record_audit_event`, called for an accepted (non-replay) result submission with `user_id=None` — a worker is not a human user, and the function's `user_id` parameter is already nullable for exactly this kind of caller.

## Genuinely new (Phase 2.1)

- `worker_results`, `worker_observations` tables + four additive columns on `worker_jobs` (`claimed_at`, `lease_expires_at`, `claimed_by`, `claim_token_hash`), migration `102857ca8d1d`.
- `EvidenceLifecycleRepository.claim_job`/`get_job_by_id`/`get_result_for_job`/`list_observations_for_result`/`count_observations_for_job`/`submit_result`.
- `EvidenceLifecycleService.claim_job`/`submit_result`.
- `require_worker_principal` + `Settings.worker_shared_secret`/`worker_lease_seconds`.
- `app/modules/evidence_lifecycle/internal_api.py` — `/api/v1/internal/worker-jobs/{claim,{job_id}/result}`.
- No new dependency: everything above is stdlib (`secrets`, `hashlib`, `hmac`) plus already-approved SQLAlchemy/FastAPI/Pydantic features (`FOR UPDATE SKIP LOCKED` is a SQLAlchemy Core method call, not a new library).

## Why `worker_results`/`worker_observations` are new tables, not columns bolted onto `worker_jobs`

A job can be claimed and its lease can expire more than once (each reclaim is a new attempt), but at most one attempt ever produces the accepted, canonical result. Modeling the result as its own table with a `job_id` unique constraint makes "exactly one accepted result per job, ever" a database-enforced fact rather than an application convention — and gives a durable, independent home for the full submitted payload (`canonical_payload`) that would be awkward to bolt onto the job row without duplicating most of its own columns.

## Why `get_job_by_id` is unscoped by `case_id`

Every case-scoped, human-facing query in this module takes `case_id` as a mandatory filter (see `docs/architecture/evidence-lifecycle.md`'s "Case-scoping rule"). The internal worker endpoints deliberately do not follow that rule for job lookups, because a worker's authorization model is different in kind: it authenticates by holding a valid claim token for one specific `job_id`, not by case membership. `get_job_by_id` is the one place this module intentionally looks up a job without a `case_id` filter — restricted to `internal_api.py`, which is itself gated by `require_worker_principal` and never reachable from the case-scoped router.

## Security test's forbidden-contract list updated for this module's new, legitimate scope

`tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`'s `_FORBIDDEN_CONTRACT_IMPORTS` previously forbade importing `ObservationV1`/`WorkerResultV1` at all, on the Phase 2 assumption that this module only ever *produces* `WorkerJobV1`. Phase 2.1 legitimately needs to *validate and persist* a worker-submitted `WorkerResultV1` (and the `ObservationV1`s inside it) — the frozen contract's own Pydantic validators are the enforcement mechanism the task brief explicitly requires ("Validate the submitted `WorkerResultV1` using the frozen Pydantic contract before persistence"). The forbidden set was narrowed to `{EntityV1, EventV1, WorkerProgressV1}` — this module still never touches entity/event resolution or worker-progress reporting, and it never *fabricates* an `ObservationV1`/`WorkerResultV1` from raw evidence content itself (that remains extraction-module territory) — only re-validates and round-trips what a worker submitted.

## `worker_shared_secret` normalizes a blank value to `None` (real bug caught before this record)

Found while wiring `WORKER_SHARED_SECRET` into `compose.yaml`'s `api` service, before ever committing the passthrough: `docker compose`'s `${VAR}` substitution (no default) resolves an **unset** variable to an **empty string** inside the container, not an absent one — and pydantic-settings treats an environment variable's mere *presence* as "provided" regardless of its contents, so `Settings.worker_shared_secret` would have resolved to `SecretStr('')`, not `None`. `require_worker_principal`'s fail-closed check is `if configured_secret is None: raise 503` — an empty-but-not-`None` secret would skip that branch entirely and proceed to `hmac.compare_digest(token, "")`, which returns `True` for an empty submitted token (e.g. a request literally carrying `Authorization: Bearer ` with nothing after it) — a real authentication bypass on the internal worker endpoints in exactly the deployment configuration (compose, secret unset) this task's own "fails closed outside tests" requirement was meant to cover. Fixed with a `field_validator` on `worker_shared_secret` that normalizes any blank/whitespace-only value to `None` at the `Settings` level (so every caller gets the fix, not just this one passthrough), plus a redundant defense-in-depth emptiness check in `require_worker_principal` itself. Regression test: `tests/unit/test_config.py::test_blank_worker_shared_secret_normalizes_to_none`.

## Open questions for team review (Phase 2.1)

- No real per-worker credential system exists — `require_worker_principal` is a narrow shared-secret stand-in. See "Worker identity" in `docs/architecture/worker-job-lifecycle.md` for the exact Aditya handoff this implies.
- No max-attempt cutoff — a job with a perpetually-expiring lease is reclaimable forever. A later phase should decide the policy (likely a small addition to the claim query's eligibility condition).
- No automatic redrive of `deferred`/`cancelled` jobs.
- Denied worker actions (bad/expired/wrong claim token, scope mismatch, conflict) are not audit-logged, mirroring the same pre-existing gap already documented for case-scoped user endpoints.

---

# Phase 2.2 Decisions — Nipun Secure Worker Evidence Delivery

Full design and the revised security boundary live in `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section and `docs/architecture/structured-processing-worker.md`'s "Input-access boundary" section (updated in place, not superseded, since this phase closes the exact gap that section documented). This section records the decisions worth cross-referencing from here.

## Why this revises, rather than violates, "no raw-evidence-download API"

Phase 2's `docs/architecture/evidence-lifecycle.md` documented "no raw-evidence-download API" as a *user-facing/generic-caller* boundary — the concern it names explicitly is an object key or presigned URL a caller could resolve directly against MinIO, bypassing the API as the credential holder. `GET /api/v1/internal/worker-jobs/{job_id}/input` doesn't do that: it never returns an object key, bucket name, MinIO endpoint, or presigned URL — the API remains the only thing that ever holds a MinIO credential, and it streams the exact bytes through itself. What's new is *who* can trigger that stream and *when*: a worker that has actively claimed a specific job, only while that job's claim/lease is live, authenticated by the same `require_worker_principal` boundary plus the same per-job claim token `/result` already requires. This is authorized to a worker performing the job it was assigned, not to "anyone with the shared secret" and not to a case-scoped human caller — a narrower grant than either existing access path, not a wider one.

## Reused, not duplicated (Phase 2.2)

- **Claim-token verification**: `get_claimed_evidence_input` reuses `_hash_claim_token` and the identical unconditional-token-check-first ordering `submit_result` already established (see `docs/architecture/worker-job-lifecycle.md`'s claim-token section) — deliberately *narrower* than `submit_result`'s branching, since input delivery has no "already terminal" replay case: only a job that is *right now* `running` with an unexpired lease may stream its input.
- **Evidence lookup**: `EvidenceLifecycleRepository.get_evidence` (Phase 2, unchanged) — no new repository query was needed; the job record already carries `case_id`/`evidence_id`.
- **Safe-error philosophy**: a storage failure (including "object missing") propagates uncaught to `app/core/errors.py`'s existing central safe-500 handler, exactly like every other `StorageError` in this module already does — no bespoke error path was invented for this endpoint specifically.
- **`asyncio.to_thread`-per-blocking-call convention**: `MinioObjectStorage.open_stream`'s chunk bridge (`_stream_response`) follows the identical pattern every other method on that class already uses for minio-py's synchronous API.

## Genuinely new (Phase 2.2)

- `ObjectStorage.open_stream`/`ObjectStream` (`storage.py`) — a bounded, chunked read handle; `MinioObjectStorage` bridges minio-py's synchronous `.stream()` generator to an async one via `asyncio.to_thread` per chunk, never reading the whole object into API memory at once. `FakeObjectStorage.open_stream` is the matching in-memory test double.
- `EvidenceLifecycleService.get_claimed_evidence_input`/`ClaimedEvidenceInput` (`service.py`).
- `GET /api/v1/internal/worker-jobs/{job_id}/input` (`internal_api.py`) — streams the response via FastAPI's `StreamingResponse`, `Cache-Control: no-store`, a safe RFC 6266 `Content-Disposition` (sanitized against header injection), and `X-TraceX-Evidence-Id`/`X-TraceX-Evidence-SHA256`/`X-TraceX-Source-Type`/`X-TraceX-Parser-Profile` headers — never an object key, bucket, endpoint, or credential.
- Reuses the existing `X-Claim-Token` header (not a second header name) for consistency with `/result` — considered and rejected a distinct `X-Worker-Claim-Token` header since the two endpoints prove the identical thing (possession of the job's claim token) the identical way.

## Why `Content-Disposition` instead of a bespoke `X-Original-Filename` header

`structured_processing`'s originally-*proposed* endpoint shape (written before this endpoint existed, in `docs/architecture/structured-processing-worker.md`) used a custom `X-Original-Filename` header. Implemented here as a standard RFC 6266 `Content-Disposition` instead — a well-defined, standard way to carry a filename on a byte-stream response, with a documented non-ASCII encoding (`filename*=UTF-8''...`) that a bespoke header would have to reinvent. `original_filename` is caller-supplied display metadata (trimmed/length-capped at upload time, never sanitized against quotes or control characters — see `service._safe_filename`) — `_safe_content_disposition` strips CR/LF and escapes quotes before building the header, the one place that safety property is now enforced, since it's the first place `original_filename` is ever placed into a response header.

## Why streaming, not `read_bytes`

`ObjectStorage.read_bytes` (Phase 2, used internally by `MinioSourceResolver` for trusted orchestration code) fully buffers the object before returning. Reused as-is for that internal use (evidence is already capped at `MAX_EVIDENCE_BYTES`, and that call site was never HTTP-exposed), but this new endpoint is reachable over HTTP by a caller this module doesn't fully control the size of, so `open_stream` was added instead of extending `read_bytes`'s contract — a genuinely different memory-safety requirement, not a refactor of the existing internal path.

## `generic_tabular_v1`/`generic_json_v1` remain unreachable via live routing — considered, deferred

Investigated whether `routing.py` could route some `document`/`cdr`/`financial` uploads to these two fallback profiles instead of their default processor. Concluded this cannot be done as a "server-side allowlist" the way the task asked without breaking a principle this module already deliberately established (see "One canonical processor per `source_type`" above): choosing between multiple valid profiles for the *same* `source_type` requires reading and classifying the bytes' shape, which is explicitly documented as the *owning processing module's* job, not evidence-lifecycle's coarse ingestion-time routing. Making `generic_tabular_v1`/`generic_json_v1` reachable would require either (a) a new `source_type` dedicated to "generic tabular/JSON, not CDR or financial shaped" — a frozen-contract change, out of scope for any module to make unilaterally — or (b) content-shape-sniffing logic inside `evidence_lifecycle` at upload time, which contradicts the "coarse routing only" boundary this module already committed to and documented. Left unimplemented; flagged in `docs/qa/known-limitations.md` as a genuine open question for team review, not silently worked around.

**Resolved in Phase 2.3, below**, via exactly option (a) — a controlled, additive `SourceType` extension, approved by the team rather than decided unilaterally.

## Open questions for team review (Phase 2.2)

- [x] ~~`generic_tabular_v1`/`generic_json_v1` routing reachability~~ — **resolved in Phase 2.3 below.**
- A storage failure mid-stream (after response headers are already sent) cannot be converted into a clean error response — an inherent HTTP-streaming limitation, not something this endpoint's code can work around; the connection simply terminates.
- No lease-renewal exists yet (unchanged from Phase 2.1) — a worker streaming a very large evidence file close to its lease boundary could have the lease expire mid-stream; the stream itself is unaffected (already authorized before the check), but a subsequent `/result` submission past that point would be rejected as `lease_expired`, same as today.

---

# Phase 2.3 Decisions — Nipun Explicit Structured-Data Upload Routing

Approved, team-reviewed resolution of the Phase 2.2 open question above: a controlled, additive extension of `SourceType`, not a content-sniffing workaround and not a change to any existing routing entry.

## Why an additive `SourceType` change is not a frozen-contract violation

`CLAUDE.md`'s shared-contract rule forbids changing a `V1` contract's required fields, types, or validation rules *in a way that breaks existing valid payloads*; `docs/architecture/contracts.md`'s versioning policy explicitly allows "additive, backward-compatible changes... with a contract-test update." Adding two new `SourceType` enum members is exactly that: every payload that was valid before this change (every existing `source_type` value, on every existing evidence record, in every existing test) remains valid and behaves identically — nothing is removed, renamed, or tightened. `tests/contract/test_evidence.py::test_evidence_record_accepts_phase_2_3_structured_source_types` is the required contract-test update.

## Why two source types, not one

`structured_tabular` (CSV/XLSX) and `structured_json` (JSON) are kept separate, with fully disjoint accepted-content-type sets, rather than one combined `structured_data` type accepting all three MIME types. A single combined type would need a *second* signal beyond `source_type` to decide between `generic_tabular_v1` and `generic_json_v1` for a given upload — and the only candidate signal is `content_type`, which would mean content-type-based branching inside the routing decision itself. Keeping them separate preserves the exact invariant `routing.py`'s docstring already states: "one declared source type maps to exactly one canonical processor," with zero branching logic anywhere in `route_for`.

## Why `parser_profile` became fully server-controlled (all source types, not just the new two)

Auditing `service.upload_evidence` while implementing this change found that `EvidenceRecordV1.parser_profile` was, in every source type, silently set from a client-supplied form field with no relationship to `route.processor_name` — the actual routing decision. No existing test asserted a specific stored value (all pass `parser_profile=None`), so this was a live, untested inconsistency: a client could have uploaded a `document` and declared `parser_profile="anything"`, and that string would have been persisted and returned verbatim. Fixed by always computing the stored value from `route.processor_name` inside `upload_evidence`, for every source type — not scoped narrowly to `structured_tabular`/`structured_json`, since the same gap existed identically everywhere else. The `parser_profile` parameter/form field itself was kept (not removed) specifically so `upload_evidence`'s signature — and every existing call site across `tests/unit/evidence_lifecycle/{test_upload_service,test_worker_claim,test_worker_result}.py` and `tests/integration/evidence_lifecycle/*.py` — needed zero changes, satisfying "existing document/CDR/finance routing tests remain unchanged and pass" literally.

## Genuinely new (Phase 2.3)

- `SourceType.STRUCTURED_TABULAR`/`STRUCTURED_JSON` (`app/contracts/evidence.py`).
- Two new entries in `SOURCE_TYPE_CONTENT_TYPES`/`ROUTING` (`routing.py`), routing to Jasraj's pre-existing (Phase 2, unmodified) `generic_tabular_v1`/`generic_json_v1` profiles.
- `EvidenceLifecycleService.upload_evidence`'s stored `parser_profile` is now always `route.processor_name` (see above).
- `migrations/versions/af5b05e61b08_structured_source_type_routing.py` — widens the `ck_evidence_records_source_type`/`ck_worker_jobs_source_type` `CHECK` constraints (`f2086e1e89f6`'s hand-written, hardcoded eight-value list) to also accept the two new values. **Only found live**: the Pydantic contract change alone was not sufficient — a real upload against the real database failed `500` until this migration was applied, because PostgreSQL independently enforces its own copy of the allowed-values list. See `docs/qa/test-results.md`'s Phase 2.3 entry for the exact failure and fix.
- `tests/unit/evidence_lifecycle/test_structured_routing.py` (11 tests), a new contract test, and a parametrized extension of `tests/integration/structured_processing/test_worker_live.py`'s full live pipeline test to cover `generic_tabular_v1`/`generic_json_v1` alongside the pre-existing `fir_report_text_v1` case.

## No changes to Jasraj's parser implementation

`app/modules/structured_processing/structured/profiles.py`'s `GENERIC_TABULAR_V1`/`GENERIC_JSON_V1` (name, version, accepted content types, parsing logic) were inspected and found to already match this task's required routing table exactly — no compatibility correction was needed or made.

## Open questions for team review (Phase 2.3)

- None specific to this change — it closes the one open question Phase 2.2 raised, and introduces no new unresolved boundary.

---

# Phase 2.4 Decisions — Aditya Worker Identity, Authorization Binding, and Security Audit Completion

Full design lives in `docs/architecture/worker-identity-and-security.md` (the dedicated document this phase's task brief required); this section records the decisions worth cross-referencing from here.

## Reused, not duplicated (Phase 2.4)

- **Token generation/hashing pattern**: `secrets.token_urlsafe(32)` + a fast hash of a high-entropy secret, the exact primitives `access_control.tokens.generate_refresh_token`/`hash_refresh_token` and (Phase 2.1) `evidence_lifecycle`'s claim-token generation already established. `hash_worker_credential` adds one documented deviation — an optional pepper — see below.
- **Audit logging**: `app.modules.access_control.audit.record_audit_event`, reused for every new event type. No parallel audit store.
- **`Table`/repository conventions**: `worker_credentials_table` (new) follows `access_control.repository`'s existing hand-written-`sa.Table`-matching-a-migration pattern exactly.
- **Case/membership/session model and auth flow**: entirely unchanged. This phase adds a second, independent kind of principal (`WorkerPrincipal`) alongside `AuthenticatedPrincipal`/`AuthorizedCasePrincipal` — it does not touch either.

## Genuinely new (Phase 2.4)

- `worker_credentials` table + `worker_jobs.claimed_by_worker_id` column, migration `48e9e76153ca_worker_credentials_and_job_ownership`.
- `WorkerCredentialRecord`/`WorkerCredentialStatus` (`access_control/models.py`); `AccessControlRepository`'s `create_worker_credential`/`get_worker_credential_by_id`/`get_worker_credential_by_digest`/`list_worker_credentials`/`rotate_worker_credential`/`revoke_worker_credential`.
- `app/modules/access_control/worker_credentials.py` — the trusted-operator CLI, `hash_worker_credential`, `resolve_worker_pepper`, `generate_worker_token`.
- `WorkerSecurityConfigurationError` (`access_control/errors.py`); `Settings.worker_token`/`worker_credential_pepper` (replacing `worker_shared_secret`, removed).
- Rewritten `WorkerPrincipal`/`require_worker_principal` (`evidence_lifecycle/dependencies.py`); `claimed_by_worker_id`/`worker_id` parameters threaded through `EvidenceLifecycleService.claim_job`/`submit_result`/`get_claimed_evidence_input` and `EvidenceLifecycleRepository.claim_job`.
- `InvalidClaimTokenError.reason` (a safe, internal-only classifier, never in the HTTP response).
- `case_access_denied`/`worker_authentication_denied`/`worker_processor_scope_denied`/`worker_job_access_denied`/`worker_credential_rotated`/`worker_credential_revoked` audit events; `record_audit_event_safely` (`access_control/audit.py`) for the four denial events, the plain propagating `record_audit_event` for the two accepted-operator-action events.
- No new dependency: everything above is stdlib (`secrets`, `hashlib`, `hmac`, `argparse`) plus already-approved SQLAlchemy/FastAPI/Pydantic features.

## Why `allowed_processor_names` is enforced only at claim time, not on every request

A worker that has already legitimately claimed a job (proven by a valid claim token) was, by construction, in-scope for that job's processor at the moment it claimed it — re-checking scope on `/result`/`/input` would be redundant with the claim-time check and would only matter if a worker's scope could shrink *while it holds an active claim*, which this phase's CLI has no mechanism to do (rotation preserves scope; only revocation changes standing, and revocation is checked by `require_worker_principal` on every request regardless of scope). Checking scope once, at the one point where it actually gates a new action (claiming), keeps the authorization model simple and matches exactly what the task brief asked for ("A worker can claim only processor names included in its allowed_processor_names").

## Why worker-identity verification sits immediately after claim-token verification, not folded into it

`submit_result`/`get_claimed_evidence_input` check the claim-token hash and the worker identity as two separate, sequential, unconditional steps — both before any branching (including the terminal-job replay/conflict path) — rather than combining them into one compound condition. Keeping them separate lets each failure carry its own safe `reason` code for auditing (`token_mismatch` vs. `worker_identity_mismatch`) while still rendering the identical generic `401` to the caller either way; a single combined check would have to either lose that distinction internally or invent a second exception type purely to carry it, which `InvalidClaimTokenError.reason` already does more simply.

## Why `claimed_by_worker_id` is a new column, not a repurposed `claimed_by`

`worker_jobs.claimed_by` (Phase 2.1) already existed as "the claiming worker's self-declared `processor_name`" — a non-secret observability label, explicitly documented as *not* a verified identity. Overloading it to also mean "verified worker UUID" would have silently changed its type and meaning out from under any future code (or human) reading old rows, and would have made a `NULL` ambiguous between "never claimed" and "claimed under the old system, no identity recorded." A new, additive, nullable column keeps both facts independently queryable and keeps Phase 2.1's own column untouched, satisfying the task brief's "without changing frozen worker contract payloads" instruction at the schema level too — no existing column's meaning changed, only a new one was added.

## Why a pre-migration `running` job (`claimed_by_worker_id IS NULL`) is not retroactively attributed

There is no way to safely infer *which* worker identity should own a job claimed before this migration existed — `claimed_by` (a processor name, shared by every worker capable of that processor) is not a 1:1 mapping to a `worker_id`. Rather than guessing (which would violate this codebase's "never silently coerce/guess ambiguous data" rule, applied elsewhere to CDR timestamps and phone numbers), such a row is simply left unbound: `NULL` never equality-matches a real `worker_id`, so `/result`/`/input` correctly deny it until its lease expires and a real authenticated worker reclaims it — a safe, honest "this job needs to be reclaimed under the new system" outcome rather than a fabricated ownership assignment.

## Why `worker_credential_rotated`/`worker_credential_revoked` are not wired to `record_audit_event` in this phase

Rotation and revocation are already durably recorded as first-class, queryable state (`WorkerCredentialRecord.rotated_at`/`revoked_at`/`status`, visible via the CLI's own `list` command) — a genuine audit trail in their own right, independent of `security_audit_events`. The CLI is a standalone script, run by a trusted human at a terminal, not a request-serving path where "the audit write failed, but should we still let this succeed" is a live question; wiring it to `record_audit_event` is a small, safe follow-up (flagged in `docs/qa/known-limitations.md`) rather than something this phase needed to force through given the state is already captured.

## Open questions for team review (Phase 2.4)

- Whether `worker_credential_rotated`/`worker_credential_revoked` should also emit a `security_audit_events` row (currently: state changes are captured on the `worker_credentials` row itself, not duplicated into the audit table).
- Rotating `WORKER_CREDENTIAL_PEPPER` in an already-provisioned deployment invalidates every existing worker credential's digest match at once (documented, not automated) — a later phase might want a dual-pepper transition window if this becomes operationally painful.
- No maximum number of processor names or worker credentials is enforced — not expected to matter at this project's scale, but worth noting if a very large worker fleet is ever provisioned.

# Phase 2 Decisions — Sarthak Communication Processing Worker Foundation

Full design lives in `docs/architecture/communication-processing-worker.md` (the dedicated document this phase's task brief required); this section records the decisions worth cross-referencing from here.

## Reused, not duplicated

- **Worker orchestration pattern**: `client.py`/`input_resolver.py`/`run_once`/`main` mirror `structured_processing`'s Phase 2/2.2/2.4 equivalents almost exactly (claim loop over supported processors, claim-token-bound `GET .../input`, SHA-256 verification before parsing, structured JSON logging via `structlog.contextvars`) — a second, independent implementation of the same established pattern, not a shared abstraction, matching how `structured_processing` and `communication_processing` were already two independent modules in Phase 1.
- **Worker identity and authentication**: Aditya's Phase 2.4 per-worker-credential system, entirely unchanged. This worker presents `WORKER_TOKEN` exactly like any other worker process; no new authentication path, no shared-secret fallback.
- **`process_job`'s pure-function contract, `_PROFILES`, `SUPPORTED_PROCESSORS`' name/version pairs, all seven `ProcessorProfile`s, and every existing parser** (`social/*.py`, `audio/metadata.py`, `audio/routing.py`): all Phase 1, unchanged. This phase adds orchestration and two new JSON-interchange deserializers around them, not new parsing logic for the five profiles that already had one.
- **Deterministic observation IDs and canonical serialization**: `provenance.py`'s pre-existing `observation_id()`/`build_extractor()`/`mention_to_observation()`, unchanged.

## Genuinely new

- `app/modules/communication_processing/input_resolver.py`, `client.py` — this module's own `WorkerApiClient`/`WorkerInputResolver` pair, a second implementation of `structured_processing`'s pattern (not imported from it — cross-module reuse of orchestration internals was judged not worth coupling two otherwise-independent modules together for what is, per-module, under 200 lines).
- `audio/transcript_import.py::parse_transcript_import_payload`, `audio/diarization_import.py::parse_diarization_import_payload` — JSON-interchange deserializers for the two profiles that previously only accepted pre-built dataclasses.
- `worker.py`: `run_once`, `main`, `RunOnceOutcome`, `_build_input_payload`, `CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE`, `SUPPORTED_PROCESSORS`.
- `errors.py`: `WorkerOrchestrationError`/`WorkerAuthenticationError`/`InputResolutionUnavailableError`/`WorkerApiError` (mirroring `structured_processing.errors`'s identical four), `ErrorCode.MALFORMED_JSON_PAYLOAD`.
- `limits.py`: `MAX_INPUT_BYTES` (client-level transport bound, distinct from the pre-existing `MAX_AUDIO_BYTES`/`MAX_CHAT_EXPORT_BYTES` source-level bounds).
- No new dependency: `httpx` was already an approved, installed dependency (used by `structured_processing.client` and the FastAPI app itself); no ASR/diarization/LLM/embedding/cloud-AI package was added.

## Why the two JSON-interchange deserializers live in their existing files, not a new `parsers/` package

The task brief's suggested file tree (`parsers/social_export.py`/`parsers/audio_transcript.py`) predates inspection of the actual Phase 1 module layout, which already dedicates one file per profile family (`audio/transcript_import.py` already owned `TranscriptSegmentInput`/`transcript_segments_to_mentions`; `audio/diarization_import.py` already owned the diarization equivalent). Adding the new `parse_*_payload` functions to those same files keeps each profile's full lifecycle (deserialize → validate → mention) in one place and avoids introducing a package boundary that would immediately need to re-import back across it. The social-export parsers already lived under `social/`; nothing about them needed to move.

## Why the routing gap (five of seven profiles unreachable via live upload) is reported, not fixed

Unlike Phase 2.3's `structured_tabular`/`structured_json` (which had explicit prior team approval to add), no such approval exists yet for new `SourceType`s or a `parser_profile` hint covering `transcript_import_v1`/`diarization_import_v1`/`whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1`. `evidence_lifecycle/routing.py` is a shared contract this task's brief explicitly forbade changing without that approval ("do not add/modify a SourceType... unless clearly within an already-approved, additive route"). See `communication-processing-worker.md`'s "Routing boundary" section for the recommended additive decision, left for team review rather than implemented unilaterally.

## Why `_ensure_worker_credential`'s test helper now fails loudly instead of silently retrying on a revoked digest

Both this module's and `structured_processing`'s live-test helper originally treated "an active credential with a matching digest exists" as the only no-op case, and fell through to an `INSERT` otherwise. Since `credential_digest` is unique per row *regardless of status* (a revoked credential's digest is never freed for reuse — this is intentional: revocation must be permanent, or a leaked-then-revoked token could be silently reactivated by anything that re-provisions it), a revoked row with a matching digest made that fallthrough `INSERT` hit the unique constraint and crash with a raw `IntegrityError`, discovered when this module's own live test and `structured_processing`'s live test collided over the *same* shared local-dev `WORKER_TOKEN` digest in a single full-suite run (see `docs/qa/known-limitations.md`). Fixed in both files' identical helper: a matching-but-revoked row now fails the test immediately with an actionable message ("revocation is permanent — change the token literal, then rerun") instead of surfacing a confusing SQL error. No change to `access_control`'s production repository or CLI — revocation-permanence as a real security property was deliberately preserved, not routed around.

## Open questions for team review

- The routing gap above: which of the two proposed shapes (server-validated `parser_profile` hint vs. new `SourceType`s per platform) `evidence_lifecycle/routing.py`'s owner prefers, if/when these five profiles need to be reachable via a real upload.
- Whether a future phase wants `transcript_import_v1`/`diarization_import_v1` to also accept a raw-audio input that this worker itself defers (current behavior: `audio_metadata_v1`-shaped input routed to either import profile returns `DEFERRED`/`DEFERRED_REQUIRES_ASR`/`DEFERRED_REQUIRES_DIARIZATION`, unchanged from Phase 1) once a real ASR/diarization adapter is ever approved.
