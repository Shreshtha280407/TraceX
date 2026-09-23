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
- **CDR/financial timestamp parsing accepts only a fixed, documented format list.** A CDR/financial export using a timestamp format outside `cdr._TIMESTAMP_FORMATS` fails the whole record as `required_field_missing` (CDR) or `invalid_source_signal` (financial) rather than being guessed at — this is the intended "never silently coerce an ambiguous format" behavior, but it does mean some real-world exports will need a documented-format update before they process cleanly. **Partially resolved (Gap-Closure follow-up)**: trailing-`Z` ISO 8601 (`2032-01-01T00:10:00Z`) and explicit fixed-offset ISO 8601 (`2032-01-01T00:10:00+05:30`) are now accepted, self-describing UTC/offset winning outright over `source_timezone`/the configured default — found via live testing against real synthetic CDR data using this exact format, which none of the six pre-existing formats matched. Still a fixed, documented list, just a longer one — a timestamp using a format outside all seven still fails the same way as before, nothing about the "never guess" behavior changed.
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
- **Resolved (Gap-Closure follow-up): one credential per worker role, never one shared `WORKER_TOKEN` across simultaneous worker containers.** `Settings.worker_token` is one field, read identically by every worker module — correct for one worker process on the host at a time, but `docker compose --profile cpu-worker up` runs `structured-worker`/`communication-worker` together, each needing its own `worker_credentials` row's scope. A shared token's digest can only match one row, so every other worker container 403'd (`worker_processor_scope_denied`) on every claim. **Found via live multi-worker testing, not by this repo's own test suite**: each live integration test (`test_worker_live.py`/`test_communication_worker_live.py`/`test_media_worker_live.py`) self-provisions its own scoped credential and runs in isolation within one `pytest` process — none of them exercises several worker *containers* running simultaneously against one shared `.env`, the actual Docker Compose deployment topology. Fixed by having `compose.yaml` override `WORKER_TOKEN` per worker service from its own env var (`STRUCTURED_WORKER_TOKEN`/`COMMUNICATION_WORKER_TOKEN`/`MEDIA_WORKER_TOKEN`), each bound to a distinct, correctly-scoped credential — `api` is unaffected (it never reads `settings.worker_token`). No change to the credential-scoping model itself (`allowed_processor_names`, digest lookup, `require_worker_principal`) — a provisioning/wiring gap, not a design gap. See `docs/architecture/worker-identity-and-security.md` and `docs/runbooks/local-development.md`.
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

# Phase 7 Gate C + Aditya Part 5 (2026-09-20)

- The current Gate B evidence is frozen as `gate-b-v1`. Only the two FIR
  OCR results and IBM AMLSim deterministic-parser result have the complete
  approved dataset/configuration/artifact/result hash chain. GoMask remains
  unavailable. VIRAT and AMI measurements remain preserved but deferred from
  release selection because their manifests are `in_progress` and their
  external safe-result hashes are not present in Git. No metric or hash was
  invented to close that gap.
- Every other planned/inaccessible dataset and candidate remains explicitly
  deferred or unavailable. Any future expansion requires a new versioned
  manifest/evaluation cycle; it cannot silently replace this release's
  evidence.
- Gate C retains the existing deterministic Phase 5 rules baseline only. It
  does not select the final relationship-scoring ML algorithm. Logistic
  Regression, XGBoost, LightGBM, calibration, comparison, and final freeze
  remain Shreshtha Part 6 work.
- `/readyz` can prove dependency/capability reachability, not that an external
  extraction worker process is alive. It reports worker process liveness as
  `not_observed`; no heartbeat registry was invented.
- Correlation outbox retry exhaustion stops the current worker process and
  leaves durable events queued. Operator restart/reconciliation remains
  required after the underlying graph service recovers.
- Collection endpoints are bounded to 200 rows per request but do not yet
  return cursor metadata. This release prevents unbounded reads; cursor-based
  pagination remains later API work.
- No Docker, LAN, new dataset/model acquisition, training, frontend, or full
  repository suite was run for Gate C, by scope.

# Phase 7 Part 6 — final release decision and documentation closure (Shreshtha)

- **No ML relationship-scoring comparison was run, by decision, not by
  omission.** Logistic Regression, XGBoost, and LightGBM were not
  implemented, trained, calibrated, installed, downloaded, benchmarked, or
  selected. See ADR-018. The reason is structural, not a resource
  shortfall: Phase 7 Part 1's synthetic case plan freezes evaluation
  *splits*, not a real, independently labelled relationship-outcome ground
  truth, so a comparison against it would not be trustworthy evidence.
- **The active release configuration is unchanged from Gate C's freeze.**
  `tracex-release-v1-baseline` (`phase5-rules-baseline`) remains the only
  enabled relationship-scoring component; no model artifact is loaded;
  `final_relationship_ml_selected` stays `false` in
  `configs/benchmarks/release-freeze.v1.json`.
- **No production code changed for this decision.** `release_freeze.py`'s
  validators, `pipeline.py`'s fail-closed configuration gate, and
  `scoring.py`'s deterministic rules scorer already enforced everything
  this decision states; this Part confirmed that by inspection rather than
  editing already-correct code.
- **ML relationship scoring remains explicit future work**, to be revisited
  only after the initial deployed product exists and a proper,
  independently labelled relationship dataset is available for a new,
  versioned evaluation cycle — never claimed as evaluated or completed for
  this release.
- **Phase 8 is unaffected by this decision.** It tests and deploys the
  frozen rules configuration on a LAN/multi-laptop setup; it does not
  change the scoring algorithm.
- Phase 7 is complete for this release's scope with the deterministic
  baseline, per this decision and Gate C's freeze.

# Phase 7 Closure — WP-1 (Shreshtha)

Closes gap register G5 (open self-registration) and G6 (no HTTP case
management; unused per-evidence ABAC hook). See ADR-019.

- **`POST /api/v1/auth/register` no longer exists (404), replaced by
  admin-gated `POST /api/v1/admin/users`.** A `create-admin` CLI
  (`app/modules/access_control/cli.py`) bootstraps the first admin;
  idempotent, refuses a second admin unless `--force` is passed.
- **New `users.system_role` column** (nullable, `CHECK` constrained to
  `NULL`/`'admin'`) — the smallest additive model for deployment-wide
  admin capability; case-level permissions are unaffected.
- **New case-management routes**: `POST /api/v1/cases`,
  `GET /api/v1/cases/{id}`, `GET /api/v1/cases/{id}/status`,
  `POST /api/v1/cases/{id}/members`. Case creation is authenticated-only
  (not case-scoped — the case doesn't exist yet); member addition is
  gated by a new `CaseAction.MEMBER_MANAGE`, granted only to
  `CASE_OWNER`/`CASE_MANAGER`.
- **Membership update/removal routes do not exist yet** — out of this
  WP's scope; adding a member is the only mutation exposed. A
  mis-provisioned membership currently has no HTTP-reachable fix.
- **Per-evidence classification is now enforced** on evidence reads: a
  caller whose case clearance is below one specific evidence item's
  `classification` gets `403` on direct read and the item is silently
  omitted from `GET .../evidence` listings.
- **A genuine pre-existing test gap was found and fixed while writing
  this WP's own tests**: `FakeAccessControlRepository.create_case` didn't
  enforce the real `cases.uq_cases_case_reference` UNIQUE constraint the
  live PostgreSQL schema has, so a duplicate-case-reference test passed
  against the fake for the wrong reason (never hit the conflict path at
  all). Fixed by making the fake raise `sa.exc.IntegrityError` on a
  duplicate reference, matching real Postgres behavior.
- **Removing the public `/register` route had a wide test-suite blast
  radius**: ~19 test files across `access_control`, `evidence_lifecycle`,
  `graph`, `integrity`, `structured_processing`, `communication_processing`,
  `media_processing`, and `security` unit/integration suites called it as
  pure test setup. All were updated to seed users directly via
  `AccessControlRepository.create_user`/`tests.fixtures.access_control.
  factories.make_user_record` instead, then continue through the real
  `/login` flow — no test assertion was weakened to accommodate this.

# Phase 7 Closure — WP-2 (Shreshtha)

Closes gap register G2 (`EntityV1` never instantiated; no entity layer).
See ADR-020.

- **Entity creation is scoped to one entity per observation, not per
  role/descriptor.** A two-party CDR/finance record's caller and callee
  are merged into one entity's identifiers rather than split into two.
  This is a documented simplification, not the final design — splitting
  by role needs `retrieval.RetrievedCandidate` to carry a descriptor-level
  identifier (it currently only carries `observation_id`), which is a
  real, larger change to the retrieval cascade's own return shape,
  deferred rather than attempted lossily here.
- **No scoring/ranking exists for entity-resolution candidates** —
  candidates carry the retrieval cascade's own reasons/identifier types/
  vector score/contradictions, but there is no "how confident" number
  beyond that, matching the register's own scope (G2 asks for candidate
  generation and reviewable decisions, not a second scoring model).
