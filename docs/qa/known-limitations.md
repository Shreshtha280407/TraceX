# Known Limitations — Phase 1

## Phase 4 final release-gate environment blockers (2026-09-14)

The final Phase 4 gate remains in progress. A test-only AnyIO/pytest-asyncio
compatibility fixture now avoids the former root-task worker stall, and the
complete local suite passes (1693 passed, 61 conditionally skipped). Docker
Engine is reachable, but the fresh dedicated API image cannot complete its
locked dependency install because Docker-build DNS cannot resolve
`files.pythonhosted.org` (observed separately for `neo4j` and `lxml`). Thus
the required dedicated Compose API, clean-database migration, readiness
outage/recovery, and synthetic live E2E evidence remains unverified. This is
not evidence that a Phase 4 pipeline is complete.

## Phase 3 acceptance environment limitation (2026-09-13)

The complete test suite is green, but a fresh five-service Compose image build
could not be verified here because Docker Desktop could not reach Docker Hub
while resolving `python:3.12-slim`. PostgreSQL, Neo4j, Redis, and MinIO were
healthy and the documented host API path completed live verification. Re-run
`docker compose up --build -d` on a networked host before release packaging.
This does not change deliberate product limits: no real ASR/diarization, entity
resolution, scoring, hypotheses, cloud OCR, or automatic model downloads.

These are intentional, scoped-out gaps, not oversights. Each belongs to a later phase.

- **No graph entity resolution.** Phase 3 graph mapping leaves document,
  endpoint, and instrument values as evidence-local `SourceClaim`s. It does
  not join separately emitted observations. A CDR or finance event is
  deferred unless its own canonical record supplies every required
  source-backed field.
- **Raw document dates remain non-temporal claims.** A document relation
  observation with raw date text does not become a temporal event until a
  later explicit canonical date-normalisation policy exists.

