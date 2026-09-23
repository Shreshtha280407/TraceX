# Worker Identity and Security (Aditya Phase 2)

Replaces the temporary `WORKER_SHARED_SECRET` boundary Nipun's Phase 2.1/2.2 work explicitly flagged as a stand-in (see `docs/architecture/worker-job-lifecycle.md`'s original "Worker identity" section, and `docs/architecture/phase-2-decisions.md`'s Phase 2.1 "Open questions") with revocable, per-worker service identities: every worker process now authenticates as a specific, named, individually-scoped, individually-revocable identity — not "anyone who knows the one shared value."

## What changed, and what didn't

**Changed**: `require_worker_principal` (`app/modules/evidence_lifecycle/dependencies.py`) now authenticates against a real `WorkerCredentialRecord` looked up by credential digest, not a single compared secret. `WorkerPrincipal` now carries `worker_id`/`display_name`/`allowed_processor_names` instead of being an empty marker. A successful `/claim` now records *which* authenticated worker identity owns the job (`worker_jobs.claimed_by_worker_id`); `/result` and `/input` now additionally require the caller to *be* that worker, not just hold the job's claim token.

**Unchanged**: everything Nipun built in Phase 2/2.1/2.2/2.3 — claim-token generation/hashing, lease/reclaim semantics, `FOR UPDATE SKIP LOCKED` concurrency, idempotent result submission, the evidence-delivery streaming endpoint, and the `structured_tabular`/`structured_json` routing — is untouched. A worker still never receives a PostgreSQL, Neo4j, MinIO, or Redis credential of any kind.

## Worker credential lifecycle

### Model

`app/modules/access_control/models.py`'s `WorkerCredentialRecord` (table `worker_credentials`, migration `48e9e76153ca_worker_credentials_and_job_ownership`):

| Field | Type | Notes |
|---|---|---|
| `worker_id` | UUID (primary key) | Stable across rotation |
| `display_name` | str | Operator-assigned, non-secret |
| `status` | `active` \| `revoked` | `WorkerCredentialStatus` |
| `allowed_processor_names` | tuple of str (JSONB) | Which `WorkerJobV1.processor_name` values this worker may claim |
| `credential_digest` | str, unique, indexed | `HMAC-SHA256(pepper, token)`, or plain SHA-256 if no pepper is configured — **never the plaintext token** |
| `created_at` | datetime | |
| `rotated_at` | datetime \| null | Last rotation, if any |
| `revoked_at` | datetime \| null | Set once, on revocation |

### Create, distribute, rotate, revoke — the trusted-operator CLI

`app/modules/access_control/worker_credentials.py` is the *only* way a worker credential is ever created, rotated, or revoked in this codebase. There is deliberately no public HTTP API for any of it (see "Explicit non-goals" below) — this CLI runs wherever the server's own PostgreSQL configuration is already available (a developer machine, a deployment bastion, a CI provisioning step), the same trust boundary an operator running `alembic upgrade head` already has.

```bash
# Create — prints the plaintext token exactly once, to the local terminal.
uv run python -m app.modules.access_control.worker_credentials create \
  --name structured-worker \
  --processor fir_report_text_v1 \
  --processor cdr_generic_v1 \
  --processor financial_transaction_generic_v1 \
  --processor generic_tabular_v1 \
  --processor generic_json_v1

# Rotate — invalidates the old token immediately; prints the new one once.
uv run python -m app.modules.access_control.worker_credentials rotate --worker-id <uuid>

# Revoke — idempotent; safe to call more than once.
uv run python -m app.modules.access_control.worker_credentials revoke --worker-id <uuid>

# List — never prints a token or a digest.
uv run python -m app.modules.access_control.worker_credentials list
```

**Distribution**: the plaintext token printed by `create`/`rotate` is the value an operator pastes into that specific worker process's own `.env` as `WORKER_TOKEN`, and nowhere else. It is never emailed, never pasted into a chat, never committed.

