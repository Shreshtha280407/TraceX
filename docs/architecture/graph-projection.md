# Canonical Observation-to-Neo4j Graph Projection (Phase 2.5)

Owner: Shreshtha. Extends the Phase 1 graph foundation (`docs/architecture/neo4j-graph-foundation.md`, `docs/architecture/graph-taxonomy-v1.md`) with the piece that was explicitly deferred then: a durable, retryable path that takes an accepted canonical `ObservationV1` (persisted by Nipun's `evidence_lifecycle` worker-result acceptance flow) and reliably projects it into a case-scoped Neo4j graph — without ever risking the canonical PostgreSQL record, without ever exposing raw evidence through the graph, and without silently discarding a result if Neo4j happens to be down.

**PostgreSQL is authoritative. Neo4j is derived and rebuildable.** Every fact this module writes into Neo4j already exists, more completely, in `evidence_lifecycle`'s `worker_observations`/`evidence_records` tables. If the entire graph database were dropped and every `graph_projection_jobs` row reset to `queued`, re-running the projector would reconstruct an identical graph. Nothing about case authorization, evidence storage, or worker identity depends on Neo4j being up.

## The pipeline

```text
Canonical ObservationV1 persisted by worker-result acceptance
    (EvidenceLifecycleRepository.submit_result, same transaction)
    → durable graph_projection_jobs row (status=queued)
    → app.modules.graph.outbox_repository.claim_batch (FOR UPDATE SKIP LOCKED)
    → app.modules.graph.projector (reconstruct contracts, call projection.py)
    → case-scoped, idempotent Neo4j MERGE
    → GET /api/v1/cases/{case_id}/graph/observations (case-scoped read)
```

## Why this sits across two modules, and the one-directional import rule

`graph_projection_jobs` is *defined* in `app/modules/evidence_lifecycle/repository.py`, not `app/modules/graph/`, because `evidence_lifecycle` owns the transaction the row must be durable within — `EvidenceLifecycleRepository.submit_result` inserts one row per accepted observation in the exact same `engine.begin()` block as `worker_results`/`worker_observations`. `evidence_lifecycle` never imports `app.modules.graph` (a hard, statically-enforced boundary — see `tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`), so it only ever inserts the initial `queued` row; it has no idea what a "projection" is.

`app/modules/graph/outbox_repository.py` imports that exact `graph_projection_jobs_table` (and, read-only, `worker_observations_table`/`evidence_records_table`) directly from `evidence_lifecycle.repository`. This is a sanctioned, one-directional dependency: `app.modules.graph` is a *consumer* of `evidence_lifecycle`'s accepted results, the same relationship every extraction module (`structured_processing`, `communication_processing`, `media_processing`) already has to `evidence_lifecycle` — the only difference is that those modules consume over the internal worker HTTP API (`WorkerJobV1` in, `WorkerResultV1` out) and must remain PostgreSQL/Neo4j/MinIO-blind, while `app.modules.graph` is explicitly a **backend-owned internal service**, not an extractor worker, so it is not bound by that DB-blindness rule (see `CLAUDE.md`'s "A future worker's only contract is `WorkerJobV1` in, `WorkerResultV1` out" — this projector is not that kind of worker). It still never receives Neo4j credentials or PostgreSQL access itself from *its own* callers being untrusted; it is code written and reviewed by the same team as the rest of the backend.

**As of Phase 3 (Nipun)**, `evidence_lifecycle` gained a *second* accepted-observation source — partial `ObservationBatchSubmissionV1` micro-batches, alongside the pre-existing terminal `WorkerResultV1` path — but both write into the exact same `worker_observations` row shape (distinguished only by a nullable `result_id` vs. `observation_batch_id` linkage column) and both enqueue the exact same `graph_projection_jobs` row shape, in the same transaction. **This module required no code change at all** to correctly consume batch-sourced observations: `outbox_repository.get_observation` already looks a row up by `observation_id` alone, with no idea whether it came from a result or a batch. See `docs/architecture/phase-3-decisions.md`. **As of Jasraj's Phase 3 work**, this is no longer a theoretical capability: `structured_processing.worker`'s real document/OCR/NER and CDR/finance batch orchestration is the first real producer to exercise this exact path — every real OCR/regex/NER/relation-derived observation and every real chunked CDR/finance record reaches this same, unmodified outbox/projector, confirmed live (`docs/qa/test-results.md`). Shreshtha's graph-mapping work can therefore consume these observations without Jasraj needing to change any output semantics.

## Graph model