- **No real ingestion pipeline.** `EvidenceRecordV1` is a contract only; there is no upload endpoint, no MinIO write path, and no real SHA-256 computation over uploaded bytes.
- **No real OCR engine.** `app/modules/structured_processing/document/ocr_routing.py` only makes the *routing decision* (which pages need OCR) and returns a `DEFERRED` result with a checkpoint naming them — no Tesseract, cloud OCR API, or OCR model is invoked. A later phase must consume that checkpoint and actually run OCR.
- **No NER/NLP/LLM-based extraction.** `document/fir_report.py` is fixed regex patterns only (explicit labels or structurally-distinctive formats); it will miss anything not matching those patterns (an unlabelled FIR number, a name written in free prose, a non-Indian phone/vehicle format). No source extractor for video/image/audio/social-chat exists either.
- **`document/fir_report.py`'s patterns are intentionally narrow.** Indian mobile numbers only (`[6-9]\d{9}`, no landline/international formats); Indian vehicle-plate format only; a curated, fixed list of common UPI handles (not exhaustive); `date_time_mention` captures raw matched text only and never resolves DD/MM vs MM/DD ambiguity into an actual parsed date.
- **DOCX extraction is deterministic but not layout-faithful.** `document/docx.py` concatenates paragraphs, then tables, in that order — a table embedded mid-document appears at the end of the extracted text stream, not inline where it visually sits. See ADR-002.
- **CDR/financial timestamp parsing accepts only a fixed, documented format list.** A CDR/financial export using a timestamp format outside `cdr._TIMESTAMP_FORMATS` fails the whole record as `required_field_missing` rather than being guessed at — this is the intended "never silently coerce an ambiguous format" behavior, but it does mean some real-world exports will need a documented-format update before they process cleanly.
- **No object-storage-backed `SourceResolver`.** `app/modules/structured_processing/models.SourceResolver` is a protocol; only `StaticBytesResolver` (in-memory) and a small local-file resolver (in `tests/integration/structured_processing/`) exist. A real MinIO-backed resolver is later-phase evidence-lifecycle integration work.
- **Every source-processing worker now has one real caller: a one-shot CLI, not a daemon.** `uv run python -m app.modules.{structured_processing,communication_processing,media_processing}.worker --once` and `uv run python -m app.modules.graph.worker --once` each claim/attempt one unit of work through their respective internal APIs and submit their result/outcome, then exit. There is still no daemon, consumer loop, scheduler, or container service running any of them continuously anywhere in this repository — someone (or something external to this repo) has to invoke `--once` themselves, every time.
- **Resolved (Phase 2 — Sarthak): `communication_processing.worker.process_job` now also has one real caller.** `uv run python -m app.modules.communication_processing.worker --once` (see `docs/architecture/communication-processing-worker.md`) claims one compatible job through the same internal worker API, resolves its evidence through the same claim-token-bound, SHA-256-verified input stream, and submits its result — the identical one-shot-CLI-not-a-daemon pattern `structured_processing` established, a second independent implementation of it. Still no daemon/consumer loop/scheduler; `linking.deterministic`'s `CommunicationLinkCandidate` functions remain uncalled by anything in this repository (see below).
- **Only two of `communication_processing`'s seven processor profiles are reachable through a real evidence upload.** `evidence_lifecycle/routing.py` routes `SourceType.AUDIO` → `audio_metadata_v1` and `SourceType.CHAT` → `generic_social_json_v1` only. `transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, and `instagram_export_v1` are fully implemented, unit-tested (via directly-constructed `WorkerJobV1`s), and listed in `SUPPORTED_PROCESSORS`, but no `SourceType`/routing rule ever assigns a real uploaded file to them — reported, not fixed, per this task's explicit instruction not to change shared routing without prior approval. See `docs/architecture/communication-processing-worker.md`'s "Routing boundary" and `docs/architecture/phase-2-decisions.md`'s open questions for the two proposed (not-yet-approved) additive shapes.
- **A revoked worker-credential digest can never be reused, including by a test helper's fixed dev-only token literal.** `credential_digest` is unique per row regardless of `status` (`worker_credentials`'s DB constraint), and revocation is intentionally permanent — there is no "reactivate" path in `access_control`'s production repository or CLI, by design. `structured_processing`'s and `communication_processing`'s live-test `_ensure_worker_credential` helpers both now fail loudly with an actionable message (rather than crashing on a raw `IntegrityError`) if their configured token's digest belongs to an already-revoked row — see `docs/architecture/phase-2-decisions.md`'s "Why `_ensure_worker_credential`'s test helper now fails loudly..." section and `docs/qa/test-results.md`'s Phase 2 Sarthak entry for the real collision this surfaced. Practical effect: if a local dev `WORKER_TOKEN`/test-literal token is ever revoked (deliberately, or by an operator cleaning up stale rows), that exact literal value must be replaced, not reused.
- **Resolved (Phase 2.2): the structured-processing worker can now fetch a claimed job's evidence bytes.** `GET /api/v1/internal/worker-jobs/{job_id}/input` (Nipun, `app/modules/evidence_lifecycle/`) streams a currently-claimed job's evidence through the API itself — claim-token-bound, lease-scoped, never an object key/bucket/MinIO endpoint/credential. See `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section. `structured_processing.client.WorkerApiClient.fetch_input`/`input_resolver.LiveInputResolver` (unchanged in shape from their original design) now consume it for real, including a worker-side SHA-256 verification of the received bytes before parsing. `InputResolutionUnavailableError`/`DEFERRED`/`input_resolution_unavailable` is retained as a defensive fallback (e.g. an older API build, a genuine transient failure), not removed.
- **Resolved (Phase 2.3): `generic_tabular_v1`/`generic_json_v1` are now reachable through the live upload path.** Two new, additive `SourceType` values (`structured_tabular` → `text/csv`/XLSX, `structured_json` → `application/json`) route real uploads to these profiles — see `docs/architecture/evidence-lifecycle.md`'s routing table and `docs/architecture/phase-2-decisions.md`'s "Phase 2.3 Decisions" for the approved, team-reviewed resolution (a new `source_type`, not content-shape-sniffing).
- **A storage failure mid-stream on the worker-input endpoint cannot become a clean error response.** Once `GET /api/v1/internal/worker-jobs/{job_id}/input`'s response headers are sent, a subsequent read failure from MinIO simply terminates the connection — an inherent HTTP-streaming limitation (status code and headers can't change after they're sent), not something this endpoint's own code can work around. A failure *before* headers are sent (including "object missing") is unaffected and still returns the normal safe-500 envelope.
- **No entity resolution.** `EntityV1.created_from_observation_ids` exists, but nothing in this repo actually resolves raw `ExtractedEntityMention`s into entities, merges duplicates, or deduplicates aliases. `app/modules/graph/projection.py` projects whatever `EntityV1` it is handed — it never merges, dedupes, or infers entities itself.
- **Graph projection exists as a foundation, but nothing calls it yet.** `app/modules/graph/` (schema, repository, projection, safe queries) is built and tested (`docs/architecture/neo4j-graph-foundation.md`), but no API endpoint, worker, or orchestrator in this repo invokes `project_evidence`/`project_observation`/`project_entity`/`project_event` yet — there is no ingestion pipeline to call it from (see the two items above). It is exercised directly by `tests/unit/graph/` and `tests/integration/graph/` only.
- **`SUPPORTS` (`Observation` -> `Event`) is schema-defined but not populated.** Neither `ObservationV1` nor `EventV1` carries the link needed to know which observation(s) an event was built from in the frozen `V1` contracts. See `docs/decisions/ADR-001-graph-projection-and-case-isolation.md` (Decision 4) — flagged for team review once a later phase builds events from observations.
- **No candidate identity links or review workflow.** `docs/architecture/graph-taxonomy-v1.md` documents the identity-safety rules (alias/fuzzy-match/transliteration/corroboration are never proof of identity) that a later phase's candidate-link relationship type and human-review workflow must respect, but no such relationship type or workflow exists yet.
- **No cross-modal correlation, candidate scoring, or hypothesis engine.**
- **No graph analytics.** No centrality, community detection (Leiden/WCC), PageRank, or motif search — explicitly out of scope per `CLAUDE.md` for this phase and the next.
- **No MFA, no SSO, no external identity provider.** `app/modules/access_control/` is single-factor email+password only, by explicit design for this phase.
- **No production secret manager.** `AUTH_JWT_SECRET`/`POSTGRES_PASSWORD`/etc. come from a git-ignored `.env` file for local development only; nothing here integrates with Vault/AWS Secrets Manager/etc.
- **No browser cookie/CSRF workflow.** Access tokens travel in the `Authorization` header, refresh tokens in an explicit request body — no cookie is ever set, so no CSRF defense exists or is needed *yet*. If a future browser frontend adopts cookie-based token storage, CSRF hardening becomes a hard requirement at that point — see `docs/architecture/security-boundaries-v1.md`.
- **No CORS configuration.** No `Access-Control-Allow-Origin` header is ever set (not even the endpoints that exist). Add explicit development origins (never `*`) only once a browser frontend actually needs it.
- **No full case-management API.** `cases`/`case_memberships` tables and repository methods exist as a minimal access-control anchor only — there is no `/api/v1/cases` endpoint to create/list/update a case or its memberships. `app/modules/access_control/dependencies.py`'s `require_case_*` dependencies are the integration points for Nipun's later case/evidence endpoints to build against.
- **No per-evidence classification.** `policy.authorize_case_action`'s `resource_classification` parameter is a forward-compatible hook, unused by anything in this phase (only case-level `classification` is checked).
- **`require_authenticated_user` reads the database on every authenticated request.** A deliberate choice (so logout/deactivation take effect immediately, not just at token expiry — see ADR-003, Decision 1) with an unmeasured latency cost at real load; revisit once real case/evidence endpoints exist to load-test against.
- **Resolved (Integration Hardening 1): the previously-reported "`BaseHTTPMiddleware` interaction" was re-investigated and found to be a different issue than originally diagnosed.** The observed symptom (an unhandled exception escaping to a test caller instead of returning a safe response) was actually caused by httpx's `ASGITransport` test-only default (`raise_app_exceptions=True`), not by `BaseHTTPMiddleware` — verified against a live `uvicorn` process, which returned the correct safe response the whole time. A separate, real gap *was* found during the same investigation: Starlette dispatches the bare-`Exception` handler from its outermost `ServerErrorMiddleware`, bypassing all user middleware's header injection for that one response path. Both are now fixed centrally in `app/core/errors.py`; the per-endpoint/per-dependency `_internal_error` workaround in `app/modules/access_control/` was removed as redundant. Full writeup in `docs/architecture/security-boundaries-v1.md`'s "Integration Hardening 1" section and `docs/decisions/ADR-003-...md`, Decision 10.
- **Remaining framework characteristic (not a bug): `ASGITransport(raise_app_exceptions=True)` (httpx's default) re-raises a truly-unhandled exception to the test caller instead of returning the response the app actually sent.** This is Starlette's own documented `ServerErrorMiddleware` behavior (send the response, then re-raise so a server/test harness can also observe it), not something this project's code controls. Every test fixture in this repo that drives HTTP requests through the real app now explicitly sets `raise_app_exceptions=False` to match real client/server behavior; a *new* test file that constructs its own `ASGITransport` without this setting will reproduce the original, misleading symptom if it ever exercises a genuinely-unhandled-exception path.
- **No Merkle checkpoints or signatures.** `app/core/canonical.py` provides stable hashing as a utility; no chain, checkpoint, or Ed25519 signature is implemented.
- **Only one domain migration beyond the Alembic baseline.** `migrations/versions/7e8499f34f29_access_control_foundation.py` adds `users`/`cases`/`case_memberships`/`auth_sessions`/`security_audit_events`; no other domain tables exist yet (evidence/observation/entity/event storage remains PostgreSQL-free per `CLAUDE.md`'s Phase 1 scope).
- **`/readyz` checks are liveness/connectivity only.** They open a minimal connection (`SELECT 1`, `RETURN 1`, `PING`, `bucket_exists`) and nothing more — they don't validate schema state, credentials scope, or bucket policy.
- **Live integration tests depend on local environment state.** `tests/integration/test_readiness_live.py` only runs meaningfully when a developer has a real `.env` and has started `docker compose up -d postgres neo4j redis minio`; in CI or a fresh clone without that, all four of its tests report as skipped (not failed) — this is by design, not a gap in coverage of the default `uv run pytest` run, but it does mean the *default* run doesn't prove real infrastructure connectivity.
- **`get_entity_events` range filtering only compares `Event.event_time`.** An event recorded with only a `time_window` (no single `event_time`) is included in an unfiltered call but will not match a `start_time`/`end_time` range filter in this phase.
- **No load, chaos, or performance testing.** Out of scope for a Phase 1 foundation.
- **No real ASR or diarization.** `app/modules/communication_processing/audio/{transcript_import,diarization_import}.py` only validate/import already-produced segments; they never generate a transcript or speaker attribution from raw audio. A job requesting one, given only raw audio, returns `DEFERRED` (`deferred_requires_asr`/`deferred_requires_diarization`) — a later phase must supply the actual ASR/diarization system and feed its output through these import functions. **Extended (Phase 3 — Sarthak): this absence is now a typed boundary, not an implicit one.** `audio/asr_adapter.py`/`audio/diarization_adapter.py` define an `AsrAdapter`/`DiarizationAdapter` `Protocol` each, with exactly one production implementation (`UnavailableAsrAdapter`/`UnavailableDiarizationAdapter`) that always reports `state=UNAVAILABLE` and always raises rather than fabricate a result. Not implementing a real model was a deliberate decision, not an oversight: this module's own `test_module_safety.py` (written in an earlier phase by this same owner) statically bans `numpy`/`torch`/`transformers`/`whisper`/`faster_whisper`/`pyannote`/`speechbrain`/`librosa`/`sklearn` — every practical local ASR/diarization toolkit — and there is no ML-free stdlib alternative. See `docs/architecture/phase-3-decisions.md`'s Sarthak section, flagged there for team review (whether reversing that ban for a future phase is desired).
- **Only WAV is supported for local audio inspection.** MP3/M4A/AAC/OGG/FLAC/WMA/Opus all route to `unsupported_audio_format` (never fabricated metadata) — see `docs/architecture/audio-social-and-communication-processing-v1.md`. A future approved media-probe adapter (still not `ffmpeg`/subprocess-based, per CLAUDE.md's non-goals for this module) would be needed to extend this.
- **WhatsApp export parsing supports exactly one documented plain-text shape.** `DD/MM/YY, HH:MM - Sender: text` (with continuation lines). Exports using a different date/time format, a different locale's separator, or the newer "no dash" WhatsApp export variant are not recognized and fail as `malformed_chat_export` rather than being guessed at.
- **Telegram's rich-text entity-array `"text"` form is not parsed.** Only a plain-string `"text"` value is read; a message whose `"text"` is the formatted-entities array form (bold/links/mentions) is recorded with `text_present=False` rather than attempting partial extraction.
- **Instagram export parsing supports the basic "Download Your Information" shape only.** No message ID or reply-reference field exists in this shape, so `message_id`/`reply_to` are always `None` for Instagram observations (provenance is still non-empty via `json_path`).
- **`aliases/transliteration.py`'s character tables are intentionally small.** They cover common Devanagari/Gurmukhi consonants, independent vowels, and vowel signs — sufficient for typical given names, not exhaustive of either script (no conjunct consonants beyond simple virama suppression, no rare/archaic characters). A token with any uncovered character yields `category="not_generated"`, never a partial transliteration — see `docs/architecture/multilingual-alias-candidates-v1.md`.
- **Script detection supports Latin, Devanagari, and Gurmukhi only.** Any other script (Cyrillic, Han, Arabic, Bengali, Tamil, etc.) is reported as `Script.UNKNOWN` — correctly *not* silently misclassified as one of the three supported scripts, but also not transliterated.
- **`find_same_conversation_candidates` never fires within a single evidence file.** By design (ADR-005, Decision 5) — see `docs/architecture/audio-social-and-communication-processing-v1.md`.
- **No orchestration calls `linking.deterministic`'s candidate functions yet.** Unlike `process_job` (now callable via the `--once` CLI — see above), `CommunicationLinkCandidate` generation has no caller anywhere in this repository — built and tested in isolation only, same situation `graph.projection` is in.
- **`username_or_handle` extraction has no platform-specific validation (Phase 3 — Sarthak).** `social/identifiers.py` accepts any bare `@name` token (2-32 word characters) regardless of whether it's a plausible handle on any real platform — every such extraction is already an unresolved extracted claim, never a verified identity, so this is a precision limitation, not a safety gap.
- **`phone_number`/`email_address`/`url` extraction from chat text (Phase 3 — Sarthak) shares `fir_report.py`'s exact documented limitations.** Indian mobile numbers only (no landline/international formats); a URL match is a whitespace-delimited span, not a strict RFC 3986 parse; two different observation types may legitimately reference overlapping/identical substrings (e.g. an email embedded in a matched URL) with no cross-pattern overlap resolution attempted.
- **No live-model benchmark exists for ASR/diarization extraction quality (Phase 3 — Sarthak), since no real model runs in this phase.** The fixture ASR/diarization adapters (`tests/fixtures/communication_processing/{asr,diarization}_fixture_adapter.py`) return exactly the segments they were constructed with in a test — never a measured result — so no precision/recall/model-performance figure is claimed anywhere for this module.
- **No attachment-reference observation exists yet (Phase 3 — Sarthak).** The task brief's approved observation-type list for social/chat evidence includes an "attachment reference" claim (bounded metadata/provenance for a message's media attachment, never recursively processed), but none of the four chat parsers (`whatsapp.py`/`telegram.py`/`instagram.py`/`json_records.py`) currently read or model any attachment-related field — a message with a media attachment is parsed exactly like a text-only message today, with no signal that an attachment was present at all. Not implemented this phase: it is a genuinely new capability (a new field on `ChatMessageRecord`, per-platform detection logic, a new observation type/attribute) rather than a gap in existing, already-designed behavior, and was judged out of scope alongside the other deliberately-deferred additions this phase (real ASR/diarization, non-WAV audio). Flagged for team review in `docs/architecture/phase-3-decisions.md`.
- **Resolved and verified end-to-end (Phase 2 routing fix): all seven of `communication_processing`'s processor profiles are now reachable through a real evidence upload.** Five new, additive `SourceType` values (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`) route real uploads to `transcript_import_v1`/`diarization_import_v1`/`whatsapp_export_v1`/`telegram_export_v1`/`instagram_export_v1` — previously only `audio_metadata_v1`/`generic_social_json_v1` (via the pre-existing `audio`/`chat` source types) were reachable. See `docs/architecture/evidence-lifecycle.md`'s routing table and `docs/architecture/phase-2-decisions.md`. The routing fix alone was insufficient: `communication_processing/worker.py`'s own `_validate_source_type` independently re-checked source type by group (`SourceType.AUDIO` for all three audio profiles, `SourceType.CHAT` for all four chat profiles) and rejected every one of the five new, disjoint source types with `unsupported_source_type` — a genuine bug only surfaced by actually running the real worker against a real uploaded job, not by routing-level testing alone. Fixed by replacing the group check with an exact per-profile `_PROFILE_REQUIRED_SOURCE_TYPES` mapping. Verified live against the full Docker Compose stack on 2026-09-11: real evidence upload → server-selected processor → real `communication_processing` worker `--once` claim/input/result → persisted `ObservationV1` → real graph projection via `graph.worker --once` → confirmed via both a direct Neo4j query and the `/api/v1/cases/{case_id}/graph/observations` read endpoint, for all five new source types (`audio_transcript`, `audio_diarization`, `whatsapp_chat`, `telegram_chat`, `instagram_chat`). All test data (case, user, worker credential, evidence, jobs, observations, results, and Neo4j nodes) was cleaned up afterward. See `docs/qa/test-results.md` for the dated entry.
- **Resolved (Phase 2 closeout, Nipun): a real local object detector, OCR engine, and deterministic tracker now exist.** `analysis/onnx_detector.py` (YOLOX-s via `onnxruntime`, checksum-verified, CPU by default with an auto-detected optional CUDA execution provider), `analysis/tesseract_ocr.py` (the local `tesseract` binary via `pytesseract`, no cloud OCR), and `analysis/iou_tracker.py` (the same deterministic greedy IoU algorithm `fake_tracker.py` always used, promoted to a named, versioned, configurable production component). `fake_detector.py`/`fake_tracker.py`/`fake_ocr.py` remain unchanged as deterministic CI-only stand-ins. See `docs/architecture/media-processing-worker.md`'s "Real local detector"/"Real local OCR"/"Real, deterministic tracking" sections. Still no PaddleOCR/cloud OCR API, no Ultralytics/torch-based detector, no ByteTrack/DeepSORT — a deliberately lightweight, local-only, non-GPU-required stack.
- **No face recognition, person re-identification, or cross-camera identity matching.** `TrackSegment.local_track_id` is valid only inside one evidence item and one processing run; this module never compares tracks across evidence items or cases, and no biometric identification exists anywhere in this module — explicit non-goals per `CLAUDE.md`. The real detector emits only anonymous, generic object-class labels (COCO's 80 classes) — never a person's identity, name, or any biometric feature.
- **`video/frames.py` extracts each sampled timestamp via its own `ffmpeg` subprocess invocation.** Correct and simple, but not the fastest possible approach for a very large sample plan; `limits.DEFAULT_MEDIA_LIMITS.max_sampled_frames` bounds the worst case. See `docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md`'s open questions.
- **GPU visibility detection is `nvidia-smi`-only** (`capability.detect_capability()`, used for the benchmark/performance report) **— no AMD/Apple-Silicon-equivalent check.** Separately, `OnnxObjectDetector`'s own CUDA-provider detection (`onnxruntime.get_available_providers()`) is independent of this and is what actually gates real GPU-accelerated inference — see `docs/architecture/media-processing-worker.md`. Neither check has been exercised against real GPU hardware in this development environment (CPU-only sandbox) — `onnxruntime-gpu` is documented (`pyproject.toml`'s `[project.optional-dependencies] gpu` group) but not verified against a real CUDA device.
- **Resolved (Phase 2 completion, Gaurav; re-routed in the Phase 2 closeout, Nipun): media worker orchestration.** `app/modules/media_processing/worker.process_job` is callable via its own one-shot `--once` CLI, now joined by a continuous `--loop` mode (see below), verified live end to end for both image and video evidence, including automatic flow into the existing graph-projection queue. `evidence_lifecycle/routing.py` now routes every real image/video upload to `media_detection_v1` (previously `media_metadata_v1` only) — see `docs/architecture/phase-2-decisions.md`'s "Real local media inference closeout".
- **Resolved (Phase 2 completion, Gaurav): `video/x-matroska` upload support.** `evidence_lifecycle/routing.py` accepted `video/x-matroska` for `SourceType.VIDEO` since Phase 1, but `media_processing/source.py`'s `MediaKind` had no matching entry — a real `.mkv` upload would pass routing and then fail inside the worker with `unsupported_content_type`. Found only by wiring the worker to a real claimed job's resolved content type, not by testing routing or `classify_media` in isolation against each other. Fixed additively (`MediaKind.VIDEO_MATROSKA`); no other code needed to change since `ffprobe`/`ffmpeg` are container-format-agnostic.
- **No object-storage-backed `SourceResolver` for media.** `app/modules/media_processing/source.SourceResolver` is a protocol; only `StaticBytesResolver` (in-memory) exists. A real MinIO-backed resolver is later-phase evidence-lifecycle integration work, same as `structured_processing`.
- **CI runs verification but isn't wired to branch protection.** `.github/workflows/ci.yml` runs `ruff format --check`, `ruff check`, `mypy app`, `pytest`, and `docker compose config` on push/PR, but enforcing it as a required check is a GitHub repository-settings change, out of scope for this task.
- **Resolved (Phase 2 closeout, Nipun) for `media_processing`/`graph`: continuous `--loop` modes now exist.** `uv run python -m app.modules.media_processing.worker --loop` and `uv run python -m app.modules.graph.worker --loop` run continuously (configurable idle-poll interval, bounded exponential backoff, a `max_consecutive_failures` cutoff, graceful SIGINT/SIGTERM shutdown that finishes any in-flight job/batch before stopping) — see `docs/architecture/media-processing-worker.md`'s "Continuous operation" and `docs/architecture/graph-projection.md`'s identical section. `--once` remains available and unchanged for both. `structured_processing.worker`/`communication_processing.worker` still have `--once` only — no `--loop` mode was added to either in this phase (out of this task's scope; `evidence_lifecycle`'s durable `worker_jobs` table + Redis `RPUSH` dispatch, and the atomic `FOR UPDATE SKIP LOCKED` claim both workers already build on, are unchanged and would support the identical `--loop` pattern if a future phase adds it). No `BLPOP` loop, Celery, Kafka, or Kubernetes-based scheduler exists anywhere in this repository — `--loop` is a single long-running process per instance, not a distributed queue consumer.
- **No automatic redrive of undispatched jobs, or of `deferred`/`cancelled` jobs.** A `worker_jobs` row with `dispatched_at IS NULL` means the Redis publish was attempted and failed (or the process crashed between commit and publish) — the row itself is durable and queryable, but nothing in this repository automatically retries the publish. Separately, once a job reaches `deferred` or `cancelled` (a valid terminal submission, per `docs/architecture/worker-job-lifecycle.md`), nothing automatically requeues it either. A later phase must decide whether either is a scheduled sweep or built into the eventual consumer's own startup.
- **Resolved (Phase 3 — Aditya): max-attempt cutoff on job claiming.** `worker_jobs.max_attempts` (default `5`, configurable via `WORKER_JOB_MAX_ATTEMPTS`) now bounds reclaiming. A job whose lease keeps expiring is reclaimed up to that limit; once exhausted, `claim_job` sweeps it to a durable terminal `failed` state (via the existing `submit_result` path, with a safe `retry_exhausted` error code) rather than leaving it reclaimable forever. See `docs/architecture/worker-job-lifecycle.md`'s "Lease and retry policy, stated plainly".
- **Resolved (Phase 2.4): per-worker credentials replace the shared-secret boundary.** `require_worker_principal` now authenticates against a real `WorkerCredentialRecord` (digest-only storage, pepper, revocable, per-worker `allowed_processor_names`) provisioned by the trusted-operator `app.modules.access_control.worker_credentials` CLI — `Settings.worker_shared_secret` no longer exists, and there is no fallback path that accepts a bare shared secret. `/claim` enforces per-worker processor scoping; `/result`/`/input` require the caller to *be* the worker identity bound to that job. See `docs/architecture/worker-identity-and-security.md`.
- **Resolved (Phase 2 closeout, Nipun) for `media_processing`: lease renewal now exists.** `POST /api/v1/internal/worker-jobs/{job_id}/renew` extends a currently-`running`, unexpired-lease job's lease (same claim-token + worker-identity authorization as `/result`/`/input`; a lease can never be extended past its own expiry, so a legitimate reclaim by another worker after a real expiry always wins). `media_processing.worker`'s `_lease_heartbeat` calls it automatically on a background thread during real analysis. `structured_processing.worker`/`communication_processing.worker` do not use this endpoint (no `--loop` mode exists for either yet — see above), though nothing prevents them from adopting it identically in a future phase. **Extended (Phase 3 — Aditya):** renewal is now capped at an absolute maximum lease lifetime (`WORKER_LEASE_MAX_SECONDS`, default `3600`, measured from the original claim time) computed atomically inside the same `UPDATE ... RETURNING` statement via `LEAST(...)` — no repeated renewal can extend a lease indefinitely. See `docs/architecture/worker-job-lifecycle.md`.
- **Resolved (Phase 2.4): denied worker actions are now audit-logged.** `worker_authentication_denied`, `worker_processor_scope_denied`, and `worker_job_access_denied` are recorded via the existing `access_control.audit` service (safe fields only — never a bearer/claim token, object URI, or request body) for every rejected claim, scope mismatch, or claim-token/identity check on `/api/v1/internal/worker-jobs/*`. See `docs/architecture/worker-identity-and-security.md`'s "Audit event policy and safe fields".
- **`EvidenceRecordV1.processing_status` still doesn't reflect job completion (Phase 2.1).** Even after a worker result is submitted and the job reaches a terminal state, `evidence_records.processing_status` remains whatever it was set to at upload time (`queued`) — this module deliberately did not touch `evidence_records` in Phase 2.1 (out of the task's explicit scope, and the `deferred`/`cancelled` -> status mapping is genuinely ambiguous). A later phase should decide the exact status-mapping policy.
- **No case CRUD API still.** Evidence-lifecycle endpoints require an existing `cases`/`case_memberships` row (seeded directly via `app.modules.access_control.repository`, same as every integration test in this repo); there is still no `/api/v1/cases` endpoint to create one through the API itself.
- **Resolved (Phase 2.4): denied-access-attempt auditing is now wired for case-scoped endpoints.** `app.modules.access_control.dependencies.require_case_action` now records a `case_access_denied` audit event (via `record_audit_event_safely`, so an audit-sink failure never turns the `403` into a grant) before raising — closing the gap previously documented here and in `docs/architecture/phase-2-decisions.md`'s Phase 2 "Open questions".
- **`source_type=other` has no registered processor.** Uploads declaring it are rejected outright (`422`) rather than accepted with no job — see `docs/architecture/evidence-lifecycle.md`'s routing table.
- **`object_uri` is still never returned by any API response**, including to an authorized case member — by design (see "No raw-evidence-download API" in `docs/architecture/evidence-lifecycle.md`), and this is unchanged by Phase 2.2's worker-input-stream endpoint (which streams bytes through the API, never the object key itself, and only to a worker holding a live claim token for that exact job — see "Worker evidence delivery" in the same doc). There is still no *human*-facing authorized-viewing/download path for a case member; a later phase must add one using `MinioSourceResolver`/`ObjectStorage.open_stream` internally, not a raw object URL.
- **`Idempotency-Key` matching does not consider `classification` or `parser_profile`.** Two requests with the same key, same `source_type`/`content_type`/`sha256`, but a different declared `classification` or `parser_profile` are treated as an identical replay (the first request's values win silently) rather than a conflict — a narrow, deliberately scoped-down comparison (see `docs/architecture/phase-2-decisions.md`), worth reconsidering if either field turns out to matter for a real workflow.
- **Rotating or removing `WORKER_CREDENTIAL_PEPPER` invalidates every existing worker credential's digest at once (Phase 2.4).** `hash_worker_credential` recomputes `HMAC-SHA256(pepper, token)` at verification time from whatever pepper is currently configured — there is no per-credential pepper version, so a pepper change requires re-provisioning (`rotate`) every affected worker, not just the one being rotated. Documented, not automated; acceptable for a foundation phase with a small, manually-managed worker fleet.
- **No maximum-attempt cutoff or fleet-size limit on worker credentials (Phase 2.4).** `worker_credentials create` will provision an unbounded number of credentials, and a worker whose lease keeps expiring can be reclaimed forever (a pre-existing Phase 2.1 limitation, unchanged by this phase) — there is no automatic revocation of a credential that fails authentication repeatedly, and no cap on how many active credentials may exist at once.
- **The sandbox's Docker build network is intermittently flaky reaching PyPI.** `docker compose up --build` failed three times in a row on three different pre-existing dependencies (`opencv-python-headless` ×2, `asyncpg`) with a DNS resolution error, then a fourth attempt hit the same transient error on `neo4j` before completing successfully — confirming this is environment-level network flakiness, not a dependency or code issue. The full evidence-lifecycle flow was verified live twice: first via `docker compose up -d postgres neo4j redis minio` + `uv run uvicorn app.main:app` (documented "Option B"), then again against the fully containerized `api` image once the build succeeded — see `docs/qa/test-results.md` for both. Not a blocker; noted in case a future build in this same environment needs a retry.

- **Resolved (Phase 3 — Jasraj): real document/OCR/NER/relation extraction and real vectorized CDR/finance batch processing now exist.** Nipun's Phase 3 built the durable, tested, secure *submission path*; this phase built the real producer that uses it — real per-page PDF trust classification, real local Tesseract OCR for scanned/untrustworthy pages, deterministic layout normalization, real regex + local NER (deterministic fallback and a real bootstrapped spaCy model) mention extraction, rule-based relation/event extraction, and real Polars/PyArrow-chunked CDR/finance normalization. See `docs/architecture/document-structured-processing.md`.
- **No entity resolution, graph taxonomy change, or scoring introduced by Phase 3.** Observations submitted via a batch flow through the exact same, unmodified `graph_projection_jobs` outbox and `app/modules/graph/` projection logic every other accepted observation already does — nothing about entity resolution, relationship scoring, or the hypothesis engine changed or was implemented.
- **The progress non-regression check is a soft, read-then-decide guard, not a hard database constraint.** `submit_observation_batch` reads the latest `worker_progress_events` row, validates, then writes — all within one request but not inside a `SELECT ... FOR UPDATE`-guarded critical section. Acceptable because a real worker submits its own batches for one job strictly sequentially (no legitimate concurrent-batch-submission use case exists yet); a future phase needing a hard linearizability guarantee here should add row-level locking. See `docs/architecture/phase-3-decisions.md`.
- **No public read endpoint exists yet for transformation-provenance records.** `TransformationProvenanceV1` rows are durable and queryable via `EvidenceLifecycleRepository.list_transformations_for_batch`, but there is no `GET` API exposing them to an analyst — the task brief explicitly allows this ("even if no public read endpoint is yet required"). A future phase may want a case-scoped provenance-review endpoint.
- **The batch-submission route's path (`/api/v1/internal/worker-jobs/{job_id}/observations`) differs from the master plan's illustrative example (`/internal/jobs/{job_id}/observations`).** A deliberate coherence choice (extending the existing worker-internal router rather than adding a second one) — see `docs/architecture/phase-3-decisions.md`'s "Open questions for team review". Flagged, not treated as a defect.
- **`is_final_batch` is bookkeeping metadata only.** It is stored on the `observation_batches` row for a worker's own observability but never transitions `worker_jobs.status` — a worker must still submit a terminal `WorkerResultV1` via the pre-existing `/result` endpoint to complete a job, exactly as before this phase.

- **No worker-credential expiry (Phase 3 — Aditya, deliberate).** `WorkerCredentialRecord` has no `expires_at`/TTL concept — a credential is valid until an operator explicitly revokes it via the existing CLI, with no automatic time-based expiry. The task brief's own wording made this conditional ("if expiry exists in the current design"); it does not exist, and adding it was judged outside the smallest-necessary extension for this phase. See `docs/architecture/phase-3-decisions.md`'s open questions.
- **No cryptographic tamper-evidence over the audit trail.** `worker_job_claimed`/`worker_job_lease_renewed`/`worker_job_reclaimed`/`worker_job_completed`/`worker_job_failed`/`worker_job_access_denied`/`worker_job_retry_exhausted` (and every other `security_audit_events` row) are ordinary, unsigned PostgreSQL rows — there is no Merkle chain, checkpoint, or Ed25519 signature over them yet. Per `CLAUDE.md`, that is explicitly Phase 6 work; this phase's audit events are evidentiary-adjacent operational logging only, not yet a tamper-evident chain.
- **No maximum-attempt cutoff or fleet-size limit on worker credentials, still.** Unchanged from the Phase 2.4 note above — `worker_jobs.max_attempts` (Phase 3) bounds job retries, not how many credentials a fleet may hold or how many consecutive authentication failures a single credential may accrue before automatic revocation. Both remain manually-operated.

- **`fir_report.py`'s `police_station_mention`/`legal_section_mention` regex patterns can over-match in free-flowing prose without line breaks (Phase 1, exposed more visibly by Phase 3's real OCR/prose fixtures).** The pattern matches up to 60 non-newline/comma/semicolon characters after a label — correct and precise for realistic FIR documents (which have line breaks between fields), but a single long run-on sentence with no punctuation after "Police Station:" can cause it to greedily consume far more than the intended value (observed directly in `docs/qa/test-results.md`'s real OCR-precision measurement, where it captured a trailing clause). Not fixed in this phase (out of scope — `fir_report.py`'s regex patterns are Nipun's Phase 1 code) but now directly observable through real OCR/prose fixtures where it previously wasn't exercised as visibly.
- **The deterministic NER fallback's gazetteer/heuristics are intentionally narrow.** ~35 stopwords, ~14 organizational suffixes, and ~36 Indian city/state names — sufficient for the synthetic fixtures in this repository, not remotely exhaustive of real Indian names, organizations, or places. A capitalized word/phrase outside these fixed lists is either missed (a real name) or occasionally mis-flagged (an unlisted proper noun matching the `PERSON` Title-Case heuristic). See `document/ner_fallback.py`'s own module docstring.
- **The real local NER model (`en_core_web_sm`) is a small, general-purpose English pipeline, not fine-tuned for Indian names/places/investigative vocabulary.** It will miss some real mentions and occasionally mislabel others — see `docs/architecture/document-structured-processing.md`'s "Honest accuracy note" and the actual measured field-match precision/recall in `docs/qa/test-results.md`. Neither NER adapter exposes a native per-entity confidence score; both use a fixed, documented confidence tier per adapter (`0.50` fallback / `0.75` real model), not a per-instance model probability.
- **OCR bounding-box precision is line-level, not word-level.** `DocumentPageOcrEngine` groups tesseract's own per-word output into per-line regions (the same choice `media_processing`'s own OCR adapter already made) — a regex/NER match against a substring of a line resolves to that whole line's bounding box, not a tighter box around just the matched word(s). Exact enough to locate a claim on the page; not pixel-exact to the specific matched text.
- **`document/relations.py`'s `PROXIMITY_MAX_CHARS = 200` threshold is a fixed, documented constant, not corpus-tuned.** A relation observation's confidence (`0.60`) already reflects that it is an inference from proximity, never an asserted fact, so a threshold that's occasionally too wide or too narrow for a specific document's layout does not create a false certainty — only a missed or extra *candidate* association for a human/later-phase reviewer to judge.
- **CDR/finance timezone resolution defaults to `Asia/Kolkata`, and an unrecognized explicit `source_timezone` value falls back to that default rather than rejecting the record.** A deliberate choice consistent with this module's existing India-only assumptions (Indian mobile format, `Rs.`/`₹` prefixes) — see `docs/architecture/phase-3-decisions.md`. A CDR/finance export using a genuinely different, unstated timezone convention will have its timestamps silently (though provenance-recorded) interpreted as IST rather than rejected.
- **XLSX chunked reading has no native vectorized disk-level reader.** `structured/chunked_processing.iter_xlsx_record_chunks` uses `openpyxl`'s existing lazy row iterator (unchanged from Phase 1) for the actual file read, only assembling each bounded group of rows into a `pyarrow.Table` after the fact — genuinely vectorized *batch handling*, not a vectorized *read*, since neither Polars nor PyArrow has a native chunked-XLSX reader without an additional optional engine this project does not depend on.
- **The real local NER model asset's live-loaded behavior (`SpacyNerAdapter` against the bootstrapped `en_core_web_sm` directory) was verified directly in this development sandbox**, including a real `bootstrap_ner_model.py` download-and-verify-and-extract run — but this sandbox's own tooling policy blocks any `pip install`/`uv pip install`-shaped command, even for a legitimate, documented, operator-invoked bootstrap step. This did not block verification here because the bootstrap design (extract a directory from a downloaded, checksum-verified zip archive) needs no such step at all — flagged for team awareness that this class of command is restricted in this specific sandbox, not because any real capability was left unverified.
- **`SourceType.DOCUMENT` still has no route to a structured-JSON report export shape.** Only PDF/DOCX/TXT are accepted for documents (unchanged since Phase 1) — a hypothetical JSON-based report export format was not added in this phase (no such shape is defined anywhere in this codebase), consistent with the task's "smallest additive routing change" instruction and the fact that no routing change was needed for any format this phase actually processes.
- **Media OCR is line-level and English-first.** `ImageOcrAdapter` preserves the original image/frame geometry exactly, but it does not yet deskew, denoise, threshold, crop, upscale, or emit word-level observations. Small, blurred, rotated, low-contrast, or non-English text may be missed; additional language packs must be installed by an operator and selected explicitly. No language/model is downloaded by startup, Docker build, or a normal test.
- **Live media OCR verification requires local infrastructure and local tooling.** The live suite deliberately self-skips when the API stack, Tesseract/language pack, readable local font, or (for video) ffmpeg/ffprobe is unavailable. A skip is reported as unavailable, never as a fixture-backed real OCR pass.
## Phase 4 media orchestration