**Resolved (Gap-Closure follow-up): one credential per worker role, never one shared token across simultaneous worker containers.** `Settings.worker_token` is a single field — every worker module (`structured_processing.worker`, `communication_processing.worker`, `media_processing.worker`) reads it identically, with no code-level notion of "which role am I." That's correct for the CLI-documented workflow above (one worker process on the host at a time, its own `.env`, its own single `WORKER_TOKEN`). It silently breaks the moment more than one worker *role* runs simultaneously against the same `.env` — exactly what `docker compose --profile cpu-worker up` does (`structured-worker` + `communication-worker` together, each with a distinct `worker_credentials` row and a distinct `allowed_processor_names` scope). A single shared token's `credential_digest` can only ever match one of those rows; every other worker container 403s (`worker_processor_scope_denied`) on every claim, indistinguishable at a glance from a real authorization problem.

This was found via live multi-worker testing — bringing up the `cpu-worker` profile's containers together and watching one of them fail every claim — not by this repo's own test suite. Each live integration test (`test_worker_live.py`/`test_communication_worker_live.py`/`test_media_worker_live.py`) self-provisions its own scoped credential and runs in isolation from the others within one `pytest` process; none of them exercises the actual deployment topology of several worker *containers* running at once against one shared `.env`. That gap in coverage is real and worth keeping in mind for future worker-related changes: passing per-suite, in-process tests does not confirm the Docker Compose topology behaves the same way.

**Fix**: `compose.yaml` no longer relies on the shared `x-tracex-app-env` anchor's `WORKER_TOKEN` for any worker service. Each of `structured-worker`/`communication-worker`/`media-worker` overrides `WORKER_TOKEN` in its own `environment:` block from its own env var (`STRUCTURED_WORKER_TOKEN`/`COMMUNICATION_WORKER_TOKEN`/`MEDIA_WORKER_TOKEN`), each provisioned as a distinct credential via the CLI above with that role's own `allowed_processor_names`. `api` never reads `settings.worker_token` at all (it only verifies presented tokens against `worker_credentials`), so it keeps the anchor's default and is unaffected. No change to the credential-scoping model itself (`allowed_processor_names`, `require_worker_principal`, the digest-lookup mechanism) — this was a provisioning/wiring gap, not a design gap. See `docs/qa/known-limitations.md` for the full writeup and `docs/runbooks/local-development.md` for the corrected operator instructions.

### Token storage and Git-secret rules

- `secrets.token_urlsafe(32)` (256 bits of entropy) generates every worker token — the same primitive `access_control.tokens.generate_refresh_token` already uses for refresh tokens.
- Only `credential_digest` is ever persisted. The raw token exists only in the CLI operator's terminal output and the target worker process's own environment.
- **Issued worker tokens go only into untracked local/deployment secrets** — a developer's own `.env` (git-ignored) or a deployment's real secret store. They must **never** appear in: Git (any commit, any branch), `.env.example` (which ships only a blank placeholder), application logs, test reports, CI output, or a screenshot. `hash_worker_credential`/`require_worker_principal`/the CLI's own print statements are the only places a raw token is ever handled, and none of them log it.
- `WORKER_CREDENTIAL_PEPPER` (server-side) is likewise never committed — `.env.example` ships it blank, documented as optional outside production and required inside it (see "Configuration" below).

## Processor scoping

`allowed_processor_names` is enforced at claim time only: `POST /api/v1/internal/worker-jobs/claim` rejects (`403`, `worker_processor_scope_denied` audited) a `processor_name` outside the authenticated worker's allow-list, **before** the claim query ever runs — no job is claimed on the scoped-out worker's behalf. Result submission and input streaming don't re-check scope (a worker that already legitimately claimed a job is, by construction, one that was in-scope for it at claim time); they check *identity* instead — see below.

## Claim ownership and lease-reclaim semantics

`worker_jobs.claimed_by_worker_id` (nullable UUID, FK to `worker_credentials.worker_id`, `ON DELETE SET NULL`) records the authenticated worker identity that currently owns a `running` job. It is set unconditionally on every successful claim — including a reclaim after lease expiry, which is exactly how ownership legitimately transfers: worker A claims, its lease expires without a result, worker B (any worker in scope for that processor, not necessarily A) claims the same job next, and `claimed_by_worker_id` now reads B. `claimed_by` (Nipun's Phase 2.1 field — the claiming worker's self-declared `processor_name`) is unchanged and untouched; the two fields serve different purposes and both remain.