Reuses the existing Phase 1 taxonomy (`Case`/`Evidence`/`Observation`, `HAS_EVIDENCE`/`HAS_OBSERVATION`/`YIELDED_OBSERVATION`) unchanged — see `docs/architecture/graph-taxonomy-v1.md` for the full node/relationship reference and *why* `YIELDED_OBSERVATION` (not `HAS_OBSERVATION`) is the Evidence→Observation edge name. This phase adds exactly one new node kind and one new relationship:

```text
(:EntityMention {
  case_id,
  mention_id,
  observation_id,
  mention_type,      -- nullable; ExtractedEntityMention.entity_type_hint verbatim
  display_label       -- ExtractedEntityMention.text verbatim
})

(:Observation)-[:MENTIONS {ordinal}]->(:EntityMention)
```

`mention_id` is derived deterministically (`app.core.ids.deterministic_uuid`) from `(case_id, observation_id, ordinal, NFC-normalized-and-whitespace-collapsed text)` — never from `entity_type_hint` or text similarity alone. Two consequences that are both intentional, not oversights:

- **Re-projecting the same canonical observation is idempotent.** The mention IDs it produces are identical every time, so `MERGE` never creates a duplicate node or a duplicate `MENTIONS` edge.
- **Two different observations that happen to mention the same-looking text always get two distinct `EntityMention` nodes.** There is no cross-observation deduplication, fuzzy matching, or identity inference here at all — an `EntityMention` is evidence-local by construction. Resolving mentions into real, cross-observation `EntityV1` entities is explicit, later-phase entity-resolution work; nothing in this phase performs it, automatically or otherwise.

`ExtractedEntityMention.attributes` (an open-ended `dict[str, JsonValue]` bag) is never projected — the same exclusion policy already applied to `ObservationV1.attributes`/`EntityV1.attributes`/`EventV1.attributes`.

### Existing `object_uri` storage (a Phase 1 decision, not reopened here)

`app/modules/graph/projection.py`'s pre-existing `EVIDENCE_ALLOWED_PROPERTIES` (Phase 1, Shreshtha's own earlier work) includes `object_uri` on the projected `Evidence` node — an already-reviewed decision, exercised by `outbox_repository.py`'s reconstruction of a full `EvidenceRecordV1` to feed the unchanged `project_evidence`. This phase does not touch that decision. What this phase *does* guarantee is narrower and newer: `GET /api/v1/cases/{case_id}/graph/observations`'s response shape (`app/modules/graph/schemas.py`) has no `object_uri` field at all — the new read API can never return it, regardless of what Neo4j stores internally (verified in `tests/security/graph/test_graph_module_boundaries.py` by introspecting the actual Pydantic response models, not by scanning source text). If a future phase decides the underlying `Evidence.object_uri` property itself should be removed from Neo4j, that is a team decision for whoever owns `docs/decisions/ADR-001-graph-projection-and-case-isolation.md` next, flagged here for visibility, not resolved unilaterally by this phase.

## Durable projection queue (`graph_projection_jobs`)

```text
projection_id       UUID, primary key
case_id              UUID
evidence_id          UUID
observation_id       UUID, UNIQUE  -- at most one job row per observation, ever
status               queued | running | succeeded | failed | deferred
attempt              int, starts at 0
max_attempts         int (GRAPH_PROJECTION_MAX_ATTEMPTS, default 5)
lease_expires_at     timestamptz, nullable
last_error_code      text, nullable   -- a short, safe classifier
last_error_message   text, nullable   -- sanitized only, never a raw exception/Cypher/stack trace
created_at / updated_at / completed_at
```

Migration: `migrations/versions/a204a94ccd49_graph_projection_jobs.py`.

### Attempt/lease/retry policy — deliberately different from `worker_jobs.claim_job`

`evidence_lifecycle.repository.claim_job` increments `attempt` only on a lease-expiry *reclaim*, never on a first claim, and has no max-attempt cutoff at all (a documented Phase 2.1 limitation). `graph_projection_jobs` diverges on purpose: **`attempt` increments on every claim, including the first**, because this table's `max_attempts` genuinely bounds total retries — "how many times has this actually been tried" is the number that has to be right for the bound to mean anything.