- The coordinator persists only media-work metadata. It does not decode media,
  verify worker-generated artifact bytes, or perform GPU/LAN worker health
  scheduling. A worker must report existing truthful `deferred`/`failed`
  terminal status when unavailable; full fleet policy remains Aditya's work.
- Manifest creation is a trusted coordinator service seam, not a public or
  worker-self-service API. Real media worker adoption and end-to-end Docker/GPU
  verification are deliberately deferred to the Phase 4 merge wave.

# Phase 5 graph/correlation integration

## Phase 6 Part 2 integrity access

PostgreSQL append-only triggers protect ordinary application/database roles,
but a database superuser can disable triggers or alter rows. There is no
external anchoring, KMS/HSM, or key rotation service. Integrity recording is
best-effort after its primary write; operators must monitor the safe
`integrity.event_record_failed` signal and run the bounded case reconciler.
The reconciler currently covers durable evidence-registration events; owners
of later modality/review producer seams must add their safe source adapter.
Live Compose validation is deferred to Shreshtha's Phase 6 Part 5 gate.

- **Resolved (Phase 5A reconciliation, Shreshtha).** The Phase 5 foundation ships a typed
  `CorrelationProjectionContext` and replay worker seam; the semantic correlation Cypher handler
  (`intelligence/projection.py::make_correlation_projection_handler`) is now registered into a reachable
  process via `app/modules/graph/intelligence_worker.py --replay-once`/`--replay-loop`. Before this fix,
  the handler existed and was fully unit-tested but no code under `app/` ever composed it with
  `replay_graph_updates` outside a test -- queued events were durable and replayable, but nothing in a
  real process ever claimed and projected them. A live end-to-end test
  (`tests/integration/graph/test_intelligence_pipeline_live.py`) now proves a real submission is
  replayed and projected into a real `Correlation` node exactly once, and that replaying twice never
  duplicates it.