A `worker_jobs` row claimed before this migration has `claimed_by_worker_id IS NULL`. It is not retroactively attributed to any identity — it simply becomes unclaimable-by-identity until its lease expires and a real authenticated worker reclaims it under the new system (a `NULL` never equality-matches a real `worker_id`, so the identity check below fails closed for it, exactly as intended).

## Worker input/result authorization requirements

`POST /result` and `GET /input` both require, in this exact order (see `EvidenceLifecycleService.submit_result`/`get_claimed_evidence_input`):

1. A valid, active worker credential (`require_worker_principal`) — `401`/`503`, audited `worker_authentication_denied`.
2. The job exists and has a claim-token hash at all — else `401` (`InvalidClaimTokenError`, `reason="unknown_or_unclaimed_job"`).
3. The presented claim token's hash matches — else `401` (`reason="token_mismatch"`).
4. **The authenticated worker is the identity currently bound to this job** (`job.claimed_by_worker_id == principal.worker_id`) — else `401` (`reason="worker_identity_mismatch"`). This check runs immediately after step 3, **before** the already-terminal/idempotent-replay branch — a wrong worker presenting a right-shaped-but-not-theirs claim token can never retrieve a cached result or a stream it wasn't entitled to, whether the job is still running or already complete.
5. (`/result` only) The lease is still valid, and the submitted result's own scope/status validate.

Every one of these renders as the identical generic `401` to the caller (never distinguishing which check failed) — the same default-deny philosophy every other denial in this codebase already follows — while the *audit* trail (below) does record which one, safely.

## Audit event policy and safe fields

Reuses `app.modules.access_control.audit.record_audit_event`/`record_audit_event_safely` — no parallel audit store. This is operational security telemetry only; it is **not** cryptographically tamper-evident, and does not become so until Phase 6 implements the Merkle/hash-chain audit-integrity design — see "Explicit non-goals" below.

| `event_type` | Raised from | Outcome | Safe `metadata` fields | `case_id` |
|---|---|---|---|---|
| `case_access_denied` | `access_control.dependencies.require_case_action` | `DENIED` | `action` | yes |
| `worker_authentication_denied` | `evidence_lifecycle.dependencies.require_worker_principal` | `DENIED` | `reason` (`missing_or_malformed_credential` / `invalid_credential` / `revoked_credential`), `worker_id` (only when known — i.e. a revoked credential was actually found) | no — authentication fails before any case/job is resolved |
| `worker_processor_scope_denied` | `evidence_lifecycle.internal_api.claim_job` | `DENIED` | `worker_id`, `processor_name`, `processor_version` | no — scope is checked before any specific job is looked at |
| `worker_job_access_denied` | `evidence_lifecycle.internal_api.submit_result`/`get_worker_job_input`/`renew_job_lease`/`submit_observation_batch` | `DENIED` | `worker_id`, `job_id`, `reason` (from `InvalidClaimTokenError.reason` — e.g. `token_mismatch`, `worker_identity_mismatch`, `lease_expired`, `not_claimed`, `unknown_or_unclaimed_job`) | no — a claim-token failure may not even resolve to a real job; never probes for one just to attach a `case_id` |
| `worker_credential_rotated` | `access_control.worker_credentials.rotate_worker_credential` | `SUCCESS` | `worker_id` | no |
| `worker_credential_revoked` | `access_control.worker_credentials.revoke_worker_credential` | `SUCCESS` | `worker_id` | no |
| `worker_job_claimed` (Phase 3) | `evidence_lifecycle.internal_api.claim_job` (first-ever claim) | `SUCCESS` | `worker_id`, `job_id`, `evidence_id`, `processor_name`, `processor_version`, `attempt` (always `1`) | yes |
| `worker_job_reclaimed` (Phase 3) | `evidence_lifecycle.internal_api.claim_job` (lease-expiry reclaim) | `SUCCESS` | same shape as `worker_job_claimed`; `attempt` is `>= 2` | yes |
| `worker_job_lease_renewed` (Phase 3) | `evidence_lifecycle.internal_api.renew_job_lease` | `SUCCESS` | `worker_id`, `job_id`, `evidence_id`, `attempt` | yes |
| `worker_job_completed` (Phase 3) | `evidence_lifecycle.internal_api.submit_result` (`status=succeeded`) | `SUCCESS` | `job_id`, `evidence_id`, `result_id`, `status`, `observation_count` | yes |
| `worker_job_failed` (Phase 3) | `evidence_lifecycle.internal_api.submit_result` (any other terminal status) | `SUCCESS` | same shape as `worker_job_completed`; `status` names the actual terminal outcome (`failed`/`deferred`/`cancelled`) | yes |
| `worker_job_retry_exhausted` (Phase 3) | `evidence_lifecycle.internal_api.claim_job` (opportunistic sweep, once per exhausted job) | `SUCCESS` | `job_id`, `evidence_id`, `attempt`, `max_attempts` | yes |