- **Claim eligibility**: `status` is `queued` or `deferred`, or `status` is `running` with an expired lease — in every case, only while `attempt < max_attempts`. `FOR UPDATE SKIP LOCKED`, exactly like `evidence_lifecycle.repository.claim_job`, so two concurrent projector runs never claim the same row twice (proven live in `tests/integration/graph/test_outbox_repository_live.py` via two genuinely concurrent `claim_batch` calls).
- **`queued` vs. `deferred`**: both are claimed identically; the distinction is diagnostic only. `deferred` means "this attempt found the observation's evidence dependency not actually projected yet" (an `ObservationProjectionResult.DEFERRED` outcome — not expected in practice, since every attempt projects the evidence in the same pass, but handled safely if it ever happens). `queued` (after a failed attempt) means "Neo4j itself was unreachable."
- **Retry exhaustion**: once `attempt >= max_attempts`, a retryable failure is written as durably `failed` instead of being requeued — inspectable (`last_error_code`/`last_error_message`/`completed_at` all set), never silently dropped, never retried forever.
- **Crash-recovery sweep**: `claim_batch` first sweeps any `running` row with an expired lease *and* exhausted attempts (a projector that crashed mid-attempt on what was already its last try) to `failed`. Without this, such a row would sit in `running` forever — correctly never reclaimed, but also never terminal, invisible to both "queued for retry" and "failed for inspection." Proven live in `test_crash_recovery_sweep_marks_stuck_exhausted_job_failed`.

### Outcome → status mapping (`app/modules/graph/projector.py`)

| What happened | Durable status | Retryable? |
|---|---|---|
| `project_evidence`/`project_observation` apply, mentions apply (or none exist) | `succeeded` | terminal |
| Canonical observation/evidence row missing from PostgreSQL (data integrity, not transient) | `failed` immediately | no, regardless of remaining attempts |
| A Neo4j read/write raises `GraphConnectionError` (Neo4j unreachable) | `queued` (or `failed` if exhausted) | yes |
| `project_observation`/`project_observation_mentions` returns `DEFERRED` | `deferred` (or `failed` if exhausted) | yes |

**A Neo4j outage never discards an accepted worker result.** `submit_result` never touches Neo4j at all — the canonical `WorkerResultV1`/`ObservationV1` rows are already durably committed to PostgreSQL before a `graph_projection_jobs` row is ever claimed. The worst a Neo4j outage can do is leave that one row retryable.

## The projector (`app/modules/graph/projector.py`, `app/modules/graph/worker.py`)

```bash
uv run python -m app.modules.graph.worker --once
uv run python -m app.modules.graph.worker --loop     # Phase 2 closeout
```

`--once` claims up to `GRAPH_PROJECTION_BATCH_SIZE` (default 25) eligible jobs and attempts each one, then exits — a cron/systemd timer, or an operator invoking it manually, can run it repeatedly. Exit code `0` if every claimed job either succeeded or was left safely retryable/deferred; `1` if any job reached a terminal `failed` state (so a scheduler can alert on it without treating an ordinary "Neo4j was briefly down, will retry next run" as an error).

**`--loop` (Phase 2 closeout)** runs that same batch logic continuously in-process instead of exiting after one batch — see "Continuous operation" below. Both modes share the identical `run_batch` claim/project/mark-outcome logic; `--loop` only adds the outer continuous-operation wrapper.

For each attempt, evidence is re-projected unconditionally (a cheap, safe `MERGE` no-op if it's already correct) rather than tracked separately as "already done" — this keeps each attempt fully self-sufficient and avoids a spurious `DEFERRED` if a different job for the same evidence hasn't run yet.

### Continuous operation (`--loop`)

`worker.run_loop` builds its PostgreSQL engine and Neo4j driver **once** and reuses them for the whole loop's lifetime (unlike `--once`'s fresh-connection-per-invocation), appropriate for a long-running process:

