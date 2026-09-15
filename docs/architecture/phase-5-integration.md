# Phase 5 graph/correlation integration foundation

Owner: Nipun. Status: **complete** (Phase 5 final integration). See "Phase 5
final integration" below for the closeout record; the sections above it
describe the Phase 5A foundation this closeout builds on, unchanged.

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

## Phase 5 final integration

After Shreshtha (5A), Aditya, Jasraj, Gaurav, and Sarthak (5B) all merged,
this closeout resolved every open Phase 5 integration item and froze the
transparent rules baseline. Full command-level verification is in
`docs/qa/test-results.md`'s dated entry; this section records the design
decisions.

### Refresh-rate-limit regression (P5-REGRESSION-AUTH-001)

Root cause: `AuthService.refresh` keyed its rate limiter on the refresh
token's own string value (`hash_rate_limit_key("refresh", request.
refresh_token)`). A refresh token is single-use and rotates on every
successful call (`rotate_session`), so a legitimate client's own repeated
calls each present a *different* token string -- every attempt landed in
its own one-shot bucket and the limiter could never actually trigger for
a real, authenticated, rotating client. This was not a Phase 5B
regression (`git blame` confirms `service.py`'s refresh path is
unchanged since Phase 1); it was latent and only became visible once a
regression test exercised a realistic rotating-token client instead of a
fixed never-issued one.

Fix: key the limiter on `ctx.ip_marker` (falling back to a fixed
`"unknown"` bucket when no client host is discoverable) -- the same
stable, pre-validation identity `login`'s own rate limiter already keys
on (there, `request.email`). An unauthenticated request with an invalid
token still returns `401` for each attempt until the same-IP bucket is
exhausted, at which point it (and any further attempt, valid or not)
returns `429`; a legitimate rotating-token client now correctly hits
`429` once it exceeds the configured limit, which was unreachable before.
See `app/modules/access_control/service.py`'s `refresh` method and
`tests/unit/access_control/test_service.py::
test_refresh_rate_limit_applies_across_rotated_tokens_from_the_same_caller`
/ `tests/unit/access_control/test_api.py::
test_refresh_rate_limit_returns_429_for_a_real_rotating_authenticated_client`.

### Persisted media-manifest/chunk enforcement (P5-INTEG-VISUAL-001, P5-INTEG-COMMUNICATION-001)

Both open items were the same wiring gap: `app/modules/evidence_lifecycle/
media_orchestration.py`'s manifest/chunk/checkpoint persistence and
`EvidenceLifecycleService.create_media_manifest`/`submit_observation_batch`
(with `media_publication=`) were fully built and unit-tested in Phase 4,
but nothing ever called `create_media_manifest`, and neither the visual
nor the communication worker called `publish_media_chunk` -- both
submitted every observation through the plain, unscoped `/observations`
batch route, and a worker's own locally-derived manifest was never
registered with the coordinator at all (the audio worker's own docstring
said the quiet part: "It never attempts a direct coordinator/database
write").

Closed additively, reusing the existing tables/service/validation
unchanged:

- **New internal endpoint** `POST /api/v1/internal/worker-jobs/{job_id}/
  media-manifest` (`app/modules/evidence_lifecycle/internal_api.py`) --
  claim-token-authenticated exactly like `/media-chunks/publish`, calling
  the existing `EvidenceLifecycleService.create_media_manifest` (now
  itself claim-token/worker-identity/lease-checked via a new
  `_require_active_claim` helper -- it previously had no such check at
  all, since nothing ever called it as a real endpoint). A worker submits
  its locally-planned `ChunkManifest` (it, not the coordinator, can probe
  a video's duration or an audio file's length); the coordinator's job is
  authorization plus first-writer-wins persistence -- the same
  idempotent-or-conflict shape every other worker-submission path here
  already uses.
- **Interval-containment validation, both sides**: a new shared
  `media_orchestration.find_chunk_for_interval(manifest, start_ms=,
  end_ms=)` decides which chunk an interval belongs to, using half-open
  `[start, end)` boundaries (the manifest's own last chunk is inclusive,
  so nothing at a source's exact end is left with no containing chunk).
  A worker uses it to pick a chunk before publishing; the coordinator
  (`EvidenceLifecycleService._validate_media_publication`, via the new
  `_require_observation_within_chunk`) independently re-derives it from
  each observation's own `source_locator` and rejects a mismatch with
  `422` -- never trusting a worker-declared chunk index on its own. An
  interval crossing a chunk boundary matches zero chunks under this rule
  and is rejected the same way, never arbitrarily assigned.
- **Visual worker** (`app/modules/media_processing/worker.py`):
  `run_once` registers a manifest once a video's real duration is known
  (via a new `register_manifest` callback threaded through `process_job`/
  `_process`, mirroring the existing `submit_ocr_batch` callback's "HTTP
  I/O stays in the orchestration layer" pattern). Because a
  coordinator-persisted chunk accepts exactly one publication ever (see
  `_validate_media_publication`'s "already completed" check), every
  sampled frame's OCR output is now buffered per assigned chunk
  (`pending_chunk_frames`) and flushed as one combined
  `MediaChunkPublication` per chunk via a new `ocr_batching.
  build_video_chunk_ocr_batch` (replacing the old one-`publish`-per-frame
  shape, which would have violated that same constraint) once frame
  sampling finishes. Non-chunked observations (whole-file metadata/
  detection/track, and the final OCR-stream completion marker) are
  unchanged, still on the plain unscoped `/observations` path.
- **Communication/audio worker**
  (`app/modules/communication_processing/worker.py`):
  `run_communication_job_with_batches` now builds and registers the
  manifest itself (`plan_audio_manifest` + `client.create_media_manifest`)
  *before* calling `_handle_local_audio` (which no longer plans its own
  manifest -- it takes an already-registered one as a parameter).
  ASR/diarization mentions already carried a `chunk_id` attribute
  (`local_pipeline.process_local_audio` has stamped one on every mention
  since Phase 4 -- inert metadata nobody consumed until now); they are
  grouped by that attribute and published one combined
  `MediaChunkPublication` per chunk. The whole-file audio-metadata
  mention (no `chunk_id`) and every other profile (transcript/diarization
  import, social exports) are unaffected, unchanged on the plain batch
  path.
- Both workers' own `client.py` gained `create_media_manifest`/
  `publish_media_chunk` methods (communication's client had neither
  before); no new migration -- every table this uses already existed
  from Phase 4.

### Producer validation gates at Phase 5 sourcing

`app/modules/graph/intelligence/sourcing.py`'s `descriptor_from_
observation` already gated communication-family observations on
`communication_signal_validation.correlation_ready`. Visual-family
observations (OCR text, detection, track) need no additional gate here:
they were already excluded from descriptor mapping entirely (raw OCR
text is source text, not a parsed identifier -- see the module's own
"Deliberately NOT mapped" list), so `visual_signal_validation`'s
`correlation_ready` flag was already moot for this specific boundary.
Structured-family (Jasraj's document/CDR/finance) observations had *no*
gate at all -- confirmed by grep, `structured_signal_validation` (the
literal name this integration task named) appears nowhere in the
codebase; the real, already-shipped attribute is `source_signal_quality`
(`structured_processing.signal_validation.SignalValidationResult`,
`{"outcome": "accepted"|"rejected"|"incomplete", ...}`), attached today
only by `cdr.py`/`finance.py` (always `"accepted"` currently, never
assumed to stay that way) -- `fir_report.py`'s regex-extracted document
mentions attach none at all.

Added a new `_is_structured_validation_rejected` check (`sourcing.py`),
applied in both `descriptor_from_observation` and the motif-edge builder
(`_call_or_transfer_edge`, which reads CDR/finance attributes directly
rather than through that function): a present, non-`"accepted"` outcome
is excluded from descriptor/motif input; an absent attribute (document
mentions today) is legacy-compatible, never rejected merely for
predating this metadata -- the identical fallthrough the pre-existing
communication check already used.

### Per-party (two-party) descriptor extension

Closed the documented Phase 5A limitation ("a `cdr_call_record`
observation's exact-blocking identity is therefore its caller's number
only") additively: `ObservationDescriptor` (`intelligence/models.py`)
gained `descriptor_id` (deterministic from case/observation/role/
normalized-value; defaults to a fresh random id only for hand-built
descriptors that never set one explicitly) and `participant_role`
(`"caller"`/`"callee"`/`"sender"`/`"receiver"`, or `None` for every
existing single-party descriptor). `descriptor_from_observation` is
unchanged -- still one descriptor, still the primary party only, for
every existing caller of that exact signature. A new
`descriptors_from_observation` (plural) is the Phase 5B entry point:
identical single-descriptor behavior for every type except
`cdr_call_record`/`financial_transaction_record`, which now return both
parties as independent, role-scoped descriptors. `pipeline.
build_case_correlation_submission` was updated to call the plural
function; nothing else changed.

`retrieval.retrieve_candidates` needed three changes to support two
descriptors sharing one `observation_id`: its internal accumulation is
now keyed by `descriptor_id` (not `observation_id`, which would let one
role's match silently overwrite the other's); a new same-origin-event
check skips any pair where both descriptors share an `observation_id`
(the two ends of one call/transfer are not two independent signals about
the same identity -- never a candidate of each other); and a final
merge-by-`(left_observation_id, right_observation_id)` pass collapses
the rare case where both roles of one record independently match the
same third observation into exactly one `RetrievedCandidate` (preserving
the one-candidate-per-observation-pair contract `correlation_candidate_
links` and every downstream scoring/persistence consumer relies on).
This pass also fixed a latent pre-existing bug where `contradiction_
reasons` was read from the last loop iteration's local variable rather
than tracked per candidate pair.

### Rules-baseline measurement and freeze

See `docs/decisions/ADR-006-phase-5-rules-baseline.md`'s "Measurement and
freeze" addendum for the full record: `phase5_preliminary_rules_v1`
(unchanged) is the frozen baseline, measured against a new versioned
synthetic benchmark (`tests/fixtures/graph/phase5_rules_benchmark.py`,
`tests/unit/graph/test_phase5_rules_benchmark.py`) at Precision@K = 1.0,
Recall@K = 1.0, 0 false links, with correct same-event suppression,
contradiction downgrade, case isolation, and determinism. `scoring.py`
gained a `RulesProfile`/`BASELINE_RULES_PROFILE` (an explicit, comparable
configuration object) so `score_candidates(..., profile=...)` can run
any named variant -- used to compare one explicit alternative, which
changed no candidate's retrieval or ranking, only its numeric score.

### End-to-end acceptance

`tests/integration/graph/test_phase5_final_acceptance_live.py` exercises
the entire flow above through real HTTP/PostgreSQL/Neo4j boundaries in
one test: case/evidence context, worker claim, manifest registration,
chunk-scoped OCR publication (plus a rejected out-of-scope-interval
attempt), two-party CDR observations (plus one deliberately-rejected
decoy) and a chat observation via the plain batch route, correlation
pass, idempotent replay, authorized and denied case-scoped reads, and a
raw-content check distinguishing a chat message's excluded free-form
body from an OCR observation's legitimately graph-facing recognized
text. It hand-builds the wire payloads a real worker would submit
(mirroring `test_full_pipeline_live.py`'s established pattern) rather
than running the real `media_processing.worker.run_once` CLI end to end;
the pre-existing `tests/integration/media_processing/
test_media_worker_live.py::
test_video_frame_ocr_batch_submission_end_to_end_live` (whose cleanup
this closeout also fixed -- see `docs/qa/known-limitations.md`) covers
that real-worker-process path for the visual side. No equivalent live
test runs the real audio worker process end to end; the audio chunk-scoped
wiring is covered by unit tests against a fake client
(`tests/unit/communication_processing/
test_communication_worker_orchestration.py`) only -- see
`docs/qa/known-limitations.md`.

### Docker-backed release gate

Running the required dedicated Compose project (`tracex-phase5-gate`)
surfaced one genuine, pre-existing gap unrelated to Phase 5's own logic:
`Dockerfile` never copied `README.md`/`alembic.ini`/`migrations/` into
the runtime image (only `app/`), so `uv run alembic upgrade head` could
never run inside *any* phase's built container -- not a regression this
phase introduced, but a release-gate command this phase's own
instructions explicitly require, so it was fixed here: a minimal `COPY
README.md alembic.ini ./` + `COPY migrations ./migrations`, three
already-committed files, no other image behavior changed. See
`docs/qa/test-results.md`'s dated entry for the full command-level
record, including the port-isolation approach used to run the dedicated
project alongside the main stack's already-running containers without
disturbing them.