Every event additionally carries the standard `record_audit_event` fields where known/safe: `request_id`, `user_id` (always `None` for every worker-facing event above — there is no human user behind a worker call), `outcome`. **Never** present in any event: a bearer token, a claim token, an object key/URI, a request body, a stack trace, or raw evidence content — verified behaviorally in `tests/unit/evidence_lifecycle/test_worker_identity_api.py`, `test_worker_internal_api.py`, and `test_worker_lease_renewal.py`.

**Why the eight Phase 3 rows use `record_audit_event_safely`, including the four `SUCCESS`-outcome ones**: `record_audit_event_safely` was originally documented as denial-path-only, but its actual behavior (swallow a write failure, never propagate) is exactly right for a second case too — an already-committed state change whose entire value to the caller is a one-time secret the response body carries (a `claim_token`, an extended `lease_expires_at`), not the state change itself. An audit-sink outage turning that into a 500 would strand an already-claimed/renewed job with nobody holding a usable token or knowing the new expiry, recoverable only by waiting out the lease. This is different from `worker_job_completed`/`worker_job_failed` (still the propagating `record_audit_event`, matching the pre-Phase-3 `worker.result.accepted` precedent): a result is idempotent and safely retryable, so a genuine audit-write failure there should surface as a visible error, not be silently absorbed. See `app/modules/access_control/audit.py`'s `record_audit_event_safely` docstring for the full reasoning.

**`worker_credential_rotated`/`worker_credential_revoked` are recorded via the plain (propagating) `record_audit_event`, not the denial-only `record_audit_event_safely`.** Rotation/revocation are accepted operator actions, not denials — the same distinction `access_control/audit.py`'s own docstring draws for every other accepted-path event (e.g. login/register) — so an audit-write failure here surfaces as a real error to the operator running the CLI rather than being silently swallowed. `revoke_worker_credential` audits every call, including a redundant call against an already-revoked credential, since the operator action itself (not just the resulting DB state) is what's worth a trail entry. Rotation/revocation are also already durably recorded as `WorkerCredentialRecord.rotated_at`/`revoked_at`/`status`, queryable via `list_worker_credentials` — the audit event is additional, not the only record.

**Failure to write a denial audit never grants access or turns a deny into a successful request** — `record_audit_event_safely` (`access_control/audit.py`) swallows a write failure and logs a warning instead of propagating it; every denial call site in this phase uses it. `require_case_action`'s `case_access_denied` audit closes the exact gap `docs/architecture/phase-2-decisions.md`'s Phase 2 "Open questions" and `docs/qa/known-limitations.md` documented since Phase 2.

## Workers remain database/MinIO/Neo4j/Redis blind

Unchanged from every prior phase: a worker's credential proves *identity*, not infrastructure access. `WorkerPrincipal`/`WorkerCredentialRecord` never carry a PostgreSQL, Neo4j, MinIO, or Redis credential; `GET /input` still streams evidence bytes through the API process itself (see `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery"), never handing out an object key or storage credential. Nothing in this phase changes that boundary.

## Temporary/shared-secret behavior is removed, not retained as a fallback