- **Idle polling**: an empty batch (`claimed == 0`) sleeps `GRAPH_PROJECTOR_POLL_INTERVAL_SECONDS` (default 5s) before trying again — interruptible, so a shutdown request wakes it immediately.
- **Immediate retry when work was claimed**: a batch that claimed at least one job loops again right away (more work may be queued).
- **Bounded exponential backoff on failure**: an exception escaping `run_batch` itself (a Neo4j/PostgreSQL connection-level failure, not a per-job outcome — those are all handled inside `run_batch`) sleeps `min(poll_interval * 2**consecutive_failures, GRAPH_PROJECTOR_MAX_BACKOFF_SECONDS)`; `GRAPH_PROJECTOR_MAX_CONSECUTIVE_FAILURES` (default 5) stops the loop entirely — exit code `1`, an operator-visible problem to page on.
- **Graceful SIGINT/SIGTERM shutdown**: real `asyncio`-native signal handlers (`loop.add_signal_handler`, not raw `signal.signal`, since `run_loop` is itself a coroutine) set an `asyncio.Event` checked before every batch claim, never mid-batch — whatever batch is already in flight always finishes (every claimed job terminates in `mark_succeeded`/`mark_retryable_failure`/`mark_failed`, `run_batch`'s own unconditional contract) before the loop returns. Exit code `0` on a clean shutdown.
- **Multiple projector instances are safe by construction**: unchanged — the existing atomic `FOR UPDATE SKIP LOCKED` `claim_batch` already guarantees two concurrent projector runs (any mix of `--once`/`--loop`) never claim the same row twice.

### Lease-renewal heartbeat (Phase 2 closeout)

A batch whose *cumulative* processing time approaches `GRAPH_PROJECTION_LEASE_SECONDS` risks a not-yet-attempted job later in the same batch looking lease-expired to a different concurrent projector (every job claimed together in one `claim_batch` call shares the same `lease_expires_at`) — which could then legitimately reclaim and duplicate it. `run_batch`'s optional `renew_interval_seconds` (wired to `GRAPH_PROJECTION_RENEW_INTERVAL_SECONDS`, default 40s, by `--loop`; `None` — unchanged behavior — for `--once`) renews every *not-yet-attempted* job's lease whenever that much wall-clock time has elapsed since the last renewal, via `GraphProjectionOutboxRepository.renew_lease` — an atomic `UPDATE ... WHERE status='RUNNING' AND lease_expires_at >= now`, so a lease can never be extended past its own expiry (a legitimate reclaim by another projector always wins).

## Minimal graph read API

```text
GET /api/v1/cases/{case_id}/graph/observations?limit=&offset=
```

- Authorization: `app.modules.access_control.dependencies.require_graph_read` (`CaseAction.GRAPH_READ`) — an action Aditya's Phase 1 access-control foundation already provisioned for exactly this later use, granted to `case_manager`/`investigator`/`analyst`/`reviewer`/`viewer`. A denied caller gets the identical generic `403` (and `case_access_denied` audit event) every other case-scoped endpoint returns.
- Backed by `app/modules/graph/queries.py::list_case_observations` — case-scoped in every `MATCH` clause, bounded (`limit` capped at `MAX_PAGE_SIZE=200`, the same constant every other graph read query already uses), stable-ordered by `observation_id`.
- Response (`app/modules/graph/schemas.py`) never contains an object URI, raw evidence/document body, worker credential, claim token, Cypher text, or any Neo4j implementation detail — verified both by direct response-content assertions (`tests/unit/graph/test_graph_api.py`) and by static introspection of the response models' declared fields (`tests/security/graph/test_graph_module_boundaries.py`).
- This is a backend-verification and later-UI-integration endpoint, not a graph analytics/query engine — no arbitrary filtering, no Cypher exposure, no unrestricted traversal.

## Configuration

Three new, narrowly-scoped settings (`app/core/config.py`, `.env.example`, `compose.yaml`), reusing the existing `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD` (already required, no default — a production-like deployment already fails closed on a missing Neo4j configuration, unchanged by this phase):

| Setting | Default | Purpose |
|---|---|---|
| `GRAPH_PROJECTION_BATCH_SIZE` | 25 | Jobs claimed per `--once`/`--loop` batch |
| `GRAPH_PROJECTION_LEASE_SECONDS` | 120 | How long a claimed job is exclusively held before its lease expires |
| `GRAPH_PROJECTION_MAX_ATTEMPTS` | 5 | Retries before a job is left durably `failed` |
| `GRAPH_PROJECTION_RENEW_INTERVAL_SECONDS` | 40 | `--loop` only: how often a long batch's remaining leases are renewed |
| `GRAPH_PROJECTOR_POLL_INTERVAL_SECONDS` | 5 | `--loop` only: sleep between empty batches |
| `GRAPH_PROJECTOR_MAX_BACKOFF_SECONDS` | 60 | `--loop` only: exponential-backoff ceiling on repeated failure |
| `GRAPH_PROJECTOR_MAX_CONSECUTIVE_FAILURES` | 5 | `--loop` only: consecutive failures before the loop stops itself |

An optional `graph-projector` Compose service (Phase 2 closeout, `profiles: ["workers"]` — not started by a plain `docker compose up`) runs `--loop` continuously; see `docs/runbooks/local-development.md`. No new exposed port, no credential ever handed to an external worker — this module remains the one backend-owned exception to "workers never hold direct PostgreSQL/Neo4j credentials" (see `worker.py`'s module docstring), unchanged by adding `--loop`.

## Testing

- `tests/unit/graph/test_projection.py` — `EntityMention` allow-listed properties, parameterized/never-interpolated Cypher, deterministic mention IDs (stable across re-projection, distinct across case/observation/ordinal/text), defer-when-observation-missing, no-mentions-is-a-no-op-write, `attributes` never reaching a graph property.
- `tests/unit/graph/test_schema.py` — the new `EntityMention` constraint/index are idempotent and case-scoped, same as every existing statement.
- `tests/unit/graph/test_projector.py` — orchestration outcome logic (success/failed/retrying/deferred), one bad job never aborting the rest of a batch, against fully in-memory duck-typed fakes.
- `tests/unit/graph/test_graph_api.py` — authorization wiring, cross-case denial, pagination bounds, response-content safety.
- `tests/security/graph/test_graph_module_boundaries.py` — no object-storage/worker-credential/OCR/ML library imports; the read API's response models never declare a forbidden field.
- `tests/integration/graph/test_outbox_repository_live.py` — real `FOR UPDATE SKIP LOCKED` concurrency safety (two genuinely concurrent claims never double-claim), real lease-expiry reclaim, real retry exhaustion, the crash-recovery sweep — none of which an in-memory fake can meaningfully prove.
- `tests/integration/graph/test_full_pipeline_live.py` — the complete, real path: upload → authenticated worker claim → secure evidence stream → worker result → durable `graph_projection_jobs` row → projector run → real Neo4j query and the real case-scoped HTTP endpoint both confirm the projected `Observation`/`EntityMention`.
- `tests/integration/media_processing/test_media_worker_live.py` (Gaurav's Phase 2 completion; updated for the Phase 2 closeout's `media_detection_v1` routing and real analysis components) — the same complete real path, exercised by a source-processing module other than the one this pipeline was originally built and proven against: a real image and a real synthetic video, each claimed and processed by the real `media_processing` worker (real detector/OCR when a model asset is locally bootstrapped, metadata-only otherwise — never a fabricated pass either way), project cleanly with no duplication on a second projector run.
- `tests/unit/graph/test_worker_loop.py` (Phase 2 closeout) — `run_loop`'s poll/backoff/shutdown control flow, with `run_batch` monkeypatched (no real PostgreSQL/Neo4j connection needed — `create_engine`/`create_driver` build lazy client objects that only connect on first real use).
- `tests/unit/graph/test_projector.py`'s renewal tests (Phase 2 closeout) — `renew_interval_seconds`/injectable `monotonic` prove only *not-yet-attempted* jobs in a batch get their leases renewed, never a job already completed, and that `renew_interval_seconds=None` (the `--once` default) never renews at all.
- `tests/integration/evidence_lifecycle/test_observation_batch_live.py` (Phase 3) — a real document-style micro-batch submission (two partial batches via `POST /{job_id}/observations`) durably persists all rows, enqueues exactly one `graph_projection_jobs` row per observation, and — before any projector run — is confirmed genuinely absent from Neo4j (proving the ingestion endpoint itself never wrote to Neo4j directly); a real `run_batch` projector run then confirms both observations succeed, a second run confirms no duplicate work, and both a direct Neo4j query (`list_case_observations`) and the real case-scoped HTTP graph-read API confirm the projection.

## Known limitations and intentionally deferred work

- **Resolved (Phase 2 closeout): a continuous `--loop` mode now exists.** `--once` remains available and unchanged (a cron/systemd timer or manual invocation can still run it repeatedly); `--loop` is the production-style alternative — see "Continuous operation" above.
- **Resolved (Phase 2 closeout): lease renewal now exists for `--loop`.** `GraphProjectionOutboxRepository.renew_lease` extends a still-`RUNNING` job's lease; `--once` still doesn't renew mid-attempt (a single bounded batch is expected to finish well within `GRAPH_PROJECTION_LEASE_SECONDS`), by design.
- **No re-projection/rebuild-from-scratch CLI.** Recovering from a full graph loss today means manually resetting every `graph_projection_jobs` row's `status` back to `queued` (a direct SQL operation) and re-running `--once`/`--loop` until the queue drains; a dedicated "rebuild the whole graph for this case" maintenance command is future work.
- **No pepper/rotation-equivalent concern here** (this module holds no credentials of its own), but **no operator-facing dashboard or alerting** exists for jobs sitting `failed` — `last_error_code`/`last_error_message` are queryable directly in PostgreSQL only.
- **Entity resolution, candidate identity links, cross-modal correlation, and the hypothesis engine remain entirely out of scope**, exactly as `docs/architecture/graph-taxonomy-v1.md` already documented for Phase 1 — an `EntityMention` is never promoted to an `EntityV1` by anything in this phase.
- **`project_entity`/`project_event` remain unwired** (Phase 1's own limitation, unchanged): nothing in this repository constructs a real `EntityV1`/`EventV1` yet, so those two projection functions still have no real caller. Only `project_evidence`/`project_observation`/`project_observation_mentions` are wired to a durable pipeline by this phase.