- No public write/review route is exposed. Existing `GRAPH_READ` protects the
  read endpoints; write/review authorization remains intentionally deferred.
- Phase 5B secures the currently exposed graph/correlation read routes. Feature
  snapshots, analytics, motifs, and direct vector retrieval intentionally have
  no public route; if exposed later, they must use the same `GRAPH_READ` seam
  and case-bound repository predicate rather than relying only on a route
  parameter.
- A graph-update event has no automatic retry ceiling for a connection outage
  by design, preserving replayability. Operational alerting/backoff scheduling
  beyond the bounded claim invocation remains future worker operations work.

## Phase 5 Shreshtha graph intelligence

The local hashed-token vector is a deterministic retrieval aid, not a semantic embedding model.
Leiden uses a fixed seed and deterministic snapshot ordering, but its quality and operational performance are unmeasured.
Rules weights are preliminary and unmeasured. Operation Nightfall truth evaluation, measured Precision@K/Recall@K/
false-link rate, P99 bridge validation, final rules-weight freeze, full real-dataset validation, and LAN end-to-end
validation will run after all Phase 5 contributors have merged their work.

**Resolved (Phase 5A reconciliation).** A prior progress-log checkbox described the "semantic typed-outbox
handler" as done; the handler's *logic* was indeed complete and tested, but it was not registered into any
reachable process -- see the corrected entry above. `retrieval.py`'s transliteration-vs-alias overlap check
also had a genuine, order-dependent bug (only `left.transliterations` was checked against `right`'s fields,
never the reverse, so the result depended on which of two randomly-generated observation IDs happened to
sort first) -- fixed to check both directions, with a permanent regression test
(`tests/unit/graph/test_intelligence_sourcing.py::test_transliteration_match_is_symmetric_regardless_of_observation_id_ordering`).