`Settings.worker_shared_secret` no longer exists. `require_worker_principal` has no code path that accepts a bare shared secret, an unauthenticated caller, or any credential not resolvable to a specific, active `WorkerCredentialRecord`. `WORKER_TOKEN` (client-side, what a worker process presents) and `WORKER_CREDENTIAL_PEPPER` (server-side, mixed into every stored digest) are new, distinct settings — not a renamed continuation of the old boundary.

## Configuration

| Setting | Side | Required? | Behavior when missing |
|---|---|---|---|
| `WORKER_TOKEN` | Client (a worker process) | Yes, for that worker to authenticate | That worker process's own CLI (`structured_processing.worker --once`) refuses to start, safely (`WorkerAuthenticationError`, no fabricated request) |
| `WORKER_CREDENTIAL_PEPPER` | Server | Optional outside production; **required** when `APP_ENV=production` | Outside production: falls back to an unkeyed SHA-256 digest (documented, intentional — see below). In production: `require_worker_principal` fails every request `503` (`worker security is not configured`), and the CLI refuses to create/rotate a credential, both via `resolve_worker_pepper` raising `WorkerSecurityConfigurationError` with a clear, non-secret message — never a stack trace, never a silent unkeyed fallback in that environment. |
| `WORKER_LEASE_MAX_SECONDS` (Phase 3) | Server | No | Defaults to `3600` (1 hour). Absolute ceiling on `/renew`'s cumulative effect for one claim/reclaim attempt, from that attempt's `claimed_at` — see `docs/architecture/worker-job-lifecycle.md`'s "Lease and retry policy". Validated `>= WORKER_LEASE_SECONDS` at `Settings` construction time; a smaller value fails fast at startup, not silently. |
| `WORKER_JOB_MAX_ATTEMPTS` (Phase 3) | Server | No | Defaults to `5`. Bounds how many times one `worker_jobs` row may ever be claimed/reclaimed before the opportunistic sweep transitions it to `failed` — see `docs/architecture/worker-job-lifecycle.md`. |

### Why an unpeppered fallback is safe outside production

`hash_worker_credential`'s unkeyed-SHA-256 fallback applies the identical reasoning `access_control.tokens.hash_refresh_token` already documents for refresh tokens: the input is already a 256-bit-entropy random secret, so there is no dictionary/rainbow-table attack surface a slow KDF or a pepper would meaningfully close for a *local development* threat model. The pepper's real value is defense-in-depth against a **leaked production database dump** being immediately usable to forge worker credentials without also having the separately-stored pepper — a production-specific concern, hence the production-only hard requirement.

### Rotating the pepper invalidates every existing credential

`hash_worker_credential` must be called identically (same pepper, or same "no pepper" state) at credential-creation/rotation time and at every subsequent authentication. Changing `WORKER_CREDENTIAL_PEPPER` in an already-provisioned deployment silently breaks every previously-issued token's digest match — every worker would need its token rotated after a pepper change. This is an inherent property of peppering, not a bug; flagged here so an operator doesn't mistake mass worker-authentication failure after a pepper rotation for something else.

## Explicit non-goals (this phase)

No public worker-account management API, no worker login/logout flow, no OAuth/OIDC/SAML, no mTLS/certificate-authority infrastructure, no Kubernetes secrets or cloud secret-manager integration, no RBAC/ABAC redesign, no case CRUD changes, no changes to `EvidenceRecordV1`/`ObservationV1`/`EntityV1`/`EventV1`/`WorkerJobV1`/`WorkerResultV1`/`WorkerProgressV1` payload contracts. See `docs/qa/known-limitations.md` for the full deferred list.

**Aditya Phase 3 additionally does not implement**: credential expiry (the model remains active/revoked only — no `expires_at`; revocation is the only way to deny a previously-active credential, and it is permanent, matching the existing design's own "the smallest necessary extension" scope), a maximum-concurrent-credential cap, an automatic-revocation-on-repeated-authentication-failure policy, or any cryptographic tamper-evidence over the audit trail itself (Merkle roots, hash chains, signatures — Phase 6's responsibility). These security/audit events are durable operational history, not yet a provable, tamper-evident chain.