- **Entity review decisions are reversible** (`verified_same` then later
  `split`) — deliberately different from `candidate_review_decisions`,
  which allows exactly one immutable decision. Every decision is retained,
  append-only; the effective state is always the most recent one.
- **No update/remove route for an entity's own fields exists** (label,
  aliases, attributes) — only creation (via candidate generation) and
  resolution-review decisions are exposed over HTTP.
- **`POST /api/v1/entities/{id}/resolution-review` takes `candidate_id` as
  a query parameter**, not a second path segment — the plan's own route
  shape names only `{id}` in the path; `candidate_id` disambiguates which
  of that entity's several possible candidates the decision is about.
- **Resolved (Gap-Closure follow-up): case-scoped entity listing now
  exists.** `GET /api/v1/cases/{case_id}/entities` closes a real gap in the
  three routes above: none of them let a caller discover which entities
  exist for a case without already knowing their UUIDs. Surfaced by an
  external caller — TraceX-Synthetic-Data's truth-generation script — that
  had no read surface for what the entity layer produced. Same
  `require_graph_read` (`CaseAction.GRAPH_READ`) authorization and the same
  keyset-cursor pagination (`app.core.pagination`, G17) as every other
  paginated collection route; no new `CaseAction`, no change to `EntityV1`.
- **Resolved (Gap-Closure follow-up): entity creation and candidate
  generation now have a live caller.** `entity_service.create_entities_
  for_case`/`generate_entity_resolution_candidates` (this WP, ADR-020) were
  fully implemented and unit-tested from day one but had **zero callers
  anywhere in the running system** — the exact "built and tested in
  isolation, nothing real ever calls it" situation this file already
  documents twice elsewhere (graph projection; `CommunicationLinkCandidate`
  generation). Every real ingestion run produced correct observations but
  never a single entity, silently, until surfaced by live multi-service
  testing (real evidence ingested end-to-end, zero entities appeared) — not
  by this repo's own test suite, which only ever calls these functions
  directly. `intelligence_worker.py --resolve-entities --case-id <uuid>`
  (new mode, mirrors `--generate`/`--evaluate`'s exact per-case, operator-
  invoked shape) closes it: calls both functions verbatim, no change to
  their internal logic. Deliberately manual, not an automatic pipeline
  trigger — ADR-020 never specified when entity resolution should run
  relative to ingestion, and this repo's own precedent for this exact
  situation (the two cases above) is manual/deferred, not auto-wired; a
  future WP can revisit automatic triggering as its own explicit decision.
  Live-verified: run against a case with real, previously-ingested
  observations, producing 52 real entities. Candidate generation itself
  crashed on that same run (`ValueError: candidate retrieval exceeded
  bounded limit`) -- corrected below, not the "candidates, not zero"
  outcome originally reported here before that crash was discovered. See
  `intelligence_worker.py`'s own module docstring and `docs/qa/
  test-results.md` for the exact live counts.
- **Resolved (Gap-Closure follow-up, ADR-027): entity-resolution candidate
  generation no longer crashes on a real case's combinatorial identifier
  reuse.** The `create_entities_for_case` fix above only got entity
  creation working; `generate_entity_resolution_candidates`'s downstream
  call into `intelligence.retrieval.retrieve_candidates` then crashed on
  the same live case -- its exact-identifier tier does raw pairwise
  expansion (`C(N,2)` pairs for any identifier shared by `N` descriptors),
  and an account mentioned 14 times alone produced 91 pairs restating one
  fact, totalling 268 across just 4 distinct account values and blowing
  past `MAX_CANDIDATES=200`. Manually verified against Fulcrum's authored
  ground truth: all 268 were genuinely correct matches, zero false
  positives -- pure combinatorial waste, not a data or judgment problem,
  and one that will recur in any case where an identifier repeats more
  than a handful of times. Found via live testing, not by this
  repository's own test suite. New `identifier_star_edges()`
  (`retrieval.py`) collapses each identifier group's `C(N,2)` pairs down
  to `N-1` star edges, opted into only by `entity_service.py` (an
  optional, default-`None` parameter on `retrieve_candidates`; every other
  call site, including Phase 5's frozen, release-freeze-gated correlation
  pipeline, is provably unaffected). `ENTITY_CANDIDATE_CONFIG_VERSION`
  bumped `v1` -> `v2` accordingly. See ADR-027 for the full design,
  including why `retrieve_candidates` was extended rather than modified
  in place.
- **Resolved (Gap-Closure follow-up): `sourcing.py` now maps a structural
  vehicle identifier (CDR/financial `vehicle_context`, and a `vehicle_id`/
  `vehicle_context`-named `json_scalar_value` leaf -- e.g. Fulcrum's
  `structured/sightings.json`, ingested via `generic_json_v1`'s one-
  observation-per-scalar-leaf fallback) to the *existing*
  `vehicle_registration` identifier kind -- previously only reachable via
  OCR'd document text. Not a new identifier category, and not the same
  circularity problem `person_id`/`location_id` have (see the next item):
  `_EXACT_IDENTIFIER_ENTITY_TYPES` already trusted a vehicle-plate-shaped
  value from document text as a genuine, source-backed identity signal;
  this only widens which modality can supply that already-trusted kind.
  Found live: previously, every `SYN-VEH-*` Fulcrum ground-truth token
  failed to resolve to any entity. `descriptors_from_observation`'s two-
  party CDR/finance expansion merges the record's `vehicle_context` into
  *both* role-scoped party descriptors (it's shared, not role-scoped
  itself); `descriptor_from_observation` (singular) gained the same
  mapping for parity, plus a new `json_scalar_value` case matching
  `source_locator.json_path`'s exact trailing segment (never a substring)
  against `{"vehicle_id", "vehicle_context"}`. Shared with Phase 5's
  correlation pipeline (`sourcing.py` isn't release-freeze-gated the way
  `retrieval.py`'s pairing algorithm is -- confirmed `RULES_CONFIG_HASH`
  only hashes `scoring.py`'s weights -- and this module's own docstring
  already shows a history of additive extensions without version-bump
  ceremony), so `--generate`'s candidate output also sees vehicle-
  registration matches now, not only `--resolve-entities`'s. Live-verified
  against the same real Fulcrum case: entity count 52 -> 61, candidate
  count 44 -> 49 (sightings-derived observations that previously produced
  zero descriptors now produce one), and all four `SYN-VEH-FULD-*` tokens
  confirmed present on real entities' `stable_identifiers`.
- **`person_id`/`location_id`-shaped fields remain deliberately unmapped,
  however they arrive.** Unlike a vehicle registration plate, no external
  registry makes a person's or a location's identity a directly-labeled,
  independently-verifiable field in real evidence -- that is exactly what
  entity resolution exists to *infer* from contact-method/vehicle signals,
  never to accept as given; accepting a raw `person_id`/`location_id`
  field directly would be circular for real (non-synthetic) data.
  `_IDENTIFIER_TYPES` has no `person`/`location` kind at all, deliberately
  -- so Fulcrum's `SYN-PER-*`/`SYN-LOC-*` ground-truth tokens remain
  permanently out of scope for this pipeline; see
  `generate_entity_resolution_truth.py` in the sibling `TraceX-Synthetic-
  Data` repository for where that boundary is now documented on the
  truth-generation side.
- **Fulcrum's `candidate_precision`/`candidate_recall`/`false_link_rate`
  now reflect a simulated-reviewer oracle, not genuine human judgment.**
  Run for real against `case-fulcrum-dev`/`case-fulcrum-val` for the first
  time, `--evaluate` came back with every candidate implicitly "predicted
  different" (`candidate_recall: 0.0`, `candidate_precision: null`) --
  honest, not a bug: `EntityReviewOutcome.VERIFIED_SAME` is "never
  automatic, never inferred", and nobody had ever reviewed either case's
  real candidates. `TraceX-Synthetic-Data/scripts/simulate_reviews_for_
  evaluation.py` (external, evaluation-only, invoked opt-in via
  `generate_entity_resolution_truth.py --simulate-reviews-for-evaluation`)
  now applies ground-truth-driven decisions through the real `POST
  .../resolution-review` endpoint, exact-pair-match only, anything the
  truth file doesn't explicitly cover left unreviewed -- see ADR-028 for
  the full design and its Fulcrum-only, never-Nightfall boundary. This
  means these metrics measure candidate-generation/ranking quality against
  a perfect oracle reviewer, not real human-review-workflow quality -- a
  genuine human-review-quality measurement was never obtainable from a
  pre-authored synthetic truth file regardless.
- **Resolved (Gap-Closure follow-up, ADR-029): `identifier_star_edges`
  (the entry above) was itself a symptom patch, not the actual design --
  corrected here, not silently.** The master implementation plan's Section
  15.2 specifies true Tier-1 "exact blocking": group descriptors sharing
  an identifier into a block first (no scoring, no candidates), then only
  compare *across* blocks via Tiers 2-4. `identifier_star_edges` never did
  this -- it still emitted `N-1` pairwise candidate rows *within* one
  block. Run against Nightfall's real case (381 descriptors, far more
  distinct identifier groups than Fulcrum ever had), the sum of `N-1`
  edges across all groups reached 370+, still over `MAX_CANDIDATES=200`,
  still crashing `--resolve-entities` -- found live, not by this repo's
  own test suite, same as the original bug. New `exact_identifier_blocks()`
  (`retrieval.py`) replaces it entirely (removed, no parallel code path):
  union-find across descriptor IDs, transitive and cross-kind (strictly
  more correct than the old per-`(kind, value)` grouping), collapsing each
  block to its one canonical member before any pairwise comparison --
  zero within-block candidates, not `N-1`. `ENTITY_CANDIDATE_CONFIG_
  VERSION` bumped `v2` -> `v3` accordingly. `create_entities_for_case` is
  untouched -- blocking never merges entities, preserving `EntityRepository.
  get_or_create_entity_for_observation`'s explicit "never merges" invariant.
  See ADR-029 for the full design.

  **Honest correction to the entry above's live numbers**: this fix does
  not, and provably cannot, improve Fulcrum's `candidate_recall` over the
  star-edge baseline (0.1667, 2/12) -- it is mathematically 0 for *any*
  Fulcrum-shaped truth structure, confirmed by direct computation
  (`tests/unit/graph/test_intelligence.py::
  test_fulcrum_shaped_recall_is_zero_under_true_blocking_not_hand_tuned`),
  not merely theorized. Every one of Fulcrum's 12 truth "same" pairs is,
  by `generate_entity_resolution_truth.py::build_pairs`' own construction,
  two members of one identifier block -- and true blocking guarantees zero
  candidates between any two same-block members, by design, regardless of
  which specific pair a truth file happens to assert. Closing this
  specific gap would require either full `C(N,2)` coverage (reintroducing
  the volume explosion this fix exists to close) or aligning `build_pairs`'
  canonical-pair-selection rule with retrieval's own -- a `TraceX-
  Synthetic-Data` change, out of this fix's scope. Nightfall's volume
  crash is unambiguously fixed; Fulcrum's `candidate_recall` regressed
  further as a direct, provable consequence of the same fix, not a
  separate defect -- see ADR-029's own "Known, accepted limitation"
  section and this session's live-measured before/after numbers.
- **Resolved (Gap-Closure follow-up, ADR-030): Tiers 2-3 also did raw
  pairwise expansion -- ADR-029 only fixed Tier 1.** A follow-up audit
  read every candidate-generation module and found Tier 2 (alias/handle/
  transliteration) computed inline in the same unbounded `combinations()`
  loop as everything else -- live-confirmed on Nightfall's real (corpus-
  bug-fixed) data: 10 alias values repeated 6 times each produced 150
  `EXACT_ALIAS` pairs, the identical pattern Tier 1 had. Tier 3 had a
  spec-correct implementation (`PgvectorCandidateStore.search`, a real
  bounded nearest-neighbor query) sitting completely unused (zero live
  callers), while what actually ran was an inline all-pairs cosine scan in
  the same loop. New `retrieval.lexical_blocks()` (Tier 2, same union-find
  shape as Tier 1's `exact_identifier_blocks`, on a combined casefolded-
  alias/transliteration/handle token space) and
  `vector_store.vector_linked_candidates()` (Tier 3, wires in the
  previously-unused pgvector store for real) close both gaps.
  `ENTITY_CANDIDATE_CONFIG_VERSION` bumped `v3` -> `v4`. First live run
  surfaced a real bug in this fix itself, not a corpus issue: Tier 3 was
  initially called against the *raw, unblocked* descriptor set instead of
  the same reduced set Tiers 1-2 had already collapsed -- caught by
  inspecting Nightfall's real candidate graph (60 of 351 entities showed
  candidate edges, a near-complete clique, clearly wrong for supposedly
  bounded retrieval) before accepting the result, not from a passing test
  suite. Fixed by threading the same `apply_blocks` reduction through to
  `vector_linked_candidates`'s input; re-verified live. See ADR-030 for
  the full design, including a documented, deliberately-unfixed pre-
  existing characteristic of `graph_intelligence_vectors`' schema (a
  two-party CDR/finance descriptor pair sharing one `observation_id` can
  overwrite each other's vector row on upsert -- currently a no-op since
  neither carries lexical signal, not redesigned in this ADR).

  Live-verified, all three real cases, after both the Tier-1 (ADR-029) and
  Tier-2/3 (ADR-030) fixes: `case-fulcrum-dev`/`case-fulcrum-val` each
  72 entities, 36 real candidates (down from 53 candidates measured with
  the Tier-3 bug still present, confirming the fix's effect), `candidate_
  recall: 0.0` (same provable-zero reason as the ADR-029 entry above --
  unrelated to this fix), `precision_at_k`/`recall_at_k`: `0.0` (real
  ranked candidates now exist to score, previously `null`). Nightfall:
  351 entities, 43 candidates, **no `MAX_CANDIDATES` crash** -- the
  session's hard requirement -- `--evaluate` run exactly once (ADR-016):
  every metric `null` (Nightfall's truth file has zero pairs by design,
  nothing to score against). Real Nightfall candidate graph inspected
  directly (not inferred from a recall number): all 10 `SYN-PER-NF-*`
  entities form one connected component via `local_vector_candidate`
  edges at 43/45 possible pairs -- a near-complete clique, not two
  distinguishable communities with one bridge. This is the local hashed-
  token vector's own documented, pre-existing limitation (`retrieval.py`'s
  module docstring: "a weak retrieval aid, not a semantic model") showing
  up concretely: Nightfall's 10 synthetic person names are structurally
  near-identical short tokens (`SYN-PER-NF-01` .. `SYN-PER-NF-10`), so
  their hashed-token vectors land almost indistinguishably
  close together regardless of which two people a chat message actually
  concerns -- a corpus/vector-provider interaction, not a blocking bug and
  not something this session's Tier 1-3 fixes could address.
- **Resolved (Gap-Closure follow-up, ADR-032): the vector-quality gap
  above -- fixed, and the fix's real result reported precisely, not
  forced.** Replaced `hashed_token_vector`'s single-token 32-bucket hash
  (only ever used by `vector_linked_candidates`'s Tier-3 path; every other
  caller, including Phase 5's frozen pipeline, is untouched) with `vector_
  store.case_tfidf_vectors` -- real, case-scoped character n-gram TF-IDF
  (`analyzer="char_wb"`, `ngram_range=(2, 4)`) reduced by `TruncatedSVD` to
  the same 32 dimensions, so no `graph_intelligence_vectors` schema
  migration was needed. `ENTITY_CANDIDATE_CONFIG_VERSION` bumped `v4` ->
  `v5`. Required promoting `scikit-learn` from a transitive, benchmark-
  extra-only dependency to a real core one (`pyproject.toml`) -- confirmed
  it was never actually installed in the real deployment despite being
  fully resolved in `uv.lock`; the promotion resolved in under 100ms with
  no new network round-trip, low risk, same tier as `igraph`/`leidenalg`.

  **Live result, evidence-based, not assumed**: re-ran the same real
  Nightfall case with the new signal and inspected the actual similarity
  values directly (not just a candidate count). The fix genuinely works --
  the 10 `SYN-PER-NF-*` names' pairwise cosine similarities now spread
  across 0.673-0.690 (real, computed, differentiated values), a world
  away from the old scheme's uniform near-1.0 clique. But at the
  unchanged default `threshold=0.75`, every one of those similarities
  still falls short, so the real candidate count for this case is **0**,
  not "two communities + a bridge." This was not tuned or forced either
  way: full reduced-descriptor-set (21 representatives: 10 PER + 5 ACC + 6
  VEH) pairwise similarities were computed and the true maximum (0.690)
  is reported as found. The honest conclusion: the *signal* was the real
  bug and is now fixed (confirmed by the similarity spread), but
  Nightfall's actual authored corpus contains no genuine near-duplicate
  or engineered same-identity structure for *any* signal to discover --
  consistent with this session's earlier, separate finding that
  Nightfall's corpus was never designed with intra-case identity/bridge
  structure the way Fulcrum's `communities.json`/`bridge_entity_id` was.
  The remaining gap is in what the corpus was authored to contain, not in
  the retrieval algorithm.

  Fulcrum dev/val, same fix, live-verified: candidate count 36 -> 30 (both
  cases, identical shift -- expected, since both cases share the same
  corpus-generation logic and the new signal changes which descriptor
  pairs clear the vector threshold); `candidate_recall` stays `0.0` for
  the same provable ADR-029 reason, unrelated to this fix;
  `precision_at_k`/`recall_at_k` stay `0.0` (real candidates still exist
  to rank, just a different real set of them).
- **`operation_nightfall_truth.json` (master plan Section 17.2's narrative-
  level truth artifact -- expected clusters/bridge/motifs/false clues,
  distinct from `entity_resolution_truth.json`) does not exist anywhere in
  either repository, under that name or any equivalent.** Searched both
  repos exhaustively (filenames, and the plan's own quoted language --
  "false clues", "expected clusters", "never served by investigator
  APIs") -- zero matches. `communities.json`/`motif.json` (the closest
  candidates) exist only for Fulcrum's dev/validation split
  (`operation-fulcrum/case-fulcrum-{dev,val}/metadata/`), authored to
  support Fulcrum's own bridge-ambiguity/motif ground truth; Nightfall's
  corpus (`operation-nightfall/case-operation-nightfall/`) has no
  equivalent file and never has, confirmed in an earlier session's own
  audit (this session's Part E investigation) of Nightfall's actual
  metadata. A real, separate authoring gap from anything this session
  touched -- not fixed here, reported per instruction.
- **The hypothesis-assembly engine (Phase 6 Part 5) has never been
  invoked against real WP-2 entity-resolution candidate data -- and,
  live-confirmed, structurally cannot be, as currently wired.**
  `hypothesis_models.py`'s own docstring states hypotheses are "never
  automatically generated" -- this is a human-authored citation+review
  workflow (`POST /cases/{case_id}/hypotheses`, real, reachable, requires
  `CaseAction.HYPOTHESIS_PROPOSE`), not an automated evidence-for/against
  assembly engine; there is no "reason codes"/"evidence-for/against"
  field anywhere in `HypothesisRecord` to begin with. Live-invoked for
  real this session against a fresh `case-fulcrum-dev` ingestion (72
  entities, 36 real WP-2 candidates): citing a real `worker_observations`
  row succeeded end-to-end (created at `needs_review`, reviewed to
  `accepted_by_reviewer`, both real HTTP calls, both persisted). Citing a
  real WP-2 `entity_resolution_candidates` row failed with `422
  Unprocessable Entity` -- traced to `hypothesis_repository.py`'s own
  `create_hypothesis`: `supporting_candidate_ids` is verified against
  `candidate_links_table`, which is Phase 5's *correlation* pipeline
  output (`--generate`/`build_case_correlation_submission`), never WP-2's
  `entity_resolution_candidates` table (`--resolve-entities`, what this
  entire session's work populates). The hypothesis system can cite real
  observations directly, but has no path to cite any of this session's
  real entity-resolution candidates at all -- the same category of gap as
  entity-resolution's own previously-missing trigger (`docs/qa/known-
  limitations.md`'s WP-2 section), not fixed here per this session's
  discovery-only scope for this item.
- **Resolved (Gap-Closure follow-up, ADR-031): a hypothesis can now cite a
  real WP-2 entity-resolution candidate.** Root cause confirmed by date:
  ADR-015 (2026-09-15, hypothesis workflow design) predates ADR-020's
  entity-resolution work, so `HypothesisCreateSubmission.supporting_
  candidate_ids` only ever knew about Phase 5's `candidate_links_table` --
  a sequencing accident, not a deliberate exclusion (the two candidate
  tables' own separation, confirmed the same session, IS deliberate and
  stays untouched). New, parallel `supporting_entity_resolution_candidate_
  ids` field (`HypothesisCreateSubmission`/`HypothesisRecord`), verified
  against `entity_resolution_candidates_table` with the same case-scoped
  existence check already used for the pre-existing field; the
  deterministic `hypothesis_id` hash folds in the new field too, so a
  hypothesis with vs. without this citation gets two different, both-
  idempotent IDs. Additive migration `7a8b9c0d1e2f` backfills existing
  rows with an empty array. Neo4j projection is explicitly, deliberately
  skipped for this citation type (WP-2 has no Neo4j footprint at all,
  by design) -- documented as a known, accepted limitation, not silently
  dropped. Live-verified: created a real hypothesis via `POST /hypotheses`
  against a fresh Fulcrum ingestion, citing one of its 36 real WP-2
  candidates through the new field -- see ADR-031 for the full design and
  root-cause writeup.

# Phase 7 Closure — WP-3 (Shreshtha)

Documents gap register G10 (typed labels/relationships), G11 (pgvector),
and G12 (hot window). See ADR-021.

- **G10 CLOSED for relationship-kind enforcement; entity/event-type
  enforcement is deliberately advisory, not strict.** `EntityV1`'s own
  frozen-contract docstring states the taxonomy is later-phase, open-string
  work by design — a strict enum would break that contract. Relationship
  node-kind *combinations* are strictly validated instead
  (`taxonomy.validate_relationship_combination`).
- **`CANDIDATE_ASSOCIATION` (Observation<->Observation) is documented but
  not yet projected.** It is reserved for Phase 5's existing
  `correlation_candidate_links`, a real durable record — but wiring it into
  `integration_projector.py` was judged too large/risky a change for this
  WP's scope (Phase 5's projection path is Gate C-adjacent). `POSSIBLY_
  SAME_AS`/`CONTRADICTED_BY` (Entity<->Entity, backed by WP-2's own
  `entity_resolution_candidates`) ARE projected, via the new, additive
  `entity_projection.py`.
- **`PART_OF_THREAD` was not added at all.** No durable "thread" concept
  exists in any contract or model in this codebase today; adding the
  relationship kind without a record to back it would be exactly the
  fabricated edge this pack's own rules forbid.
- **Review overlays are edge properties, not separate relationship
  kinds.** `POSSIBLY_SAME_AS`'s `effective_status` property carries
  `needs_review`/`verified_same`/`rejected`/`split` — a separate
  relationship type per outcome would conflict with WP-2's append-only,
  reversible-decision design.
- **G11 (open, unchanged): `graph.intelligence.vector_store` stores
  deterministic hashed-token vectors, not a real semantic embedding
  model's output.** Replacing it is a materially larger change (new
  dependency, new Gate B benchmark cycle) than this WP's scope.
- **G12 (open, unchanged): `HOT_WINDOW_SECONDS = 900` remains a fixed
  module constant**, not Redis-backed or case-configurable — an
  operational-tuning change, not a taxonomy one.
- **A genuine pre-existing regression test conflicted with this WP and was
  updated, not weakened**:
  `test_projection.py::test_relationship_kind_enum_has_no_entity_to_entity_kind`
  exhaustively asserted `GraphRelationshipKind` had zero entity-to-entity
  members at all, encoding a real Phase 5A design decision ("every
  evidentiary connection is mediated by a time-bounded Event"). WP-2/WP-3's
  `POSSIBLY_SAME_AS`/`CONTRADICTED_BY` are entity-to-entity but are
  identity-*resolution* review metadata, not an evidentiary connection --
  a categorically different concept the original test's reasoning didn't
  anticipate because entity resolution didn't exist yet when it was
  written. Both `models.py`'s docstring and the test were updated to state
  the narrower, still-exhaustive invariant precisely (see ADR-021); no
  assertion was deleted or loosened, only extended with the same rigor.

# Phase 7 Closure — WP-4 (Shreshtha)

Documents gap register G3 (case notes/handoff), G13 (durable review/
hypothesis projection replay), and part of G7 (rejected-item read
filtering, evidence integrity re-check, evidence reprocess, case audit
read). See ADR-022.

- **G13 CLOSED**: a third, separate, minimal durable outbox
  (`review_hypothesis_projection_events`, migration `3c4d5e6f7a8b`) rather
  than genericizing either existing outbox — deliberate, to avoid risk to
  Gate-C-adjacent Phase 5 projection code. `intelligence_worker.py
  --replay-review-once` drains it.
- **G3 CLOSED**: append-only `case_notes` (migration `4d5e6f7a8b9c`, same
  trigger precedent as `entity_review_decisions`); role-based visibility
  (`CaseAction.CASE_NOTE_READ_ALL`) enforced in `notes_service.py`, not the
  database. `graph/handoff_service.py`'s `build_handoff_summary()` is
  computed at read time from existing tables — no new table, no second
  source of truth for review state.
- **G7 (partial) CLOSED**: `include_rejected=false` (default) on
  `list_candidates_for_review`/`list_hypotheses` now actually excludes
  `REJECTED_BY_REVIEWER` items, closing a real, previously-untested
  contradiction between documented and actual behavior. New
  `GET .../evidence/{id}/integrity` (streams + re-hashes via `ObjectStorage.
  open_stream`, never buffers a whole file; `503` — not `200` with
  `matches: false` — on an unreadable object, since "tampered" and
  "unreadable" are different failure modes). New
  `POST .../evidence/{id}/reprocess` (required `Idempotency-Key`, distinct
  job key from the original upload job, so replay can never collide with
  it). New `GET .../cases/{id}/audit` (bounded, case-scoped, safe fields
  only).
- **Deferred: hypothesis "contradicting" evidence.**
  `HypothesisCreateSubmission`/`HypothesisRecord` still carry only
  `supporting_observation_ids`/`supporting_candidate_ids` — no symmetric
  "contradicting" field. Phase 5's separate `CorrelationSubmission`
  already has `contradictory_observation_ids`, but a hypothesis is a
  distinct, later-phase concept built over reviewed candidates; adding a
  matching field would need a new migration column plus a real validation
  decision (can a hypothesis exist with only contradicting evidence?) and
  its own tests — judged out of WP-4's scope. Not implemented, not
  simulated as done.
- **New tests**:
  `tests/unit/graph/{test_review_projection_replay,test_handoff_service}.py`
  (9 tests), `tests/unit/access_control/test_notes_service.py` (4 tests),
  8 new cases appended to `tests/unit/evidence_lifecycle/test_evidence_
  api.py` for the integrity/reprocess routes (22 tests total in that file,
  up from 14).
- `docs/decisions/ADR-022-review-memory-and-evidence-audit.md` (new).
- Exactly one alembic head after both new WP-4 migrations
  (`3c4d5e6f7a8b` -> `4d5e6f7a8b9c`); `git status --short`/`git diff
  --cached --stat` remain empty (nothing staged).

# Phase 7 Closure — WP-5 (Shreshtha)

Documents gap register G4 (integrity hardening). See ADR-023.

- **CLOSED: reconciliation coverage gap for entity-resolution decisions
  and case notes.** Both had zero integrity coverage before this WP --
  fixed with `to_integrity_submission()` on `EntityReviewDecisionRecord`/
  `CaseNoteRecord` (new `ENTITY_RESOLUTION_DECISION`/`CASE_NOTE_ADDED`
  event kinds), best-effort write-time wiring, and reconciliation scan
  branches for both new source tables (no schema change needed --
  `entity_review_decisions`/`case_notes` already existed from WP-2/WP-4).
- **CLOSED: `signing_keys_public` append-only key registry** (migration
  `5e6f7a8b9c0d`) + `rotate-key`/`list-keys` CLI commands. Deliberately
  register-only, no revocation field -- see ADR-023 for why.
- **CLOSED: `ManifestSink` protocol** (`manifest_sink.py`) with
  `FilesystemManifestSink`/`MinioManifestSink` implementations + `archive`
  CLI command. Write-once is an application-level guarantee only (refuses
  to overwrite an existing key/path) -- real object-lock/retention is an
  infra-level MinIO bucket setting, out of scope here.
- **CLOSED: scheduled checkpointing** (`checkpoint-once`/`checkpoint-loop`
  CLI commands). Discovers pending work from the pre-existing
  `integrity_sequence_counters` table against `merkle_checkpoints` -- no
  new table needed for discovery.
- **Live-unverified, not verified**: `MinioManifestSink`'s live round-trip
  test and the new `list_pending_checkpoint_ranges`/`build_pending_
  checkpoints` live tests all self-skip in this environment -- PostgreSQL
  was not reachable when this WP ran (MinIO and Neo4j were), and every
  test in `tests/integration/integrity/` shares one Postgres-gated
  `_migrated_database` autouse fixture, matching the existing precedent
  (`evidence_lifecycle/conftest.py`'s MinIO-only `minio_storage` fixture
  is gated the same way). All new logic is covered by real unit tests
  (`FilesystemManifestSink`, `LoadedSigningKey.public_key_material`) and
  passes `ruff`/`mypy` cleanly; the repository/service-level SQL and the
  real MinIO round-trip are implemented but not run against live infra.
- **Genuine pre-existing gap found and fixed while running the full
  `tests/unit/integrity/` sweep for the first time this session**:
  `test_migration_head.py::test_phase_6_migration_is_the_current_head`
  hardcoded a stale head revision (`a3b4c5d6e7f8`) that predates every
  migration WP-1 through WP-5 added -- it had been silently broken since
  WP-1 (`1a2b3c4d5e6f`) because no prior WP's "required tests only" run
  happened to include this file. Fixed by updating the assertion to the
  real current head (`5e6f7a8b9c0d`) and renaming the test; `test_
  exactly_one_alembic_head` (the assertion that actually matters) was
  passing correctly throughout.
- **New tests**: `tests/unit/integrity/test_manifest_sink.py` (3 tests),
  2 new tests in `test_signing.py` (`public_key_material`),
  `tests/unit/graph/test_entity_models.py` (3 tests), `tests/unit/access_
  control/test_notes_models.py` (4 tests), 3 new tests in `tests/unit/
  access_control/test_cases_api.py` (notes/audit HTTP routes --
  previously zero HTTP-level coverage for those WP-4 routes; a real
  `FakeCaseNoteRepository` fixture was added to make this possible), 9 new
  live-integration tests in `test_repository_live.py` (signing-key
  registry + pending-checkpoint discovery), 2 new live-integration tests
  in `test_manifest_sink_live.py` (all currently self-skipping per above).
- `docs/decisions/ADR-023-integrity-hardening.md` (new).
- Exactly one alembic head after the new migration
  (`4d5e6f7a8b9c` -> `5e6f7a8b9c0d`); `git status --short`/`git diff
  --cached --stat` remain empty (nothing staged).

# Phase 7 Closure — WP-6 (Shreshtha)

Documents gap register G7 (rest: graph snapshot/path/analytics/motifs,
pagination), G8 (event catalog), G16 (worker heartbeat registry), G17
(readyz observed liveness). See ADR-024.

- **CLOSED: `GET /cases/{id}/graph`, `POST /cases/{id}/graph/path`,
  `GET /cases/{id}/analytics`, `GET /cases/{id}/motifs`.** Snapshot/path
  are scoped to `Entity`/`Event` nodes only (the existing `schemas.py`
  module-level rule against exposing raw Neo4j labels/implementation
  detail forbids a generic dump of every node kind) -- see ADR-024 for
  the full reasoning, including how `max_hops` is enforced without ever
  interpolating a value into Cypher text (this module's own absolute
  rule).
- **CLOSED: `GET /api/v1/admin/workers` + worker heartbeat registry.**
  New `worker_credentials.last_seen_at` column (migration `6f7a8b9c0d1e`),
  populated by `require_worker_principal` on every successful auth --
  no separate heartbeat endpoint needed. Narrows (does not remove) `list_
  worker_credentials`'s prior "never exposed through a public API"
  docstring to "never exposed without `system_role=admin`."
- **CLOSED: `/readyz`'s `worker_process_liveness`** is now real (counts
  by `active`/`stale`/`never_seen` from the heartbeat registry) when
  Postgres is reachable; still honestly reports `not_observed` with a
  `reason` when it genuinely cannot ask, never a fabricated status.
- **CLOSED: `AuditEventType` catalog** (`audit_catalog.py`, 29 verified
  values) + a static drift-prevention test
  (`test_audit_event_catalog.py`) that fails on any uncatalogued or dead
  entry. `record_audit_event`'s signature is deliberately unchanged
  (still plain `str`) -- see ADR-024 for why a full ~29-call-site
  migration to the enum type was judged out of this WP's risk budget.
  **The "21-name event catalog" figure from the original gap-closure
  prompt could not be verified** -- that document was not available when
  this catalog was built. The catalog instead reflects the real, current,
  grep-verified call-site count (29), which is what the drift test locks
  in going forward.
- **CLOSED (partial): pagination.** `HypothesisRepository.list_
  hypotheses`/`CaseNoteRepository.list_notes` gained `offset`; `GET
  /cases/{id}/notes` gained its first-ever `limit` (previously
  unbounded). **Deliberately not touched**: `GraphCorrelationIntegration
  Repository.list_candidates` (Phase 5, Gate-C-adjacent, shared by
  multiple routes) remains `limit`-only -- same risk-avoidance reasoning
  as ADR-021/ADR-022's treatment of Phase 5 code.
- **Live-unverified, not verified**: `list_hypotheses`'s new `offset`
  parameter has no dedicated live-integration test (mirrors already-
  integration-tested `list_case_observations` logic exactly; covered by
  the unit suite and mypy, not a live DB test in this WP).
- **A genuine pre-existing gap found and fixed while running the full
  unit suite for the first time this session** (2144/2145 passed before
  this fix): `test_migration_head.py`'s hardcoded-head assertion needed
  updating again (third time this gap-closure effort: WP-1 implicitly,
  WP-5, now WP-6) -- every new migration breaks it by design. Fixed to
  the real current head (`6f7a8b9c0d1e`); not redesigned to avoid this
  recurring cost, since that would be scope creep beyond gap closure --
  flagged in ADR-024 as a maintenance note for whoever owns that test.
- **New tests**: 12 new tests in `test_graph_api.py` (snapshot/path/
  analytics/motifs, including a fixed-vs-requested-hop-bound case), 5 new
  tests across `test_api_health.py` (real worker-liveness counts) and
  `test_worker_identity_api.py` (heartbeat-touch-on-auth), 3 new tests in
  `test_api.py` (`GET /api/v1/admin/workers`), 3 new tests in
  `test_audit_event_catalog.py`, 1 new test in `test_cases_api.py`
  (notes offset pagination).
- `docs/decisions/ADR-024-read-apis-event-catalog-worker-liveness.md`
  (new), `docs/architecture/audit-event-catalog.md` (new).
- Exactly one alembic head after the new migration
  (`5e6f7a8b9c0d` -> `6f7a8b9c0d1e`); `git status --short`/`git diff
  --cached --stat` remain empty (nothing staged).

# Phase 7 Closure — WP-7B (Shreshtha)

Documents gap register G1 (offline evaluation harness, code only). See
ADR-025. No database migration in this WP (PostgreSQL-only, reuses
`EntityRepository` unchanged; no Neo4j reads either).

- **CLOSED: real offline evaluator** (`graph/intelligence/evaluation.
  py::run_offline_evaluation`, `graph/intelligence/truth_loader.py`).
  Scores entity-resolution candidate precision/recall/false-link-rate/
  false-merge-rate/precision-recall@k against a human-authored truth
  spec of entity pairs. `temporal_boundary_correctness` is always `None`
  -- no event/temporal truth schema exists yet; see ADR-025.
- **CLOSED: `intelligence_worker.py --evaluate --case-id <uuid>`** CLI
  entry point. Skips cleanly (prints a `deferred: true` report, exits 0)
  when `TRACEX_SYNTHETIC_DATA_ROOT` is unset.
- **CLOSED: static import-boundary test** (`test_no_api_route_ever_
  imports_the_evaluator_or_truth_loader`) -- grep-enforced, not just a
  docstring claim, that no `app/api/*.py`/`*_api.py`/`main.py` file ever
  imports the evaluator or truth loader.
- **NOT IN SCOPE, by explicit user instruction: authoring real truth
  data.** The truth-data JSON schema this evaluator expects
  (`<TRACEX_SYNTHETIC_DATA_ROOT>/<case_id>/entity_resolution_truth.json`)
  is fully documented in ADR-025's "Truth data schema" section -- this is
  exactly what needs to be built in the sibling `TraceX-Synthetic-Data`
  repository. Nothing in this codebase reads or depends on that repo
  existing; the evaluator has never been run against real truth data and
  cannot be honestly claimed as "verified" against one -- only "verified"
  against synthetic unit-test fixtures constructed in this codebase's own
  test suite.
- **New tests**: `tests/unit/graph/test_intelligence_evaluation.py`
  (12 tests: root resolution, truth-file loading/validation, perfect-
  system/false-merge/missing-candidate scoring scenarios, the import-
  boundary static check), 2 new tests in `test_intelligence_worker_
  loop.py` (`--evaluate` CLI wiring).
- `docs/decisions/ADR-025-offline-evaluation-harness.md` (new).
- `git status --short`/`git diff --cached --stat` remain empty (nothing
  staged).

# Phase 7 Closure — WP-8 (Shreshtha)

Documents gap register G14 (CI live infra/secret-scan/nightly), G15
(compose worker profiles), G18 (backup-restore/integrity-verification
runbooks), G19 (tracked stray artifact cleanup). See ADR-026.

- **CLOSED: CI live service containers.** `postgres`/`neo4j`/`redis`
  added as GitHub Actions `services:`; MinIO started via a manual
  `docker run` step (GitHub Actions `services:` cannot pass MinIO's
  required command argument). Credentials match `.env.example` exactly
  -- zero test code changes needed for the integration suite to actually
  run in CI instead of self-skipping every time.
- **CLOSED: secret scanning.** `gitleaks` CLI (not the `gitleaks-action`
  Marketplace wrapper, to avoid its organization-license gate) installed
  from a pinned release. Found and allowlisted (`.gitleaks.toml`) two
  real, verified-harmless false positives (`idempotency_key` superficially
  matching the `generic-api-key` rule) -- confirmed zero leaks with the
  allowlist applied, two without it, both before wiring this into CI.
- **CLOSED: nightly CI.** A `schedule:` cron trigger (`17 3 * * *`) reruns
  the exact same live-infra job, catching environmental drift on days
  with no open PR.
- **CLOSED: `cpu-worker`/`gpu-worker` compose profiles**, replacing the
  single coarse `workers` profile. Two brand-new Compose services
  (`structured-worker`, `communication-worker`) that previously had none
  at all. Verified: bare `docker compose up` (no `--profile`) is
  unchanged (still API + infra only).
- **Live-unverified, not verified: `structured-worker`/`communication-
  worker` running under `restart: unless-stopped` with `--once`.**
  Neither worker module has a real poll-loop mode (a pre-existing,
  documented Phase 7 Part 2/4 scope boundary, not touched by this WP) --
  each container restart is one more claim attempt, not a clean poll
  loop like the other three workers' real `--loop` modes. Documented
  honestly in the runbook, not silently presented as equivalent.
- **CLOSED: `docs/runbooks/{backup-restore,integrity-verification}.md`**
  (both new). Pure documentation of existing capability -- no new backup
  tooling or CLI commands were added.
- **CLOSED (partial): `:memory:.ses` cleanup.** Deleted from the working
  tree, `.gitignore`d. Traced to commit `6af8ac6` ("Completed gaurav/
  phase-4 (#35)"). **Root cause not definitively confirmed**: verified it
  is *not* produced by this repo's core media deps (`opencv-python-
  headless`, `onnxruntime`) via a clean import test; the optional heavy
  ML extras from that same commit (`paddleocr`/`paddlepaddle`/
  `ultralytics`/`torch`) were not installed in this environment to test
  further -- reproducing the exact write would need one of those extras
  installed and file-creation traced during real model instantiation,
  outside this WP's time budget. Honestly reported as a partial
  investigation, not a confirmed root cause.
- `docs/decisions/ADR-026-ci-compose-profiles-and-cleanup.md` (new).
- No database migration this WP; `git status --short`/`git diff --cached
  --stat` remain empty (nothing staged) except the working-tree changes
  described above (all uncommitted, per this whole effort's "no commit/
  push/stage" constraint).

# Phase 7 Closure — Gap-Closure re-close (Shreshtha)

After the first closure pass and its final report, the original gap-
closure prompt document (`/home/nipun/Downloads/TraceX_Phase1-7_Gap_
Closure_Prompt.md`) became available again for re-reading in full. That
re-read found three real gaps the first pass had mis-scoped from an
incomplete/compacted memory of the prompt, plus resolved G9's status. All
fixed in this pass; documented here rather than silently folded into the
earlier WP sections, since the earlier sections' text was written before
this correction and should not be quietly rewritten as if the mistake
never happened.

- **G16 was incomplete.** The first pass closed the worker-heartbeat-
  registry half of G16 (`/readyz` observed liveness, `GET /api/v1/admin/
  workers`) but never built `GET /metrics` at all, and built `GET /api/
  v1/admin/workers` (admin-gated) instead of the plan's explicitly
  worker-credential-scoped `GET /api/v1/internal/workers`. **CLOSED now**:
  `GET /metrics` (`app/api/health.py`) -- hand-rolled Prometheus text
  format (no new `prometheus_client` dependency; the format is a handful
  of fixed-shape lines for a small, bounded metric set), covering process/
  dependency/worker-fleet aggregates only, never a case-scoped value.
  `GET /api/v1/internal/workers` (`app/modules/evidence_lifecycle/
  internal_api.py`, new `worker_fleet_router`) -- worker-credential-
  scoped via the existing `require_worker_principal` boundary, additive
  alongside (not replacing) the admin route.
- **G8 was mis-scoped.** The first pass built a catalog of `SecurityAudit
  EventRecord.event_type` values (29, real, still useful, kept as `docs/
  architecture/audit-event-catalog.md`) believing that was the "21-name
  event catalog" G8 asked for. Re-reading the prompt in full: G8's "21
  plan §20.2 names" is a different, broader catalog of TraceX's internal
  *domain* events (`graph.updated`, `checkpoint.sealed`, `worker.
  heartbeat`, `entity.*`, etc.), not security-audit telemetry. **CLOSED
  now**: `app/core/event_catalog.py` (`EventCatalogName`, 21 names, built
  bottom-up from this codebase's real emission points since §20.2's exact
  text was still unavailable), with `tests/unit/test_event_catalog.py`
  enforcing that every `IntegrityEventKind`/`AuditEventType` member and
  every one of the 21 catalog names is accounted for. See `docs/
  architecture/event-catalog.md`.
- **G17 was mis-scoped.** The first pass added `limit`+`offset` to two
  endpoints and called that G17 closed. Re-reading the prompt: G17
  explicitly asks for "cursor metadata (opaque, case-bound, tamper-
  evident)" with a test asserting "cursor from Case A rejected for Case
  B" -- offset pagination cannot satisfy either property (an offset is
  neither opaque nor case-bound). **CLOSED now (partial coverage)**:
  `app/core/pagination.py` -- a real, tested, HMAC-signed keyset-cursor
  primitive (`encode_cursor`/`decode_cursor`, `CursorPosition`), applied
  to `GET /cases/{id}/hypotheses` and `GET /cases/{id}/notes` (both
  gained `?cursor=`, `next_cursor` in their response, real round-trip and
  cross-case-rejection tests). `offset` is kept alongside `cursor` on
  both routes for a caller that hasn't adopted cursors yet -- cursor
  takes precedence when both are given. **Not extended to every
  collection endpoint** -- `GraphCorrelationIntegrationRepository.list_
  candidates`/`list_correlations` (Phase 5, Gate-C-adjacent, shared by
  multiple routes including `review_candidate`) and `queries.list_case_
  observations` (already offset-paginated, Phase 3/5) remain untouched,
  consistent with every prior WP's risk-avoidance stance on Phase 5
  projection-adjacent code (see ADR-021/022/024). A future WP extending
  cursor pagination further can reuse `core.pagination` directly -- the
  primitive itself is complete and generic, only its application is
  partial.
- **G9 resolved (was previously reported "status unknown").** Re-reading
  the prompt recovered G9's real text: "Plan tables absent (`roles/system
  role`, `evidence_artifacts`, `job_attempts`, `entity_resolution_
  candidates`, `hypothesis_evidence`, `case_notes`, `handoff_summaries`,
  `signing_keys_public`, `model_registry`, `benchmark_runs`) — add only
  what a delivered feature needs; document the rest as deliberate
  deviations." Per-table verdict:
  - `roles/system role` — **CLOSED** (WP-1: `users.system_role`).
  - `evidence_artifacts` — **NOT-A-GAP**: already implemented under
    different names (`media_derived_artifacts` table, Phase 4;
    `worker_results.derived_artifacts` JSONB column, Phase 2) — the
    capability exists, the plan's exact table name doesn't.
  - `job_attempts` — **DEFERRED, genuinely open**: `worker_jobs.attempt`/
    `max_attempts` are counters only (`last_error_*` columns hold only
    the *latest* failure) — no per-attempt history table exists, so "what
    happened on attempt 1 vs attempt 2" cannot be reconstructed after a
    retry. No feature delivered in this gap-closure effort needed that
    history, so per G9's own "add only what a delivered feature needs"
    instruction, it was not speculatively built.
  - `entity_resolution_candidates` — **CLOSED** (WP-2).
  - `hypothesis_evidence` — **DEFERRED, genuinely open** (already flagged
    in WP-4/ADR-022): `HypothesisRecord` stores only `supporting_
    observation_ids`/`supporting_candidate_ids` — no "against"/
    contradicting evidence field or table exists for hypotheses (Phase
    5's separate `CorrelationRecord.contradictory_observation_ids` is a
    different concept, not reused here).
  - `case_notes` — **CLOSED** (WP-4).
  - `handoff_summaries` — **NOT-A-GAP by design**: computed at read time
    (`handoff_service.py::build_handoff_summary`), deliberately no table
    — see ADR-022.
  - `signing_keys_public` — **CLOSED** (WP-5).
  - `model_registry` — **NOT-A-GAP**: implemented as a file-based typed
    registry (`configs/benchmarks/*.v1.json` + `ModelCandidateCatalogV1`/
    `ModelCandidateV1`, Phase 7), not a database table — a pre-existing,
    deliberate architectural choice (versioned, git-diffable, Gate-C-
    frozen artifacts benefit from files over mutable rows), not something
    this gap-closure effort should second-guess.
  - `benchmark_runs` — **NOT-A-GAP**: same file-based reasoning
    (`BenchmarkRunV1`/`BenchmarkRunStatus`, `benchmark-results.v1.json`).
- **New tests**: `test_api_health.py` gained 3 `/metrics` tests,
  `test_worker_identity_api.py` gained 3 `/internal/workers` tests,
  `test_event_catalog.py` (new file, 13 tests), `test_pagination.py`
  (new file, 8 tests), `test_graph_api.py` gained 4 hypothesis-cursor
  tests, `test_cases_api.py` gained 2 case-note-cursor tests (23 new
  tests total).
- No database migration this re-close pass; `git status --short`/`git
  diff --cached --stat` remain empty (nothing staged).

## G4 completion, live-infra escalation, and two pre-existing bugs found

The original WP-5 section above states real object-lock/retention was
"an infra-level MinIO bucket setting, out of scope here" and that
`MinioManifestSink`'s live round-trip was "live-unverified" (PostgreSQL
unreachable that session). Both are now closed.

- **CLOSED: `MinioManifestSink` real object-lock (COMPLIANCE mode).**
  `Settings.integrity_manifest_retention_years` (default 10). `ensure_
  bucket()` now creates its bucket with `object_lock=True` and calls
  `set_object_lock_config(..., ObjectLockConfig(COMPLIANCE, years,
  YEARS))` -- but only when it creates the bucket itself, since object-
  lock cannot be retrofitted onto an already-existing bucket (a real S3/
  MinIO API limitation, not a design choice); an already-existing bucket
  is left exactly as it is. `COMPLIANCE` mode (not `GOVERNANCE`) means no
  principal, not even a MinIO admin with root credentials, can
  delete/overwrite a manifest before its retention expires.
- **Verified against real, live MinIO** (not just unit-mocked): two new
  tests in `tests/integration/integrity/test_manifest_sink_live.py`
  (`test_ensure_bucket_enables_compliance_mode_object_lock`, `test_
  object_lock_rejects_deleting_the_specific_locked_version`) both pass.
  The second test targets the exact version id `write_manifest` created
  with a version-specific `remove_object` call and asserts MinIO itself
  rejects it (`S3Error: code=InvalidRequest, message="Object is WORM
  protected and cannot be overwritten"`) -- the real proof of write-once,
  independent of this module's own application-level `ManifestAlready
  ExistsError` check. (An unversioned `remove_object` only adds a delete
  marker under S3 versioning semantics and was confirmed, by hand against
  live MinIO, to leave the original locked version untouched either way
  -- not a bug, correct S3 behavior, and not what these tests rely on.)
- **Live-infra escalation.** `docker compose up -d postgres redis` was
  run this pass (Neo4j and MinIO were already up) specifically to
  eliminate self-skips and get the full suite running against every
  piece of real infra at once for the first time in this whole gap-
  closure effort. This surfaced two genuine, pre-existing bugs -- neither
  introduced by any WP in this effort -- that had never been exercised
  because no prior session's test run happened to have every infra
  dependency live simultaneously:
  - `tests/integration/access_control/test_auth_lifecycle_live.py` called
    `service.provision_user(..., provisioned_by=uuid4())` with a
    fabricated, never-persisted UUID at 4 call sites. This violates a
    real, pre-existing foreign key (`security_audit_events.user_id_
    nullable_fkey -> users.user_id`, present since the very first
    migration `7e8499f34f29`) once FK constraints are actually enforced
    against live PostgreSQL. **Fixed**: a `_seed_provisioner()` helper now
    creates a real user via `repository.create_user(...)` and passes its
    real `user_id` as `provisioned_by`.
  - `tests/integration/graph/test_outbox_repository_live.py` had 5 call
    sites that claim a fixed `batch_size=10` batch and then assume the
    just-inserted row is in it (`(job,) = [j for j in claimed if ...]`).
    `GraphProjectionOutboxRepository.claim_batch` is a genuinely global,
    unscoped, oldest-first work queue by design (mirrors the real
    production consumer in `graph/intelligence_worker.py::replay_loop`)
    -- against a live database accumulating rows across a 2500+-test full
    suite run, a single small batch can legitimately miss a freshly-
    inserted row that sorts behind older queued/deferred rows, causing a
    `ValueError: not enough values to unpack`. **Fixed**: a new `_claim_
    until_found()` helper loops `claim_batch` (mirroring how the real
    `replay_loop` consumes this same queue) until the target row is found
    or the queue is exhausted; all 5 call sites now use it.
- **Final full-suite result, against fully live infra (postgres, redis,
  neo4j, minio all healthy)**: `2571 passed, 35 skipped, 0 failed` in
  ~4m51s (verified twice, with `-rs` the second time to confirm every
  skip reason). All 35 skips are for reasons outside this pass's scope,
  none masking a real failure: 23 need the `api` container itself running
  (`docker compose up --build -d`, separate from the four infra
  services), 9 need `TRACEX_BENCHMARK_DATA_ROOT` (Gate B MacBook
  pre-flight benchmark datasets), 2 need the optional `ultralytics`
  dependency, and 1 needs a bootstrapped NER model asset.
- `ruff format --check .`, `ruff check .`, and `mypy app` all pass clean
  after this pass. No database migration; `git status --short`/`git diff
  --cached --stat` remain empty (nothing staged).