**New, deliberate Phase 5A scoping limitations** (not defects):

- `PgvectorCandidateStore` had zero test coverage before this phase; it now has both unit (fake-engine)
  and live (real pgvector) coverage, including case isolation and upsert idempotency.
- `normalise_identifier`'s phone rule strips non-digits and prepends `+` but never reconciles a country
  code -- a CDR's E.164 `+919876543210` and a bare-digit `9876543210` extracted elsewhere are correctly
  treated as distinct, not incorrectly merged. A future phase could add country-code-aware normalization
  if cross-format phone matching becomes a real need.
- **Phase 5B producer validation does not reconcile phone country codes or validate account ownership.**
  CDR/finance participant values are source-local opaque identifiers; a supplied `+91...` value and a
  bare local number are intentionally not made equivalent. No account/phone ownership, counterparty name,
  identity, relationship, guilt, candidate, or score is inferred from a two-party event.
- **Only explicitly mapped CSV/XLSX/JSON aliases are supported.** Unknown headers, free-form narration
  masquerading as a finance reference, missing core parties/time, malformed duration/time/amount/currency,
  and impossible CDR end ranges are rejected or withheld from correlation-ready attributes. A rejected row
  has safe worker/chunk diagnostics but no canonical observation; no persistent source-validation review
  model was introduced.
- **OCR provenance validation is structural, not a truth assessment.** Bounding boxes are normalized
  line-level regions, spans are source-relative, and confidence means recognition/extraction quality only.
  Existing local OCR/NER models may still miss or misread text; this phase adds no model, download, or
  full-document graph-text storage.
- **Resolved (Phase 5 final integration, Nipun)**: `sourcing.
  descriptor_from_observation`'s mapping scope is intentionally bounded to
  observation types already confirmed to carry a genuinely stable
  identifier, alias, or handle (see the module's own docstring for the
  exact list) -- unchanged. What used to be a limitation here is closed:
  a `cdr_call_record`/`financial_transaction_record`'s second party
  (callee/receiver) is now independently exact-blockable, via a new
  `descriptors_from_observation` (plural) that returns both parties as
  role-scoped descriptors sharing one origin observation, never a second
  fabricated observation or an entity. Same-event self-pairing (the two
  ends of one record) is explicitly suppressed in `retrieval.
  retrieve_candidates`, so this closure does not relax "no candidate
  merely because two participant descriptors originated from the same
  event." See `docs/architecture/phase-5-integration.md`'s "Per-party
  (two-party) descriptor extension" section.
- The temporal motif's "movement/meeting" hop only fires for a `meeting_candidate` media observation
  whose own `contributing_observation_ids` cites a call/transfer this same adapter run recognized -- a
  real, evidence-backed link, never fabricated, but real cross-modal chains need a shared identifier
  (e.g. a phone number visible in an OCR frame) the media pipeline does not yet extract.

## Phase 4 LAN worker security

No real multi-machine TLS, proxy, LAN, GPU, or dataset validation was run in
this change. TLS certificate issuance/rotation and trusted-proxy deployment are
operator responsibilities. The control-plane deadline configuration is
documented for deployment use; end-to-end dependency timeout behaviour needs
merge-wave verification.

## Phase 4 Shreshtha media graph mapping

This layer does not decode media, infer timestamps from FPS, run ASR/diarization,
identify people/vehicles/speakers/handles, correlate events, score candidates,
or declare meetings. A source-relative frame or millisecond range remains
source-relative unless an upstream canonical observation supplies an aware
absolute time. Candidate contributor references that are absent or outside the
case do not create cross-case graph links. Real GPU/audio/LAN/dataset and full
Compose acceptance validation are intentionally deferred to the Phase 4 merge
wave and post-Phase-5 validation gate.
# Phase 4 extracted-text utility limitations (Jasraj)

- The utility does not execute OCR, select ROIs, decode image/video, or test a
  real OCR engine, GPU worker, LAN deployment, or media dataset.
- Plate-like matching is structural and India-style only; it does not validate
  a registration or correct ambiguous OCR glyphs. Phone matching is Indian
  mobile format only. Account/reference extraction requires an explicit label.
- Candidate results are review-only unresolved text claims. Mapping them to a
  canonical observation is a future worker's explicit responsibility; graph
  projection, correlation, entity resolution, and identity verification remain
  out of scope.

# Phase 4 video/image worker limitations (Gaurav)

- Profile thresholds and batch limits are deterministic safeguards, not measured
  performance or accuracy claims. Real GPU, hardware decode, model assets,
  media, LAN, and dataset validation are deferred.
- Scene/motion signals are local frame-difference inputs supplied to planning;
  they do not identify people, vehicles, meetings, or relationships.
- Frame-number validation is deterministic only for a trustworthy constant-FPS
  probe. Variable-rate or unavailable mappings are retained as incomplete,
  non-correlation-ready evidence rather than guessed. No face recognition,
  biometric matching, appearance search, or vehicle/plate ownership lookup is
  implemented or claimed.
# Phase 4 audio/social worker limitations (Sarthak)

- The worker supports an offline executable/model bridge, but this repository
  ships neither executable nor model bundle. Missing configuration defers raw
  audio truthfully; real model quality, WER, diarization quality, and LAN
  validation remain merge-wave work.
- VAD thresholds/script hints are deterministic policy aids, not measured
  accuracy claims. `audioop` is used only for Python 3.12 PCM normalization;
  a supported replacement is needed before a Python 3.13 upgrade.
- **Resolved (Phase 5 final integration, Nipun)**: persisted coordinator
  manifest/chunk scope is now genuinely enforced, not just locally
  planned. `run_communication_job_with_batches` registers the manifest
  with the coordinator (`POST .../media-manifest`) before any chunk is
  published, and ASR/diarization mentions are published chunk-scoped
  (`POST .../media-chunks/publish`) instead of the previous plain,
  unscoped batch route. See `docs/architecture/phase-5-integration.md`'s
  "Persisted media-manifest/chunk enforcement" section. Unlike the visual
  side (`tests/integration/media_processing/test_media_worker_live.py`),
  no live test runs the real audio worker process end to end against
  live infra yet -- this wiring is covered by unit tests against a fake
  client only (`tests/unit/communication_processing/
  test_communication_worker_orchestration.py`); a live audio-worker
  acceptance test is deferred, not attempted.
- Platform parsing and Unicode/transliteration normalization are deterministic
  safeguards, not multilingual accuracy, account-ownership, speaker identity,
  or alias-equivalence claims. There is no face/voice biometric identification
  or external phone/handle ownership lookup.

# Phase 6 Part 1 integrity foundation (Nipun)

- **Not a blockchain claim.** A signed Merkle checkpoint proves internal
  consistency (any tamper with recorded integrity metadata is detectable
  on verification) against this one database and this one local Ed25519
  key — never independent, third-party-verifiable anchoring. See
  `docs/decisions/ADR-013-phase-6-integrity-checkpoints.md` for the full
  reasoning and the explicit list of what a stronger claim would require.
- **No database-level immutability enforcement.** `integrity_events`/
  `merkle_checkpoints`/`checkpoint_signatures` are treated as append-only
  by every application code path (no `update`/`delete` method exists), but
  nothing at the database level currently prevents a direct `UPDATE`/
  `DELETE` by a role with table access — a real deployment should restrict
  write grants on these three tables to the migration role only. Documented
  as a deliberate operational trade-off in `docs/architecture/
  phase-6-integrity.md`'s "Deliberate storage trade-off" section, not a
  silently accepted gap.
- **Single local signing key, no rotation workflow.** `Settings
  .integrity_signing_key` is one key from environment/config; there is no
  automatic rotation, no multi-key/threshold signing, and no external
  KMS/HSM. A compromised key can sign an arbitrary (internally-consistent)
  history — see the ADR's threat-model discussion.
- **HTTP exposure is intentionally narrow.** Only protected, case-scoped
  checkpoint metadata, verification, and safe bundle export are available;
  there is no raw integrity-event feed or global integrity-admin bypass.
- **Append-only triggers are not superuser-proof.** A fully privileged
  PostgreSQL operator can disable triggers or alter rows; the protection is
  strong for ordinary application/database roles, not an absolute claim.
- **Reconciliation is durable-source scoped.** It repairs evidence,
  correlation, and Part 3 structured-provenance leaves from durable metadata
  only. Observation-batch replay awaits a durable original ordering field;
  later modality/review producer owners must add safe source adapters.
- **Structured-provenance legacy boundary.** The Phase 6 Part 3 reconciler
  replays only its durable safe projection records. It does not synthesize a
  projection for observations accepted before this migration, nor does it
  derive one from raw observation payloads. A fully privileged PostgreSQL
  superuser can still bypass append-only triggers.
- **Visual/communication provenance legacy boundary.** Part 4 replays only
  exact append-only safe projections. It does not synthesize leaves for
  pre-migration observations or raw observation payloads. Direct PostgreSQL
  trigger execution and real-worker
  infrastructure validation are deferred to Shreshtha's Phase 6 Part 5 gate.
- **Checkpoint building is an explicit action, not automatic.** There is
  no scheduler, background job, or per-event auto-checkpoint in this
  phase — an operator (or later automation, not built here) must invoke
  `build-checkpoint` explicitly for a named, contiguous range.
- **`review_decision`/`hypothesis_action` are enum values only.**
  **Resolved (Phase 6 Part 5, Shreshtha).** A real review-decision and
  hypothesis workflow now emits both through the exact same,
  unmodified `record_integrity_event` facade — see
  `docs/architecture/phase-6-review-and-hypothesis.md`. No new integrity
  schema was needed, exactly as this document anticipated.
- **Live-infrastructure verification for this module was not run in this
  environment.** **Resolved (Phase 6 Part 5, Shreshtha).** The full Phase 6
  migration chain was applied to a live PostgreSQL, and
  `tests/integration/integrity/test_repository_live.py` (append-only
  update/delete rejection, checkpoint idempotency/overlap, full build-
  verify-export round trip) was run live — see `docs/qa/test-results.md`'s
  dated Phase 6 Part 5 entry for exact commands and results.

# Phase 6 Part 5 — review and hypothesis workflow, final release gate (Shreshtha)

- **Simplified two-state hypothesis lifecycle.** The task's *suggested*
  lifecycle was `draft -> proposed -> needs_review ->
  accepted_by_reviewer/rejected_by_reviewer`; this implementation only
  ever creates a hypothesis directly at `needs_review` (no `draft`/
  `proposed` state or transition route exists), because the required
  minimal write-route surface has no dedicated endpoint for a draft-to-
  proposed transition. See
  `docs/decisions/ADR-015-phase-6-review-and-hypothesis.md` decision 3. A
  team that wants a private drafting flow should hold the statement
  client-side until ready to submit.
- **Neo4j projection for review decisions and hypotheses is best-effort,
  not outbox-backed.** Unlike Nipun's `graph_update_events` durable outbox
  for correlations, a review/hypothesis Neo4j write runs synchronously
  after the durable PostgreSQL write and is logged (never re-raised) on
  failure — there is currently no automatic retry queue if Neo4j happens
  to be down at that exact moment. The durable decision/hypothesis itself
  is never lost or rolled back; only its graph projection may need a
  manual re-run (the underlying write is idempotent, so a re-run is safe)
  once Neo4j recovers. Building a second outbox exclusively for this was
  judged out of proportion for this task (see ADR-015 decision 6); a
  future phase could extend `graph_update_events`-style replay to cover
  it instead.
- **`candidate_review_decisions` and `hypothesis_actions` are genuinely
  append-only — including for test/operator cleanup.** Both tables carry
  the same PostgreSQL trigger `integrity_events` uses; confirmed directly
  during this task when the live test's own cleanup code attempted a
  `DELETE` and was correctly rejected. Integration tests therefore never
  delete these rows, relying instead on an unguessable, never-reused
  `case_id` per test run — the same precedent
  `tests/integration/integrity/conftest.py` already established for
  `integrity_events`. A production operator who needs to physically purge
  old rows (e.g. for data-retention policy, not tamper repair) would need
  a deliberate, audited, superuser-level operation — never an ordinary
  application code path.
- **Reviewing a candidate/hypothesis is exactly one accept/reject
  judgement, never a re-score.** This task does not let a reviewer adjust
  Nipun's Phase 5 rule weights, feature snapshot, or contradiction
  reasons, and does not let a hypothesis review re-open or amend the
  hypothesis's own statement/rationale/references — a changed mind
  requires proposing a new hypothesis or awaiting a later-phase amendment
  workflow (not built here).
- **`HYPOTHESIS_PROPOSE` role grants mirror `EVIDENCE_WRITE`'s roles by
  design choice, not by a requirement in the access-control matrix
  itself.** `CASE_OWNER`/`CASE_MANAGER`/`INVESTIGATOR` may propose a
  hypothesis; `ANALYST`/`REVIEWER`/`VIEWER` may not. A team that wants
  analysts to propose hypotheses (but still not write raw evidence) would
  need a genuinely new, more granular action — reusing `EVIDENCE_WRITE`'s
  role set was a proportionate default, not an inherent constraint.
- **No real Docker/PostgreSQL/Neo4j gap remains for Phase 6 as a whole**
  after this task — see `docs/qa/test-results.md`'s dated Phase 6 Part 5
  entry for the exact commands, migration application, and live test
  results that close out every `deferred to Shreshtha's Phase 6 Part 5
  gate` note left by Parts 1-4.

# Phase 7 Part 1 evaluation foundation and local-model governance (Nipun)

All of the following are deliberate Part 1 scope boundaries, not
oversights — see `docs/architecture/phase-7-evaluation-and-model-governance.md`'s
"What Part 1 explicitly does not do" section:

- **No dataset was downloaded.** Every entry in `dataset-manifest.v1.json`
  is a typed, safe manifest record only; `local_path_placeholder` names
  where a future download would go, not a path that currently contains
  anything.
- **No model weight was downloaded, and no model was run.** Every entry in
  `model-candidates.v1.json` is a catalogued candidate only.
- **No benchmark was executed.** `benchmark-metrics.v1.json` defines
  required metric *keys* per task; no `BenchmarkRunV1` with real measured
  values exists anywhere in this phase's committed artifacts.
- **No final modality model was selected.** Every candidate's
  `selection_status` is `candidate` or `conditional`; `selected` is
  structurally rejected by `ModelCandidateV1`'s own validator for any
  Part 1 record.
- **No correlation algorithm was chosen.** The Logistic
  Regression/XGBoost/LightGBM candidates are catalogued and their
  selection *policy* is frozen (ADR-016); none was installed, run, or
  compared against the frozen Phase 5 rules baseline.
- **No LAN or multi-laptop validation was attempted** — that is Phase 8's
  scope entirely.
- **Several licence-status fields are `pending_verification`, by design.**
  Ten of the fourteen dataset-manifest entries carry
  `license_status: "pending_verification"` because this session could not
  independently re-verify each source's current, exact redistribution/
  derivative-use terms — see each entry's own `license_notes`. Parts 2-4
  must resolve this before downloading the corresponding dataset; the
  manifest entry's mere existence is not pre-approval to download.
- **Two dataset owners were assigned by domain fit, not by an explicit
  task-spec table column.** The task's own "conditional or support-only"
  dataset table names no owner; `docs/architecture/
  phase-7-evaluation-and-model-governance.md`'s "Where a fact was not
  invented" section documents the reasoning, including the one genuinely
  ambiguous case (`vast_2014_mixed_records`, assigned to Sarthak per the
  VAST family grouping, spanning into Jasraj's structured-data scope too).
- **`vad`/`language_identification` metric groups extend beyond the task
  spec's own metrics table.** Neither task had an explicit metrics row in
  the original spec; Nipun added a minimal, reasonable group for each
  (documented in each group's own `description` field in
  `benchmark-metrics.v1.json`), not left uncovered.
- **`social_text_extraction` is a forward-compatible-only task, mirroring
  Phase 6's `review_decision`/`hypothesis_action` precedent.** No Part 1
  candidate exists for it; the existing deterministic social/chat parsers
  remain today's baseline. A future part may catalogue a real candidate
  against this task without a schema change.
- **The synthetic case plan names placeholder case IDs only
  (`synth-case-dev-01`, etc.).** No actual synthetic case content --
  evidence, observations, or fixtures -- exists behind any of them yet.
  Later owners generate approved modality fixtures against this plan.

# Phase 7 Part 2 evaluation: structured-data and local OCR benchmarking (Jasraj)

Gate B resolved the AMLSim and PaddleOCR execution unknowns. These limits
remain:

- **GoMask Voice CDR is blocked.** The verified official marketplace entry
  requires an account and credits to download its advertised 501 rows. The
  applicable rights also depend on the account plan/EULA. No official input
  file or SHA-256 exists locally, no substitute was used, and the CLI result
  is truthfully `unavailable`. Gate B cannot be marked fully complete without
  team direction and legitimate access.
- **The FIR OCR metrics cover annotation-region crops, not full pages.** The
  2,447 local samples are real crops derived from the official annotation
  boxes across 544 referenced images. CER/WER measure only transcription
  distance on those regions. Full-page layout recovery, real police evidence,
  and generalization to other document sets are unmeasured.
- **FIR field-extraction precision/recall/F1 are null.** The official source
  provides value text and category/bounding-box metadata, not a defensible map
  to this project's label-bearing `expected_fields`. Adding field labels would
  have fabricated ground truth.
- **The measured OCR runtime is host-specific.** Both candidates ran through
  PaddleOCR 3.7.0/PaddlePaddle 3.3.1 on one Linux x86_64 Intel CPU with
  oneDNN and optional orientation/unwarping stages disabled. VRAM is null.
  These latency and RAM values are not Apple-Silicon, GPU, Docker, or LAN
  measurements.
- **The official AMLSim sample lacks currency and calendar timestamps.** The
  dataset-specific benchmark path validates its node IDs, value, and integer
  simulation step without inventing missing semantics. Its 1.0 normalization
  accuracy means all 118,250 structurally valid simulator rows were accepted;
  it is not a fraud-detection or independently labelled semantic-accuracy
  measurement.
- **`artifact_sha256` for the CDR/finance deterministic baseline is the
  local input file's own SHA-256, not a model-weight hash.** There is no
  model weight for `existing-deterministic-parsers` (a deterministic-rules
  baseline, not an ML model) -- the input file's hash stands in as "the
  artifact this run measured," satisfying `BenchmarkRunV1`'s own validator
  requiring a non-null `artifact_sha256` for a `SUCCEEDED` status.
- **No Part 2 candidate is selected.** Every dataset/candidate pair this
  phase benchmarks keeps its Part 1 `selection_status` of `candidate`/
  `conditional` -- Gate C selects a winning OCR candidate only after the
  remaining blockers and cross-modality evidence are resolved.
- **`benchmark.py`'s OCR dispatch assumes exactly two PaddleOCR variants,
  distinguished by a `candidate_id` suffix (`"mobile"`/`"server"`).** This
  mirrors Part 1's frozen two-candidate catalogue exactly; it would need a
  small, explicit update if a future part ever adds a third OCR candidate.
  ### Gate B CDR benchmark — GoMask deferred
  The GoMask Voice CDR benchmark is deferred because the official artifact requires
  legitimate account/credit access and review of the applicable EULA. No unofficial
  substitute dataset or fabricated metric was used. The benchmark CLI records this
  state as `unavailable`.
- **No GPU, Docker, or LAN benchmark was run.** The real Gate B model runs
  were intentionally local CPU runs. Deployment validation remains outside
  Gate B.
- **Resolved (Gate B, Aditya's MacBook, real measurement): the document
  OCR fallback's default configuration is now `page_segmentation_mode=6`,
  `binarize=False`, and `fir_report.py`'s FIR-reference matcher tolerates a
  short OCR-garbled label.** Two real macOS Tesseract builds (5.5.0,
  5.5.3) both showed `binarize=True` actively harms recognition -- reverted
  to off by default. A full PSM x binarize matrix on 5.5.3 found PSM 6 the
  best-performing mode and every `binarize=True` result worse than its
  `binarize=False` counterpart; no OS-specific branch or hard-coded fixture
  value was introduced -- one deterministic default plus one narrowly
  extended, still-evidence-bound regex. See `docs/architecture/
  document-structured-processing.md`'s "Gate B macOS OCR configuration"
  section for the full measured matrix and the exact regex reasoning.
  **Confirmed** directly on the real Gate B MacBook (macOS, Python 3.12.7,
  Tesseract 5.5.3, pytesseract 0.3.13, pypdfium2 5.13.0): the focused
  structured-processing OCR tests and then the complete test suite
  (`uv run pytest -q` -> 2046 passed, 96 skipped, 1 warning unrelated to
  this change, 0 failed) both pass with this configuration in place -- see
  `docs/qa/test-results.md`'s matching dated entry for the exact command
  and counts. This confirms the three fixtures measured (`91/2026`,
  `20/2026`, `30/2026`) recover correctly on that real machine; it is not
  a claim that every future document type, layout, or real police evidence
  will OCR at this accuracy. The later FIR ICDAR 2023 crop benchmark adds
  transcription metrics, while field-quality measurement remains unavailable
  for the ground-truth reason stated above.

# Phase 7 Part 3 evaluation: visual benchmark foundation and local-model governance (Gaurav)

The following record Part 3's state after **Gate B (2026-09-20, on
Shreshtha's laptop)** actually downloaded real data, installed real
dependencies, and ran real benchmarks for `virat_ground` -- see
`docs/qa/test-data.md`/`docs/qa/test-results.md`'s dated Gate B sections
for exact sources, hashes, commands, and measured values:

- **`virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` are real, genuinely
  benchmarked, and licence-cleared.** Gate B read the actual VIRAT Video
  Dataset Usage Agreement and Ultralytics' actual AGPL-3.0 licence
  directly, downloaded one small real VIRAT clip (with its official
  annotations) and two real YOLO11 weight files, and ran the real
  `visual_benchmark_cli` against them -- producing genuine `SUCCEEDED`
  detection (`yolo11n`/`yolo11s`) and tracking (`bytetrack`) results with
  real precision/recall/mAP/IDF1/MOTA values, not synthetic fixtures.
  `license_status` moved from `pending_verification` to
  `verified_restricted_noncommercial` in both
  `configs/benchmarks/dataset-manifest.v1.json` and
  `model-candidates.v1.json` for exactly these four entries.
- **`safe_unsafe_behaviour` and `ufpr_alpr`/`paddleocr-lightweight-
  visual-text` remain `pending_verification`, legitimately deferred, not
  silently skipped.** `safe_unsafe_behaviour`'s frozen manifest entry has
  no pinned `source_reference` at all (`"pending_verification"`) to
  benchmark against; `ufpr_alpr` requires a formal academic access-request
  process this task did not attempt to bypass. `require_license_cleared_
  for_real_execution` still blocks a `SUCCEEDED` result for both, and a
  dedicated test documents exactly this split
  (`test_every_real_gaurav_dataset_and_candidate_licence_status_is_
  documented`).
- **Ultralytics (with PaddleOCR/PaddlePaddle) was genuinely installed and
  exercised for the first time during Gate B**, via `uv sync --extra
  video-benchmark`. This surfaced and fixed real API-drift bugs no mock
  could have caught: `ultralytics.utils.yaml_load` no longer exists in
  `ultralytics==8.4.156` (replaced by `ultralytics.utils.YAML.load`),
  `BYTETracker.__init__` no longer accepts a `frame_rate` argument at
  all, `BYTETracker.update()` requires a real, numpy-indexable
  `ultralytics.engine.results.Boxes` instance rather than a duck-typed
  stand-in, and the `lap` package (needed by `BYTETracker` internally)
  was missing from the `video-benchmark` extra and has been added. See
  `docs/qa/test-results.md`'s dated Gate B entry for the full list,
  including a `follow_imports = "skip"` mypy override this installation
  also required (`ultralytics` ships a `py.typed` marker, so mypy's
  behaviour otherwise differed between installed/uninstalled states) and
  a repository-wide `tests/__init__.py` fix (`ultralytics` ships its own
  colliding top-level `tests` package that shadowed this project's own
  once installed).
- **`_build_real_tracker_engine` wires Ultralytics' own bundled
  `BYTETracker`** (`ultralytics.trackers.byte_tracker.BYTETracker`, the
  identical tracker `model.track(..., tracker='bytetrack.yaml')` uses
  internally) against externally-supplied per-timestamp detections,
  lazily imported inside this one function. No separate ByteTrack package
  or model weight is needed -- `bytetrack.yaml` ships bundled inside the
  `ultralytics` package itself, since ByteTrack's association step is a
  Kalman-filter/Hungarian-matching algorithm, not a learned model. Its
  actual licence exposure is therefore `ultralytics`'s own AGPL-3.0, not
  the separately-licensed upstream ifzhang/ByteTrack MIT repository. The
  real-engine construction glue degrades to a safe
  `BenchmarkArtifactUnavailableError` -- never a crash -- whenever
  `ultralytics` is absent.
- **HOTA is always `None`.** A correct HOTA requires a geometric-mean
  detection/association-accuracy sweep across multiple IoU/alpha
  thresholds; this harness deliberately reports `None` rather than ship
  a simplified approximation under the real metric's name.
- **IDF1 uses a simplified, majority-vote per-ground-truth-track identity
  assignment**, not the optimal global bipartite assignment a full
  implementation (e.g. `py-motmetrics`) solves. Documented directly in
  `visual_benchmark_metrics.idf1`'s own docstring.
- **mAP is single-threshold (IoU >= 0.5), VOC-style 11-point interpolated
  Average Precision** -- not COCO's mAP@[.5:.95] sweep across ten
  thresholds.
- **The local benchmark manifest formats
  (`benchmark_manifest.jsonl`/`tracking_manifest.jsonl`/
  `visual_text_manifest.jsonl`) remain this task's own invented shapes,
  not a real dataset's native annotation format.** Gate B did write and
  exercise a real, working converter from VIRAT's actual
  `objects.txt` annotation format (`object_id object_duration
  currentframe bbox_left bbox_top bbox_width bbox_height object_type`,
  confirmed directly from a real downloaded file) into these JSON-Lines
  shapes for `virat_ground` specifically -- see
  `docs/qa/test-data.md`'s dated Gate B section. No such converter exists
  yet for UFPR-ALPR or Safe/Unsafe Behaviour's own real annotation
  formats, since neither dataset was downloaded (both remain
  legitimately deferred).
- **`safe_unsafe_behaviour` has no behaviour-classification candidate.**
  The manifest's own `allowed_tasks` for it is "additional visual
  detection stress-testing" only; this harness benchmarks it with the
  same YOLO11 detection candidates as `virat_ground`, never a
  purpose-built behaviour classifier -- inventing one would be an
  unapproved new task/candidate outside Part 1's frozen catalogue.
- **No cross-camera identity capability exists, structurally, not only by
  convention.** A `TrackSegment.local_track_id` becomes only a
  same-observation-ID discriminator when passed through
  `build_observation_draft_for_track_segment`/`observation_for_draft` --
  never an `ExtractedEntityMention` or any other identity-shaped field --
  proven by a dedicated test.
- **The pre-existing whole-module "no infra/ML library" static safety
  test (`test_media_safety.py::test_no_infrastructure_or_ml_library_is_
  imported`) required a narrow, explicit carve-out for
  `visual_benchmark*.py` files specifically**, since two of this task's
  four approved candidates *are* the exact libraries (`ultralytics`,
  `paddleocr`) that check was written to forbid in the *production*
  detection/OCR pipeline. A new, dedicated test
  (`test_benchmark_harness_still_forbids_every_non_candidate_infra_or_ml_
  library`) proves the carve-out is narrow -- every other forbidden
  library (databases, object storage, queues, `torch`/`tensorflow`/
  `sklearn`) remains forbidden in the benchmark harness too, and no
  production file's exemption changed at all.
- **Ultralytics was validated live during Gate B; PaddleOCR/GPU/Docker
  were not.** Gate B genuinely installed and exercised `ultralytics`
  against real YOLO11 weights and a real VIRAT clip (see above). No
  PaddleOCR installation was exercised against any real image (`ufpr_alpr`
  remains deferred), no GPU was available on the Gate B host (confirmed
  via `nvidia-smi` reporting no driver, not assumed), and this task never
  touched Docker. The committed unit test suite in `tests/unit/
  media_processing/test_visual_benchmark_*.py` still runs entirely
  against synthetic fixtures and fake engines, independent of what is or
  isn't installed on any given machine.

# Phase 7 Part 4 evaluation: audio and social/chat benchmark foundation and local-model governance (Sarthak)

All of the following are deliberate Part 4 scope boundaries, verified on
Shreshtha's development laptop only -- real dataset/model validation is
Aditya's MacBook Gate B pre-flight, not yet performed:

- **No real Common Voice Indic, AMI Meeting Corpus, or VAST dataset was
  downloaded, inspected, or benchmarked in this environment.** Every
  ASR/VAD/diarization/language-ID/social-extraction benchmark test in
  this phase runs against small, invented, synthetic fixtures and fake
  engines (see `docs/qa/test-data.md`'s Phase 7 Part 4 section); no real
  benchmark result -- a real WER, a real DER, a real extraction F1 --
  exists anywhere in this phase's committed artifacts, except for one
  genuine local run against a synthetic (not real) local directory during
  this task's own verification (see below).
- **`configs/benchmarks/model-candidates.v1.json` gained one new,
  additive entry: `existing-deterministic-social-parsers`.** Part 1's own
  catalogue deliberately left `social_text_extraction` with no candidate
  at all, and its own documentation explicitly anticipated a future part
  adding exactly this. Nothing existing in the catalogue was modified;
  the full Part 1 evaluation test suite (42 tests) was re-run after this
  addition and still passes unchanged.
- **Every real ASR/VAD/diarization candidate is currently blocked** by
  `license_status: pending_verification` (`faster-whisper-small/medium`,
  `silero-vad-v6`) or by `selection_status: conditional`
  (`pyannote-community-local`, `fasttext-lid176`) or both. `require_
  cleared_for_real_execution` blocks a `SUCCEEDED` result for all of them
  today -- confirmed by a dedicated test. No Part 4 ASR/VAD/diarization
  combination can produce a real completed benchmark until Aditya's
  MacBook pre-flight resolves the relevant licence/adoption decision.
- **`vast_social_text`/`vast_2014_mixed_records` +
  `existing-deterministic-social-parsers` is already licence-cleared
  today** -- a genuinely discovered asymmetry with Phase 7 Parts 2/3, not
  invented (the VAST Challenge organizers' permissive terms were already
  verified in Part 1, and the new deterministic-parsers candidate is
  `internal_only`, not `pending_verification`). This pair needs no
  optional dependency, no model cache root, and no gated-model token --
  confirmed by a real local CLI run against a synthetic dataset directory
  during this task's own verification, producing a genuine `succeeded`
  result. This is Part 4's most immediately actionable path for Aditya's
  pre-flight, once real local VAST data is available.
- **No faster-whisper, pyannote.audio, fastText, or torch installation
  was attempted or verified.** `faster-whisper`/`pyannote-audio`/
  `fasttext`/`torch` are declared in `pyproject.toml`'s
  `[project.optional-dependencies] audio-social-benchmark` group and
  resolved into `uv.lock`, but never installed on this machine and never
  pulled in by `uv sync --all-groups` -- `torch` remains scoped to this
  one optional extra and is never a production/base dependency.
  `audio_social_benchmark._build_real_asr_engine`/
  `_build_real_vad_engine`/`_build_real_diarization_engine`/
  `_build_real_language_id_engine`'s real wiring is a best-effort attempt
  written from each library's documented public API shape, not verified
  against an actually-installed package -- it may need a small adjustment
  once Aditya's MacBook pre-flight installs the pinned versions and
  exercises the real API for the first time.
- **`_build_real_vad_engine` never calls `torch.hub.load` against GitHub
  or any other network source.** It requires a Torch Hub *source
  snapshot* of `snakers4/silero-vad` staged manually -- never downloaded
  by this code -- under `<model_cache_root>/silero-vad/` (a plain local
  checkout containing `hubconf.py` at its root), and loads it with
  `torch.hub.load(..., source="local")`, which only ever reads local
  files, lazily importing `torch` only inside this one function. This was
  a deliberate, explicit team decision (not this task's unilateral call)
  to add `torch` to the existing `audio-social-benchmark` optional extra
  specifically for this candidate, mirroring how Phase 7 Part 3 resolved
  an analogous "finish the already-selected route" decision for its
  Ultralytics ByteTrack tracker; a follow-up instruction then closed the
  remaining network dependency in the initial wiring. `model_sha256` is
  verified directly against the snapshot's own pinned weight file
  (`files/silero_vad.jit`) before anything is loaded, and every resolved
  path is checked to remain inside the configured model cache root. The
  VAD metric/aggregation logic (`run_vad_benchmark`) was already fully
  implemented and tested against `FakeVadEngine`; the real-engine
  construction glue degrades to a safe `BenchmarkArtifactUnavailableError`
  -- never a crash or a silent download -- whenever `torch` is absent, the
  local snapshot is missing or escapes the model cache root, the weight
  file's hash doesn't match, or `torch.hub.load` itself fails.
- **`fasttext-lid176` is wired as transcript-language identification
  chained after ASR, not as a direct audio-native language-ID model, and
  now records *two* required model dependencies, not just its own.**
  fastText's `lid.176` model classifies language from text, not audio, so
  `_build_real_language_id_engine` constructs its own internal
  `faster-whisper` transcription stage (already declared in this same
  optional extra) purely as plumbing to produce text for fastText to
  classify -- only the language *classification* is attributed to the
  `fasttext-lid176` candidate's own name/version/hash; the transcription
  stage is never itself benchmarked here (Part 4's ASR candidates cover
  that separately, under their own candidate names). This was chosen over
  reusing an ASR engine's own built-in language-detection output, which
  would have misattributed that result to the wrong model under the
  `fasttext-lid176` candidate label. A follow-up instruction required
  this dependency to be safely recorded, not only used internally:
  `_build_real_language_id_engine`/`_run_language_id` now require a
  second, independent `VerifiedModelArtifact` for the transcription stage
  in addition to the primary fastText artifact -- a completed result is
  rejected if either is absent. `model_cache_root` must contain both
  `lid.176.ftz` and a `lid-transcription-stage/model.bin` faster-whisper
  weight file; each is hash-verified against its own caller-supplied
  SHA-256 before it is loaded -- see
  `docs/runbooks/local-development.md`. Since the frozen `BenchmarkRunV1`
  contract has only one `artifact_sha256` field, the transcription
  stage's own name/version/SHA-256 are folded into
  `inference_config_hash`'s input instead, making a different
  transcription-stage dependency a detectably different configuration.
  The transcript text produced internally is never returned, logged, or
  serialized anywhere -- proven by a dedicated test using a distinctive
  marker transcript.
- **DER is frame-discretized and uses a majority-vote speaker-label
  mapping** (the same methodology Phase 7 Part 3 uses for its own
  simplified IDF1), **not the full DER definition** -- no collar exclusion
  around reference boundaries, no explicit overlapped-speech accounting.
- **"Speaker turn quality" is this harness's own defined boundary-timing
  measure**, not a standardized external metric name.
- **JER is always `None`.** A correct Jaccard Error Rate needs the same
  optimal bipartite speaker assignment a full diarization-metrics toolkit
  solves; this harness's own majority-vote mapping is not that
  assignment, so this harness reports `None` rather than a value under
  JER's real name that does not mean what JER means.
- **VAD is frame-discretized at a fixed 30ms resolution**, a documented,
  fixed choice, not tuned against any real dataset.
- **The pre-existing whole-module "no infra/ML library" static safety
  test (`test_module_safety.py::test_no_forbidden_infra_or_ml_import`)
  required a narrow, explicit carve-out for `audio_social_benchmark*.py`
  files specifically** (`faster_whisper`/`pyannote`/`fasttext`/`torch`,
  plus a separate `os`-import exemption for local-root resolution),
  mirroring Phase 7 Part 3's identical, already-established pattern. A
  new, dedicated test proves the carve-out is narrow -- every other
  forbidden library (databases, object storage, queues, `subprocess`,
  `transformers`/`whisper`/`speechbrain`/`librosa`/`sklearn`/`numpy`)
  remains forbidden in the benchmark harness too, and no production
  file's exemption changed at all.
- **No live faster-whisper/pyannote.audio/fastText/torch/GPU/MacBook/Docker
  validation was run in this environment.** Every test in
  `tests/unit/communication_processing/test_audio_social_benchmark_*.py`
  runs with no live infra, no GPU, and no downloaded dataset or model --
  exactly as required by this task's own rules. See
  `docs/runbooks/local-development.md`'s "MacBook Gate B pre-flight"
  section for what Aditya's pre-flight must still verify before this
  phase's benchmark capability can produce a real, trustworthy ASR/VAD/
  diarization/language-ID result.
- **An unrelated, pre-existing flaky test was encountered during this
  task's full-suite verification, confirmed unrelated to Phase 7 Part
  4.** `tests/unit/access_control/test_api.py::
  test_refresh_rate_limit_returns_429` failed twice when run as part of
  the full suite but passed both in complete isolation and when run
  within just `tests/unit/access_control/`'s own suite in a separate
  invocation -- consistent with real-wall-clock fixed-window
  rate-limiting timing sensitivity (`app/modules/access_control/
  rate_limit.py`'s `InMemoryRateLimiter` keys its window off `self._clock()
  // RATE_LIMIT_WINDOW_SECONDS`), not this task's `communication_
  processing`/`configs/benchmarks/model-candidates.v1.json` changes. This
  task made zero changes to `access_control` or any shared test fixture;
  not fixed here as it is outside this task's scope (another owner's
  module) -- flagged for team awareness.

## Phase 7 Part 4 Gate B (2026-09-20, Sarthak) -- real benchmark execution status

Gate B is **complete for the one dataset with genuinely available,
licence-cleared, real access -- AMI Meeting Corpus -- and remains
deferred for Common Voice and VAST, for the concrete reasons below.**
This is not a universally complete Gate B; the still-blocked candidates
are exactly as documented, awaiting a real accessible dataset, not a
code change.

- **`ami_meeting_corpus` licence status updated from `pending_verification`
  to `verified_permissive`.** Independently re-verified directly from the
  official host institution (University of Edinburgh CSTR): the corpus's
  own download page states CC BY 4.0, and its Hugging Face mirror
  (`edinburghcstr/ami`, the same institution) confirms `license:
  cc-by-4.0` in its dataset card. `silero-vad-v6` updated from
  `pending_verification` to `verified_permissive` (MIT, confirmed via a
  direct GitHub LICENSE fetch). See `docs/qa/test-data.md`'s Gate B
  section for exact hashes/commits.
- **A real defect found and fixed while wiring Silero VAD: the pinned
  weight sub-path assumed an outdated repository layout.**
  `_SILERO_VAD_WEIGHT_RELATIVE_PATH` was `"files/silero_vad.jit"` (a
  best-effort guess written before this session, when torch could not be
  installed/exercised at all -- see the line above from before this Gate
  B pass). A real clone of `snakers4/silero-vad` (commit `60b7ffa2`)
  shows `hubconf.py`'s own `silero_vad()` function loads
  `src/silero_vad/data/silero_vad.jit` instead. Fixed in
  `audio_social_benchmark.py`; the corresponding test fixture in
  `test_audio_social_benchmark_safety.py::_stage_silero_snapshot` was
  updated to match, and the real CLI (`silero-vad-v6` ×
  `ami_meeting_corpus`) now completes end to end with genuine `torch.hub.
  load(..., source="local")` inference -- see `docs/qa/test-results.md`.
- **A second real defect found and fixed: `deterministic-diarization-
  fallback` was silently routed through the pyannote engine.**
  `_run_diarization` never branched on `candidate.candidate_id` -- every
  diarization request, regardless of which candidate was named, called
  `_build_real_diarization_engine` (pyannote-only wiring). Selecting
  `deterministic-diarization-fallback` therefore silently ran pyannote's
  own local-use/model-loading checks and reported pyannote's failure
  message under the wrong candidate's name. Fixed with a new, dedicated
  `_build_deterministic_diarization_fallback_engine` (always reports a
  safe, correctly-attributed `BenchmarkArtifactUnavailableError`
  explaining that this candidate has no local raw-audio speaker-
  segmentation model in this phase -- it only imports externally-supplied
  turns via the existing `UnavailableDiarizationAdapter`/diarization-
  import path) plus a real dispatch branch in `_run_diarization` and a
  regression test
  (`test_deterministic_diarization_fallback_never_dispatches_to_
  pyannote`). This means `deterministic-diarization-fallback` is now
  *correctly* reported `unavailable` -- by design, not by accident -- for
  any raw-audio diarization benchmark; it was never meant to produce
  learned speaker segmentation.
- **Three pre-existing unit tests assumed `torch`/`fasttext`/
  `faster_whisper` would never actually be installed in this
  environment; that assumption became false the moment Gate B installed
  the real `audio-social-benchmark` extra.**
  `test_real_vad_engine_reports_unavailable_and_never_crashes`,
  `test_real_language_id_engine_is_unavailable_without_fasttext_
  installed`, and `test_real_language_id_engine_is_unavailable_without_
  its_transcription_stage` all failed once torch/fasttext/faster_whisper
  were genuinely importable, since their `ImportError` branches became
  unreachable. Fixed by forcing each module's absence via
  `monkeypatch.setitem(sys.modules, name, None)` (which raises
  `ImportError` on `import <name>` regardless of whether the package is
  actually installed) -- this keeps the tests real regression coverage on
  every machine, with or without the optional extra installed, rather
  than skipping them.
- **A real, measured VAD result exists, with an honest caveat about its
  ground truth's coarseness.** `silero-vad-v6` scored recall
  0.224/precision 1.0/F1 0.366 against `ami_meeting_corpus`'s real 20-clip
  subset. This benchmark's own ground truth marks each clip's *entire*
  duration as speech (since every clip is itself one AMI-annotated
  utterance), but AMI's segment boundaries include natural padding
  silence at the edges of many clips -- Silero correctly detects only the
  genuinely-voiced portion, so recall here measures "did Silero find
  speech somewhere inside each labelled segment" against a deliberately
  coarse, clip-level ground truth, not tight boundary agreement against a
  frame-accurate reference. Precision 1.0 (every Silero-flagged frame was
  real speech) is the more meaningful number from this particular
  ground-truth construction. See `docs/qa/test-results.md` for the full
  result JSON and exact reproduction command.
- **`pyannote-community-local` remains genuinely blocked, exactly as
  designed, and was not resolved by this task.** Its `license_status`
  stays `pending_verification` and its `selection_status` stays
  `conditional` -- this task never attempted to access, download, or
  accept pyannote's own local-use terms (an adoption decision reserved
  for an explicit team/Gate C decision, not this task's to make). Running
  it against `ami_meeting_corpus` produces a real, honest `unavailable`
  result citing the licence gate.
- **`common_voice_indic` (and therefore `faster-whisper-small/medium`,
  `fasttext-lid176`) remains genuinely deferred -- not a licence gate,
  a platform migration.** Mozilla relocated Common Voice off Hugging
  Face to a separate "Mozilla Data Collective" account system (effective
  October 2025); this is a materially different access gate than a
  Hugging Face login, confirmed via the authenticated `HfApi()` client
  (repos return empty file listings or genuine 404s, not 401s). No real
  Common Voice data exists anywhere in this environment. See
  `docs/qa/test-data.md`.
- **`vast_social_text`/`vast_2014_mixed_records` remain genuinely
  deferred -- a real connectivity/trust failure, not a licence gate.**
  Their `license_status` is already `verified_permissive` (set before
  this task), but the official host `visualdata.wustl.edu` presents a
  broken TLS certificate chain (confirmed via both `WebFetch` and direct
  `curl -v`: `OpenSSL verify result: unable to get local issuer
  certificate`). No insecure download was attempted, and unverified
  third-party GitHub mirrors of VAST 2014 solution repositories were
  deliberately not used as a data source. No real VAST data exists
  anywhere in this environment.
- **The 20-utterance/one-session AMI subset is small and fixed by
  design, not a statistically representative sample.** It is drawn from
  one real meeting (`EN2002c`) and three real speakers only; it is
  sufficient to prove the real VAD/diarization-dispatch code paths
  execute correctly end to end on real data, not to draw a general
  accuracy conclusion about Silero VAD or this project's diarization
  approach. No winning candidate was selected -- that remains Gate C's
  decision.
