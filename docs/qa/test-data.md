# Test Data

## Phase 4 release-gate synthetic data

Phase 4 focused tests use only generated manifest boundaries, synthetic frame
and source-relative time metadata, synthetic social record/JSON-path markers,
and deterministic SHA-256 commitments. They contain no raw frames,
transcripts, chat messages, credentials, object-store URLs, or sensitive
evidence. The blocked Compose gate did not run an API container or create
test-domain data: its image build failed before the service started.

## Phase 3 final acceptance fixtures

The image/video OCR checks use generated labelled PNG/JPEG/MP4 fixtures with
the text `TRACEX OCR`; they are synthetic, non-sensitive, and are cleaned up
with their task-created live case data. No raw evidence, object URI, claim
token, credential, or OCR payload is recorded in acceptance documentation.

## No real evidence

Phase 3 graph-mapping fixtures use labelled synthetic values such as
`SYNTH-SOURCE-001`, `SYNTH-DEST-002`, synthetic endpoints, and a fixed 2026
timestamp. They contain no real evidence, accounts, phone numbers, people,
or documents.

Nothing in this repository — fixtures, tests, or documentation examples — uses real, sensitive, or production investigative material. TraceX handles genuinely sensitive data once later phases start real ingestion; until then, every value below is synthetic and safe to commit.

## Synthetic fixtures

All contract test data is generated in-process by `tests/fixtures/factories.py` (`make_evidence_record`, `make_observation`, `make_entity`, `make_event`, `make_worker_job`, `make_worker_result`, plus the smaller `make_source_locator`/`make_extractor` builders). Each factory returns a fully-valid instance with a fixed synthetic timestamp (`FIXED_TIME = 2026-01-01T12:00:00Z`) and placeholder values — e.g. `"John Doe"` / `"analyst-1"` / `sha256="a"*64` — so tests are deterministic and never depend on real-world identifiers. Individual tests override specific fields via keyword arguments to construct edge cases (invalid confidence, empty locator, malformed idempotency key, etc.) without duplicating every other required field.

## Test-only configuration values

`tests/conftest.py` sets safe, syntactically-valid placeholder environment variables (fake DSNs pointing at `localhost`, passwords literally named `test-only`) before any test module imports `app.main`, so the suite never depends on a developer having created a real `.env` first. These values are never used to open a real connection in the default (`uv run pytest`) run — see `docs/qa/known-limitations.md`.

## Live-infrastructure test data

`tests/integration/test_readiness_live.py` reads real connection details from a developer's own `.env` (git-ignored, never committed) when present, and only exercises whichever of PostgreSQL/Neo4j/Redis/MinIO are actually reachable via `docker compose up -d postgres neo4j redis minio`. No fixture data is written to these services in Phase 1 — the checks are pure connectivity/liveness probes (`SELECT 1`, `RETURN 1`, `PING`, `bucket_exists`), not data round-trips, since Phase 1 defines no domain tables or evidence writes.

## Graph fixtures (`tests/fixtures/graph/factories.py`)

`make_linked_graph_fixture()` builds a single, internally-consistent, synthetic `EvidenceRecordV1 -> ObservationV1 -> EntityV1 -> EventV1` chain on top of the base `tests/fixtures/factories.py` builders — wiring `case_id`/`evidence_id`/`observation_id`/`entity_id` together so `app/modules/graph/projection.py` can apply the whole chain cleanly (the base factories are each independently random by default and don't guarantee that). Accepts `case_id=`/`entity_id=` overrides so tests can deliberately reuse an ID across two fixtures — e.g. `tests/integration/graph/test_case_isolation.py` projects the same `entity_id` into two different cases to prove they never share graph identity. Same rule as everywhere else in this repo: every value is synthetic, no real evidence or identifiers.

`tests/integration/graph/` writes real (synthetic) nodes into a live Neo4j instance and always tears them down afterward — every test that projects data does so under a freshly-generated `case_id` and `DETACH DELETE`s everything under it in a fixture/test teardown, so repeated runs never accumulate leftover graph data.

## Document/structured-processing fixtures (`tests/fixtures/structured_processing/`)

`builders.py` constructs real, minimal file bytes for every supported format directly — no `reportlab` (not an allowed dependency for this phase): PDFs are hand-assembled as a minimal valid single-font byte stream (`build_minimal_pdf`, supports `None` per page to simulate a scanned/image-only page) plus `build_encrypted_pdf` via `pypdf`'s own writer; DOCX/XLSX (`build_docx`/`build_xlsx`) are built with the same `python-docx`/`openpyxl` libraries the app parses them with. `factory.py`'s `make_evidence_and_job` builds a matched `EvidenceRecordV1`/`WorkerJobV1` pair with a fixed synthetic timestamp, following the same pattern as `tests/fixtures/factories.py`. Every FIR-style fixture text (FIR numbers, phone numbers, emails, vehicle plates, amounts) is synthetic and invented for these tests — none of it corresponds to a real case, person, or account.

## Access-control fixtures (`tests/fixtures/access_control/`)

`factories.py`'s `make_user_record`/`make_case_record`/`make_membership_record` follow the same pattern as `tests/fixtures/factories.py`: fully-valid instances with a fixed synthetic timestamp and placeholder values (`"analyst-<random>@example.test"`, `"Test Analyst"`) that tests override via keyword arguments. `DEFAULT_PASSWORD` (`"correct-horse-battery-staple"`, the canonical XKCD example password — never a real credential) is hashed once at module load and reused across fixtures to avoid paying Argon2id's deliberate per-call cost for every test that doesn't care about the plaintext.

`fake_repository.py`'s `FakeAccessControlRepository` is an in-memory, duck-typed stand-in for `AccessControlRepository` implementing the identical async method signatures, used by every unit test in `tests/unit/access_control/` and `tests/security/access_control/` so `service.py`/`sessions.py`/`dependencies.py`/the FastAPI router can be exercised without a real PostgreSQL instance. It does **not** enforce foreign-key constraints the way real PostgreSQL does — `tests/integration/access_control/` exists specifically to catch bugs that only manifest against real constraint enforcement (see `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 7, for a bug this distinction actually caught).


## Raster-Image OCR Adapter Fixtures (Phase 3 — Gaurav)

`tests/fixtures/media_processing/synthetic.py` gained `make_text_png_bytes` and related text-drawing functions to create simple, deterministic raster images with embedded text. These are used to test the `ImageOcrAdapter` against a real Tesseract engine (when available locally) and form the basis for the `FixtureOcrAdapter` tests, which simulate OCR results without requiring the system Tesseract binary. No external or real-world images are used.
`tests/integration/access_control/` writes real (synthetic) rows into a live PostgreSQL instance and always cleans them up afterward — every test that creates a user/case does so via the `cleanup_user_ids`/`cleanup_case_ids` fixtures, which `DELETE` those rows (cascading to their memberships/sessions) in teardown, so repeated runs never accumulate leftover state. It applies the Alembic migration itself, once per test session, via a genuinely separate subprocess with `tests/conftest.py`'s fake test-environment variables stripped from its environment — otherwise `migrations/env.py`'s own `get_settings()` call would read the fake `POSTGRES_DSN` `tests/conftest.py` sets for the rest of the suite instead of this suite's real `.env`. Every email used is synthetic and randomly suffixed (`unique_email()` in `conftest.py`) to avoid colliding with a previous run's leftover-but-not-yet-cleaned data.

## Audio/social/alias/communication-link fixtures (`tests/fixtures/communication_processing/`)

`builders.py` constructs everything from scratch, all synthetic: `build_wav_bytes` writes a real, valid, silent PCM WAV via the stdlib `wave` module (no audio library, no real recording); `build_fake_mp3_bytes`/`build_fake_m4a_bytes`/`build_fake_ogg_bytes` are minimal magic-byte-only stand-ins for unsupported-format routing tests; `build_whatsapp_export`/`build_telegram_export`/`build_instagram_export`/`build_generic_json_export` build the exact documented shapes each parser accepts, with invented sender names (`"Alice"`, `"Bob"`), invented message text, and invented (non-dialable, non-existent) phone-number-*shaped* strings used only to prove format-detection/exclusion logic (e.g. `9876543210` in transliteration-exclusion tests) — never a real phone number, account, or export. `factory.py`'s `make_job` builds a `WorkerJobV1` with a fixed synthetic timestamp, following the same pattern as `tests/fixtures/factories.py`.

Devanagari/Gurmukhi test fixtures (`tests/unit/communication_processing/test_aliases_transliteration.py`, `test_aliases_scripts.py`) use common, generic given names (e.g. राम "Ram", सिंह "Singh" as a surname element) purely as known-good inputs to a deterministic character-mapping algorithm — they do not refer to, and are not associated with, any real person, case, or investigation.

## Video/image fixtures (`tests/fixtures/media_processing/`)

`synthetic.py` constructs everything from scratch, all synthetic: `make_png_bytes` writes a real, valid, solid-color PNG via Pillow (no photograph, no real image); `make_solid_frame` builds a synthetic in-memory RGB `numpy` array for analysis-interface tests that need no decode step at all; `make_synthetic_mp4_bytes` shells out to `ffmpeg`'s `lavfi testsrc` pattern-generator filter (a colorful test-pattern signal, the same kind used to calibrate broadcast equipment) to produce a tiny, genuinely-decodable MP4 with no real footage involved — used only by `tests/integration/media_processing/`, which self-skips if `ffmpeg`/`ffprobe` are unavailable (`ffmpeg_available()`). `factory.py`'s `make_evidence_and_job` builds a matched `EvidenceRecordV1`/`WorkerJobV1` pair with a fixed synthetic timestamp and `SourceType.VIDEO` default, following the same pattern as `tests/fixtures/structured_processing/factory.py`.

`tests/unit/media_processing/` never depends on real `ffmpeg`/`ffprobe` binaries: `test_probe.py`/`test_frames.py` monkeypatch `subprocess.run`/`shutil.which` directly on the `video.probe`/`video.frames` modules to exercise parsing/extraction logic deterministically without spawning a real process, and `test_media_worker.py`'s video-path tests monkeypatch `probe_video`/`extract_frames` on the `worker` module itself for the same reason. Only `tests/integration/media_processing/test_video_pipeline.py` exercises the real binaries, against the synthetic MP4 above.

**Phase 2 completion (Gaurav) additions**: `tests/unit/evidence_lifecycle/test_media_routing.py` reuses `make_png_bytes` for its real-image routing cases and a literal, non-decodable placeholder (`FAKE_MP4_BYTES = b"fake-mp4-container-bytes-not-a-real-video"`) for its video routing cases — routing/upload only inspects the declared `content_type`, never the bytes themselves, so a real container is unnecessary at that layer (mirrors `test_communication_routing.py`'s identical use of arbitrary placeholder bytes for its own routing-only cases). `tests/integration/media_processing/test_media_worker_live.py` uses `make_png_bytes()` and `make_synthetic_mp4_bytes()` directly against the real running API and real worker — no new fixtures were needed.

## Evidence-lifecycle fixtures (`tests/fixtures/evidence_lifecycle/`)

`factories.py`'s `make_upload_file` wraps synthetic bytes (default: a short invented FIR-style sentence, never a real report) in a real `fastapi.UploadFile` backed by `io.BytesIO`, with a `Headers({"content-type": ...})` object so `.content_type` resolves the same way a real multipart request would. `make_evidence_record`/`make_job_record` follow the same fixed-`FIXED_TIME`, keyword-override pattern as every other module's factories.

`fake_repository.py`'s `FakeEvidenceLifecycleRepository` is an in-memory, duck-typed stand-in for `EvidenceLifecycleRepository` — including the real migration's uniqueness rules, which it enforces itself by raising `sqlalchemy.exc.IntegrityError` (never a distinct exception type), so `service.py`'s conflict/race-handling code path is exercisable without a real database. `app.modules.evidence_lifecycle.storage.FakeObjectStorage` and `.jobs.FakeJobProducer` are similarly duck-typed stand-ins with `fail_put`/`fail_delete`/`fail` toggles for exercising the storage-failure and dispatch-deferred paths deterministically.

`tests/integration/evidence_lifecycle/` writes real (synthetic) rows into a live PostgreSQL instance (a `seeded_case`/`seeded_user` fixture pair provides the real `cases`/`users` rows the module's foreign keys require) and a real private MinIO object, cleaning both up in fixture teardown (`cleanup_evidence_ids` deletes `worker_jobs` then `evidence_records`; the seeded case/user rows delete themselves). It applies the Alembic migration itself, once per session, the same subprocess-with-stripped-test-env pattern `tests/integration/access_control/conftest.py` established. Self-skips (never fabricates a pass) if there's no `.env`, or if PostgreSQL/MinIO specifically aren't reachable through it.

The FIR-style sentence used across these fixtures ("FIR No. 123/2026 filed at City Police Station...") is entirely invented for testing format-detection and provenance logic — it does not correspond to any real FIR, case, or police station.

## Structured-processing worker fixtures (Phase 2 — Jasraj)

No new fixture module: `tests/unit/structured_processing/test_worker_client.py` builds a fake internal-API response cycle inline via `httpx.MockTransport` (no real server, no `FastAPI`/`uvicorn`), reusing the root-level `tests/fixtures/factories.py::make_worker_job`/`make_worker_result` for the request/response payload shapes. `tests/unit/structured_processing/test_worker_orchestration.py` defines a small local `_FakeClient` duck-typing `WorkerApiClient`'s public surface (`claim`/`submit_result`/`close`) directly in the test file, since it exists to test orchestration sequencing, not wire format — plus a `_RaisingResolver` exercising the `InputResolutionUnavailableError` -> `DEFERRED` path deterministically. `tests/integration/structured_processing/test_worker_live.py` uses no fixture data of its own: it only calls the real `WorkerApiClient` against a live running server, and always ends in either a self-skip (no `.env`/server/secret) or an explicit `pytest.skip` naming the documented input-access gap — never a fabricated pass. Every value across all three files is synthetic, following the same pattern as every other module's tests in this repository.

## Worker-credential fixtures (Phase 2.4 — Aditya)

No real worker ever exists in this repository: every test-generated worker token is a throwaway `secrets.token_urlsafe(32)` value (or a hand-written short string for negative cases) produced fresh per test, never reused across a test run, and never written to any tracked file. `tests/unit/access_control/test_worker_credentials.py` exercises `generate_worker_token`/`hash_worker_credential`/`resolve_worker_pepper`/the CLI's `create`/`rotate`/`revoke`/`list` commands entirely in-memory against `tests/fixtures/access_control/fake_repository.py`'s `FakeAccessControlRepository`, which now also holds an in-memory `worker_credentials` dict with the same six CRUD methods (`create_worker_credential`, `get_worker_credential_by_digest`, `rotate_worker_credential`, `revoke_worker_credential`, `list_worker_credentials`, `_worker_credential_from_row`-equivalent) as the real `AccessControlRepository`, so credential lifecycle logic is testable without a live PostgreSQL instance. The test pepper value (`"test-only-pepper"`-style strings) and every synthetic worker `display_name` (`"test-worker"`, `"structured-processing-worker-1"`) are invented for these tests and never correspond to a real deployment secret.

`tests/unit/evidence_lifecycle/test_worker_identity_api.py` and `tests/unit/evidence_lifecycle/test_worker_identity_service.py` provision one or more synthetic worker credentials per test via the same fake repository (or a real one against `tests/integration/`), each scoped to an invented `allowed_processor_names` tuple (e.g. `("fir_report_text_v1",)`), to prove processor-scope enforcement and per-job ownership binding without depending on any real worker deployment.

`tests/integration/evidence_lifecycle/test_worker_identity_lifecycle_live.py` provisions two real, throwaway `WorkerCredentialRecord` rows against a live PostgreSQL instance for the duration of a single test, each keyed to a freshly generated `secrets.token_urlsafe(32)` token that is never logged, printed, or persisted anywhere outside that row's `credential_digest`; it deletes both rows (plus every case/evidence/job/result/observation/user row it created) itself in a `finally` block. `tests/integration/structured_processing/test_worker_live.py`'s `_ensure_worker_credential` helper instead idempotently provisions (by digest, a no-op if it already exists) a single persistent credential matching the developer's own configured `WORKER_TOKEN` from `.env`, mirroring exactly what the `worker_credentials create` CLI would produce — this is a standing local-dev credential, not a per-test throwaway, so it is deliberately left in place rather than cleaned up. Both files self-skip entirely (never fabricate a pass) when there's no `.env`, matching every other live test in this repository.

## Communication-processing worker fixtures (Phase 2 — Sarthak)

No new fixture module: `tests/unit/communication_processing/test_communication_worker_client.py` builds a fake internal-API response cycle inline via `httpx.MockTransport` (no real server), following the identical pattern `structured_processing`'s own client test established. `tests/unit/communication_processing/test_communication_worker_orchestration.py` defines a small local `_FakeClient`/fake resolver duck-typing `WorkerApiClient`'s/`WorkerInputResolver`'s public surface directly in the test file, plus reuses `tests/fixtures/communication_processing/{factory.py,builders.py}`'s existing `make_job`/`build_wav_bytes`/`build_whatsapp_export`/`build_generic_json_export` (all pre-existing Phase 1 fixtures — see "Audio/social/alias/communication-link fixtures" above) for realistic per-profile payloads, and adds inline JSON dicts for the two new transcript/diarization interchange payloads (invented segment text, invented `source_segment_id` values, never real transcript content). `tests/integration/communication_processing/test_communication_worker_live.py` uses no fixture data of its own beyond `build_wav_bytes`/`build_generic_json_export`: it calls the real `WorkerApiClient` against a live running server and always ends in either a self-skip or a real terminal result, never a fabricated pass — it provisions its own dedicated worker-credential token (`_COMMUNICATION_WORKER_TOKEN`, a fixed dev-only literal, never a real secret) rather than reusing `structured_processing`'s live test's token, specifically so the two suites' credential scopes never collide within one full-suite run (see `docs/architecture/phase-2-decisions.md` and `docs/qa/known-limitations.md`). Every value across all three files is synthetic, following the same pattern as every other module's tests in this repository.

The one manual, non-pytest Docker/live verification performed this phase (a literal `--once` CLI subprocess run — see `docs/qa/test-results.md`'s Phase 2 Sarthak entry) used the same `tests/fixtures/communication_processing/builders.py::build_wav_bytes` synthetic silent-WAV generator, a freshly-registered throwaway user (`comm-cli-verify-<timestamp>@example.test`), and a freshly-seeded throwaway case/membership row created directly via `AccessControlRepository` (no case-CRUD API exists yet). None of this data was cleaned up afterward — it remains in the local dev database as ordinary accumulated test data, matching the existing "long-lived shared sandbox" convention `test_communication_worker_live.py` itself documents (queued-job cleanup before each parametrized run, but no user/case teardown).

## Worker claim/result fixtures (Phase 2.1)

Worker-lifecycle tests reuse the root-level `tests/fixtures/factories.py` builders (`make_observation`, `make_worker_result`) directly rather than duplicating them — a submitted `WorkerResultV1` is exactly the same contract shape those factories already build correctly for every other module's tests, so there is no evidence-lifecycle-specific variant. `FakeEvidenceLifecycleRepository` was extended with in-memory `claim_job`/`submit_result`/etc. that mirror the real repository's concurrency and uniqueness semantics closely enough to unit-test races and idempotency without a real database (`tests/unit/evidence_lifecycle/test_worker_claim.py`, `test_worker_result.py`); `tests/integration/evidence_lifecycle/test_worker_lifecycle_live.py` proves the same behavior against a real `FOR UPDATE SKIP LOCKED` row lock and real PostgreSQL uniqueness constraints. No new synthetic evidence content was introduced beyond what Phase 2's fixtures already provide (a short invented CDR CSV row, a short invented FIR-style sentence).

## Graph-projection fixtures (Phase 2.5 — Shreshtha)

`tests/unit/graph/test_projection.py`'s `EntityMention` tests reuse `tests/fixtures/factories.py::make_observation`'s existing default `extracted_entities` (a single synthetic `ExtractedEntityMention(text="John Doe", entity_type_hint="person")`) and construct additional synthetic mentions inline (`ExtractedEntityMention(text="Jane Roe"/"Alpha Corp"/..., entity_type_hint=...)`) — no real name, organization, or extracted-entity value corresponds to a real person or case.

`tests/unit/graph/test_projector.py` fakes both `GraphProjectionOutboxRepository` and `Neo4jGraphRepository` as minimal local duck-typed doubles (`_FakeOutbox`/`_FakeGraph`), following the exact same pattern `tests/unit/graph/test_projection.py::_FakeRepository` already established — no real PostgreSQL or Neo4j involved, canned `write`/`read` results fed in call order.

`tests/integration/graph/test_outbox_repository_live.py` inserts synthetic `graph_projection_jobs` rows directly (via `_insert_job`, arbitrary `uuid4()`-generated `case_id`/`evidence_id`/`observation_id` — these tests exercise claim/lease/retry logic only, never the observation/evidence content itself, so no real `worker_observations`/`evidence_records` rows are needed) against a live PostgreSQL instance, cleaning every row up itself in a `finally` block per test.

`tests/integration/graph/test_full_pipeline_live.py` uses a tiny synthetic CSV (`name,value\nalpha,1\nbeta,2\n`, routed to `structured_tabular`/`generic_tabular_v1`) and a hand-built `WorkerResultV1`/`ObservationV1` JSON payload (one observation, one `ExtractedEntityMention(text="Alpha Corp", entity_type_hint="organisation")`) submitted directly through the real internal worker API — this test proves the graph-projection pipeline end to end, not `structured_processing`'s real CSV parsing, so the result is constructed directly rather than requiring a real worker to parse it. Provisions one throwaway worker credential the same way `test_worker_identity_lifecycle_live.py` does; cleans up the worker credential row, every PostgreSQL row it created, and the real Neo4j nodes projected under its synthetic case ID (`DETACH DELETE`), plus a defensive sweep of any `graph_projection_jobs` row orphaned by an unrelated live test's own (independent) cleanup — see that test's own docstring for why the sweep exists.

## Communication-processing routing fixtures (Phase 2 routing fix — Shreshtha)

`tests/unit/evidence_lifecycle/test_communication_routing.py` uses a minimal synthetic JSON payload (`{"segments": []}`/`{"messages": []}`) for the `audio_transcript`/`audio_diarization`/`telegram_chat`/`instagram_chat` routing tests (these tests only prove *routing* — which processor a job is assigned to — not real parsing, so an empty-but-valid JSON shape is sufficient) and a one-line synthetic WhatsApp-style message (`"01/01/26, 12:00 - Alice: hello"`, an invented sender name and message) for `whatsapp_chat`. Mirrors `test_structured_routing.py`'s exact fixture style.

## Real local detector/OCR/tracker fixtures (Phase 2 closeout — Nipun)

**Model asset**: `tests/unit/media_processing/test_onnx_detector.py`'s real-detection scenarios run against whatever file is at `MEDIA_DETECTOR_MODEL_PATH` (or its `Settings` default) — this suite never downloads it itself (mirroring `ffmpeg_available()`'s self-skip convention in `tests/fixtures/media_processing/synthetic.py`); it self-skips those scenarios entirely when no such file is present. The model itself, once bootstrapped via `uv run python -m app.modules.media_processing.bootstrap_models`, is a real, third-party, Apache-2.0-licensed asset (YOLOX-s, `github.com/opencv/opencv_zoo`) — not something this repository authored or can vouch for the training data of, but its own license and public source are documented in full in `analysis/onnx_detector.py`'s module docstring and `docs/architecture/media-processing-worker.md`. The committed synthetic frame this suite runs the real detector against (`tests/fixtures/media_processing/synthetic.py::make_solid_frame`, a solid-color array) is not expected to trigger any real COCO-class detection — the test proves the real pipeline runs correctly end to end (letterbox → onnxruntime → decode → NMS → unletterbox), not detection accuracy against real photographic content, which this repository's synthetic-only fixture policy cannot exercise (see "No real evidence" above).

**OCR test fixtures**: `tests/unit/media_processing/test_tesseract_ocr.py` renders invented text (`"EXIT 42B"`, `"HELLO"`, `"VISIBLE TEXT"`) onto a blank in-memory image via Pillow, using whichever local system TrueType font it finds first from a short candidate list (`NotoSans-Bold.ttf`/`DejaVuSans-Bold.ttf`/`LiberationSans-Bold.ttf`) — none of these strings, or the font file itself, is committed to this repository; the suite self-skips its real-OCR-quality scenarios entirely if no suitable font is found locally (the real `tesseract` binary genuinely cannot read the tiny bitmap font Pillow falls back to without one). The always-run failure-mode tests (missing binary, missing language pack) need no rendered text at all.

**Tracker fixtures**: `tests/unit/media_processing/test_iou_tracker.py` constructs `ObjectDetection`s directly with small, arbitrary, invented pixel coordinates (e.g. `(10, 10, 30, 30)`) and generic labels (`"person"`, `"vehicle"`) — no real image or video content involved at all, since `IoUTracker.track()` operates purely on already-produced detection boxes.

**Loop/heartbeat fixtures**: `tests/unit/media_processing/test_media_worker_loop.py` and `tests/unit/graph/test_worker_loop.py` both use small, arbitrary, invented poll/backoff intervals (`0.01`–`0.05` seconds) so the full suite of shutdown/backoff/reset scenarios runs in well under a second — never a real wall-clock wait, and no real PostgreSQL/Neo4j/HTTP connection for the graph loop tests specifically (`run_batch` is monkeypatched; `create_engine`/`create_driver` only build lazy client objects that don't connect until first real use, so even the *real* `Settings`-derived DSN/URI values from this developer's own `.env` are only ever used to construct, never to open, a connection in these tests).

## Observation-batch/transformation-provenance/progress fixtures (Phase 3 — Nipun)

`tests/fixtures/factories.py` gained three new builders following the existing fixed-`FIXED_TIME`, keyword-override pattern exactly: `make_batch_progress` (a valid `ObservationBatchProgressV1`, default `stage="parsing"`/`message_code="PAGE_PARSED"`), `make_transformation_provenance` (a valid `TransformationProvenanceV1`, default `step_name="pdf_text_extraction"`, reusing `make_source_locator` for its `input_locator`), and `make_observation_batch_submission` (a valid `ObservationBatchSubmissionV1` wrapping one default `make_observation()`, with `progress.batch_sequence` defaulted to match the submission's own `batch_sequence` — the contract requires the two to agree, so tests overriding one must override both together). No invented text beyond what the existing `make_observation`/`make_source_locator` builders already contribute.

`tests/fixtures/evidence_lifecycle/fake_repository.py`'s `FakeEvidenceLifecycleRepository` gained in-memory `observation_batches`/`transformations`/`progress_events` dicts and a `submit_observation_batch` method mirroring the real repository's three unique-constraint checks (`(job_id, batch_id)`, `(job_id, idempotency_key)`, `observation_id` reuse), each raising `sqlalchemy.exc.IntegrityError` exactly like every other conflict path this fixture already simulates — so `service.py`'s replay/conflict-resolution logic is exercisable without a real database.

`tests/integration/evidence_lifecycle/test_observation_batch_live.py` builds its `ObservationBatchSubmissionV1`/`TransformationProvenanceV1` payloads as raw JSON dicts (mirroring `tests/integration/graph/test_full_pipeline_live.py`'s own `_observation_payload`/`_succeeded_result_payload` style) rather than importing the Pydantic contract classes directly, since it exercises the real HTTP wire format end to end. Evidence content is a short invented FIR-style sentence (`"FIR No. 1/2026 -- synthetic test content"`), and observation text/locators (`"page one text"`/`"page two text"`) are invented purely to exercise two distinct micro-batches over the same evidence item — none of it corresponds to a real report. Every case/user/worker-credential row this test creates is cleaned up in a `finally` block, including the projected Neo4j nodes under its synthetic `case_id` (`DETACH DELETE`), following the exact same pattern `test_full_pipeline_live.py` already established.

## Worker retry-limit/lease-ceiling/audit fixtures (Phase 3 — Aditya)

No new fixture module was needed: every new unit test reuses the existing `tests/fixtures/factories.py::make_worker_job`/`make_worker_result`/`make_observation` and `tests/fixtures/evidence_lifecycle/{factories.py,fake_repository.py}` builders exactly as prior phases' worker-lifecycle tests already did (see "Worker claim/result fixtures (Phase 2.1)" above) — retry-exhaustion, lease-ceiling, and audit-event tests are all expressed as new *scenarios* over these same builders, overriding `max_attempts`/`claimed_at`/`lease_expires_at` via keyword arguments where a specific timing edge case is being proven. `FakeEvidenceLifecycleRepository` gained an in-memory `get_retry_exhausted_jobs` method and a `max_lease_seconds`-aware `renew_lease` mirroring the real repository's new logic, so these scenarios are exercisable without a real database.

`tests/integration/evidence_lifecycle/test_worker_retry_and_lease_live.py` drives `EvidenceLifecycleService` directly (not raw HTTP) against a live PostgreSQL instance, using a controllable `UploadContext.now` callable to advance simulated time deterministically (no real wall-clock sleep) for the lease-ceiling and retry-exhaustion scenarios, plus a genuinely concurrent `asyncio.gather` terminal-result race using real time. It reuses the same `seeded_case`/`seeded_user` fixture pair and worker-credential provisioning pattern every other live evidence-lifecycle test already establishes, and cleans up every row it creates in a `finally` block. Every value across all of this phase's new tests is synthetic, following the same pattern as every other module's tests in this repository.

## Document/OCR/NER/relations and CDR/finance chunked-batch fixtures (Phase 3 — Jasraj)

**PDF fixtures** (`tests/fixtures/structured_processing/builders.py`): `build_scanned_pdf_page`/`build_mixed_pdf` (new) hand-assemble a real, valid single-page or multi-page PDF whose "scanned" pages are a real image XObject stream (raw, uncompressed `/DeviceRGB` samples — no filter, a legal PDF construct) containing Pillow-rendered, invented text lines (e.g. `"FIR No: 91/2026"`, `"Police Station: Colaba"`) — never a real scan or a real report. This follows `build_minimal_pdf`'s exact "hand-assemble the PDF byte stream directly, no `reportlab`" precedent, extended only to add an image XObject alongside the existing text-stream page type; the same module's font-lookup helper (`_load_font`) mirrors `tests/unit/media_processing/test_tesseract_ocr.py`'s existing "try a short list of common system TrueType fonts, fall back to Pillow's bitmap default" pattern. A page built this way is genuinely OCR-able (verified: real Tesseract recovers the rendered text) while `pypdf.extract_text()` on it always returns empty — a real scanned-document fixture, not a simulated one.

**Labelled OCR precision fixture** (`tests/unit/structured_processing/test_ocr_field_match_precision.py`): a single, small, hand-picked set of known-in-advance identifiers (an invented FIR number, an invented Indian-mobile-shaped phone number, an invented amount) rendered via `build_scanned_pdf_page` — used only to compute an honest, reproducible precision/recall measurement against real OCR + real regex extraction; see `docs/qa/test-results.md` for the actual measured values from a real run in this environment. None of the values correspond to a real report, phone number, or transaction.

**NER fixtures**: no separate fixture module — `tests/unit/structured_processing/test_ner.py` uses short, inline, invented sentences (`"Ramesh Kumar met Suresh Sharma at HDFC Bank in Mumbai..."`) directly, following the same "short invented sentence inline" convention `test_fir_report.py` already established. The real local NER model asset (`en_core_web_sm` 3.8.0, MIT-licensed, a genuine third-party asset this repository does not author or vouch for the training data of — see `docs/architecture/document-structured-processing.md`'s "Model provenance") is bootstrapped via `uv run python -m app.modules.structured_processing.bootstrap_ner_model` into the git-ignored `models/nlp/` directory, exactly mirroring `media_processing`'s existing real-detector-model bootstrap/gitignore convention; `test_ner.py`'s real-model test self-skips (never fabricates a pass) if that directory isn't present.

**Relation/worker-batch fixtures**: `tests/unit/structured_processing/test_relations.py` builds `RawMention`s directly with small, arbitrary, invented page/span coordinates and invented text (`"Ramesh Kumar"`, `"9876543210"`, `"Mumbai"`) — no fixture module needed, following the same pattern `test_provenance.py` already used for `RawMention`-level tests. `tests/unit/structured_processing/{test_worker_document_batches,test_worker_structured_batches}.py` reuse `build_mixed_pdf`/`build_scanned_pdf_page` for documents and small inline invented CSV/XLSX/JSON bytes for CDR/finance (mirroring `test_cdr.py`/`test_finance.py`'s own inline-bytes convention), with a small local `_RecordingClient` duck-typing `WorkerApiClient`'s `submit_batch`/`renew_lease` surface — the same "local fake client, no real server" pattern `test_worker_orchestration.py` already established for `claim`/`submit_result`.

**Chunked-processing fixtures**: `tests/unit/structured_processing/test_chunked_processing.py` builds small, arbitrary in-memory CSV/XLSX byte strings (via `openpyxl` directly for XLSX, matching `tests/fixtures/structured_processing/builders.py::build_xlsx`'s own construction style) with a deliberately-included malformed row (a blank required field) — no real CDR/finance export data anywhere.

**Live pipeline extension**: `tests/integration/structured_processing/test_worker_live.py`'s existing `_PIPELINE_CASES` parametrized list gained two new entries (`cdr`/`cdr_generic_v1`, `financial`/`financial_transaction_generic_v1`) reusing 100% of that test's existing live-upload/claim/cleanup infrastructure — small, invented CSV bodies (two CDR rows, two finance rows), no new fixture code needed.

## Micro-batch, timezone-default, mentioned-identifier, transliteration, and ASR/diarization-adapter fixtures (Phase 3 — Sarthak)

**Micro-batch orchestration fixtures**: `tests/unit/communication_processing/test_communication_worker_orchestration.py`'s existing `_FakeClient` (Phase 2) gained `submit_batch`/`renew_lease` methods, mirroring `structured_processing`'s identical `_RecordingClient` extension one phase earlier in this same session — no new fixture module needed, reusing the same `make_job`/`build_whatsapp_export`/etc. builders already in `tests/fixtures/communication_processing/`.

**Mentioned-identifier fixtures**: `tests/unit/communication_processing/test_social_identifiers.py` constructs `ChatMessageRecord`s directly with small, invented message text containing an Indian-mobile-shaped phone number (`9876543210`), an invented email (`alice@example.com`), an invented URL (`http://example.com/x`), and invented `@handle` tokens — no real phone number, email, or website.

**Transliteration-wiring fixtures**: `tests/unit/communication_processing/test_social_common.py` reuses the exact same known-good Devanagari given-name convention `test_aliases_transliteration.py` already established (राहुल "Rahul", शर्मा "Sharma" as a surname element) — common, generic names used purely as known-good inputs to the deterministic character-mapping algorithm, not associated with any real person.

**ASR/diarization adapter fixtures** (`tests/fixtures/communication_processing/{asr,diarization}_fixture_adapter.py`, new): `AsrFixtureAdapter`/`DiarizationFixtureAdapter` are explicit, test-only `Protocol` implementations that return exactly the (invented, inline-constructed) `TranscriptSegmentInput`/`DiarizationSegmentInput` values they were given — obvious fixture markers (`model_name="fixture_asr_adapter"`/`"fixture_diarization_adapter"`) make a result from either impossible to mistake for a genuine local-model result in a log, test report, or provenance record. Neither is imported by anything under `app/`.

**Multi-batch/provenance test fixtures** (`tests/unit/communication_processing/test_communication_worker_batches.py`, `test_communication_provenance.py`, new files): a small local `_RecordingClient` duck-typing `WorkerApiClient`'s `submit_batch`/`renew_lease` surface, mirroring `structured_processing`'s identical `_RecordingClient` one phase earlier in this same session; a small synthetic WhatsApp export of 12 invented messages (`"message number {i}"`) generated inline to force a multi-batch path deterministically at a monkeypatched, artificially small `communication_batch_size`. No fixture module needed for either.

## Phase 5 Shreshtha graph-intelligence fixtures

Focused tests use inline synthetic UUIDs, invented phone-shaped identifiers, generic Devanagari/Roman aliases,
and bounded timestamps. The projection test uses one synthetic extractor and source locator with a fake graph
repository. No Operation Nightfall, reference master, private truth, source evidence, or runtime fixture is read.

**Phase 5B document/CDR/finance validation fixtures (Jasraj):**
`tests/unit/structured_processing/test_phase5_signal_validation.py` uses only
short opaque values such as `caller-A`, `sender-A`, `TXN-001`, synthetic
phone-shaped values, a one-line synthetic OCR region, and generated UUIDs.
CSV, JSON, and generated XLSX rows are in-memory only. The one narration-like
reference is intentionally synthetic and asserts exclusion from graph-facing
attributes; no real document, transcript, CDR, account, evidence, or case data
is present.

**Phase 5A reconciliation fixtures** (new): `tests/unit/graph/test_intelligence_sourcing.py`/
`test_intelligence_pipeline.py` build real-shaped `ObservationV1`s inline (invented phone/email/vehicle/UPI
values, invented Devanagari sender names "राहुल शर्मा" and their `raahula`/`sharmaa` transliteration
candidates, invented CDR caller/callee numbers and financial account strings) mirroring Jasraj's/Sarthak's
actual canonical observation shapes -- never real evidence. `tests/unit/graph/test_intelligence_vector_store.py`
uses small hand-written fake `AsyncEngine`/connection classes (no real database) to exercise the
`VectorSearchUnavailable` path deterministically. `tests/integration/graph/test_intelligence_vector_store_live.py`
and `test_intelligence_pipeline_live.py` insert a full synthetic case/user/evidence/job/result/observation
chain directly into live PostgreSQL (reusing `test_phase5_correlation_integration_live.py`'s
`_insert_observation` helper, extended to accept an explicit observation type/attributes) and a synthetic
Evidence/Observation pair into live Neo4j, all deleted in each test's own cleanup -- no pre-existing user/case
data is ever touched, matching every other live test in this package's convention.

# Phase 4 media orchestration fixtures

The Phase 4 coordinator tests use synthetic UUIDs, opaque `s3://` references,
short time boundaries, and synthetic canonical observations. They contain no
audio/video/image bytes, object-store credentials, biometric material, or
real-case provenance.

# Phase 4 LAN worker-security fixtures

Transport tests use in-memory ASGI apps, localhost/proxy-shaped synthetic
addresses, and bounded placeholder bytes only. They contain no bearer token,
claim token, evidence, object URI, credential, or real network endpoint.

# Phase 4 media graph-mapping fixtures

`tests/unit/graph/test_phase_4_media_mapping.py` uses only synthetic UUIDs,
short source-relative millisecond ranges, a normalized synthetic bbox, bounded
handles/speaker labels, and a short synthetic transcript string used solely to
assert that it is excluded from graph properties. No raw export, credential,
artifact URI, real audio/video/image, or real-case data is used.
# Phase 4 extracted-text utility fixtures (Jasraj)

`tests/unit/extracted_text/test_ocr_postprocessing.py` uses only short
synthetic strings, generated UUIDs, normalized test boxes, and frame-relative
locators. It contains no real OCR output, media bytes, credentials, object
URIs, case material, or production identifiers.

# Phase 4 video/image planning fixtures (Gaurav)

Planning tests use synthetic metadata, generated UUIDs, and tiny in-memory
arrays only. No video, image, model bundle, OCR text, URI, credential, or case
evidence is included.

Phase 5B visual-validation tests use only synthetic frame numbers, duration,
chunk boundaries, normalized boxes, and short bounded OCR labels. They contain
no frame crops, sensitive video, plate ownership data, biometric data, or real
case evidence.
# Phase 4 audio/social planning fixtures (Sarthak)

Tests use generated UUIDs, synthetic source-relative intervals, short Unicode
strings, and a tiny in-memory constant-amplitude WAV fixture. Fixture ASR text
is short and synthetic; no real transcript, chat export, credential, object
URI, model asset, or case evidence is included.

Phase 5B communication-validation fixtures add only short synthetic timestamps,
message IDs, platform names, local speaker labels, and placeholder transcript
or message text used to prove SHA-256/length-only graph attributes. They contain
no real account owner, phone subscriber, chat participant, audio, or identity
claim.

# Phase 5 final integration — rules-baseline benchmark (Nipun)

`tests/fixtures/graph/phase5_rules_benchmark.py` (`phase5_rules_benchmark_v1`)
is a versioned, invented, non-sensitive synthetic case: nine canonical
observations (two-party CDR pair, two-party finance pair, a cross-modal
phone match, two transliteration-linked chat messages, two unrelated
negatives) with hand-labeled ground-truth positive pairs, plus a separate
direct-construction contradiction probe and cross-case probe. Every phone
number, account identifier, and name is fabricated for this fixture only.
No private police data, real case data, or Operation Nightfall data was
used, referenced, or approximated. See
`docs/decisions/ADR-006-phase-5-rules-baseline.md`'s "Measurement and
freeze" addendum for the experiment this fixture supports.

# Phase 6 Part 1 — integrity foundation fixtures (Nipun)

All integrity tests use synthetic, generated data only:

- **Integrity events** (`tests/unit/integrity/`, `tests/integration/integrity/`):
  generated `uuid4()` case/event IDs, fixed placeholder hashes
  (`"a" * 64`, etc. — never a real SHA-256 of real content), and
  `canonical_metadata` dicts containing only short synthetic strings/counts
  (e.g. `{"sha256": "a" * 64, "note": "key-1"}`). No real evidence content,
  filename, transcript, chat body, or OCR text appears anywhere in this
  fixture set — several tests (`test_models.py`) exist specifically to
  prove such content is *rejected* if a producer tried to pass it.
- **Signing keys**: every test generates its own fresh, random,
  throwaway Ed25519 key via `signing.generate_signing_key_b64()` —
  never the operator's real `.env` `INTEGRITY_SIGNING_KEY`, and never a
  hardcoded key committed to the repository. Live integration tests
  (`tests/integration/integrity/conftest.py`) explicitly override any
  key present in a local `.env` with a freshly generated one, so these
  tests never depend on (or risk logging) a real deployment key.
- **Merkle/checkpoint fixtures**: small in-memory sequences of 3-8
  synthetic events built via each test file's own `_event`/`_submission`
  helper — no real case data, and every value is either a random UUID, a
  fixed placeholder hash, or a small integer.

No Operation Nightfall data, real police case data, or production
credentials were used, referenced, or approximated anywhere in this
module's tests.

# Phase 6 Part 3 — structured provenance fixtures (Jasraj)

Structured-provenance unit tests use generated UUIDs, placeholder hashes, and
synthetic document labels, telephone-like values, account-like values, and
transaction-like values only. Assertions prove those values appear only as
commitment inputs, never in the safe projection, integrity event, checkpoint
bundle, log assertion, or response shape.

# Phase 6 Part 4 — visual and communication provenance fixtures

Part 4 uses generated UUIDs, placeholder hashes, synthetic frame/time values,
normalized boxes, and invented local track/speaker/handle strings. Assertions
prove these are commitment inputs only. No real media/audio, OCR/ASR/chat
export, plate/face data, object URI, credential, signing key, or case material
is used.

# Phase 6 Part 5 — review and hypothesis workflow fixtures (Shreshtha)

Unit tests (`tests/unit/graph/test_review_models.py`,
`test_hypothesis_models.py`, `test_review_projection.py`,
`test_review_service.py`) use generated `uuid4()` case/candidate/hypothesis
IDs, fixed placeholder rationale/statement strings ("a plausible reviewer
rationale", "a synthetic hypothesis statement", "the raw human-authored
statement text") chosen specifically to be recognizable in an assertion that
they never leak into a commitment/projection/log, and hand-written fake
repository/graph classes (mirroring `test_intelligence_projection.py`'s
`_Graph` pattern) rather than a real database or Neo4j connection.

The live end-to-end test
(`tests/integration/graph/test_review_and_hypothesis_live.py`) registers
three throwaway synthetic users (`phase6-investigator-<random>@example.test`,
`-reviewer-`, `-outsider-`) via the real registration API with a fixed
non-production password, builds two synthetic `phone_number_mention`
observations sharing one placeholder phone number (`9876543210`, the same
constant `test_intelligence_pipeline_live.py` already uses), and one
synthetic hypothesis statement/rationale pair describing a plausible but
entirely fictitious shared-operator inference. No real police case data, real
personal data, or Operation Nightfall material is used. Because
`candidate_review_decisions` and `hypothesis_actions` are genuinely
append-only (by design — see `docs/architecture/phase-6-review-and-
hypothesis.md`), this test's cleanup deliberately does not attempt to delete
those rows; it relies on an unguessable, never-reused `case_id`, the same
documented precedent `tests/integration/integrity/conftest.py` already
established for `integrity_events`.

# Phase 7 Part 1 — evaluation foundation fixtures (Nipun)

All `tests/unit/evaluation/` tests use synthetic, non-sensitive data only,
and the four frozen `configs/benchmarks/*.v1.json` files themselves
contain no real dataset content, no real model weight, and no real
benchmark measurement:

- **Dataset manifest fixtures**: `test_models.py`'s `_entry()` helper
  builds a minimal `DatasetManifestEntryV1` from short placeholder strings
  (`"Example Dataset"`, `"example task"`); `test_dataset_manifest.py`
  loads the real, committed `dataset-manifest.v1.json` and asserts against
  it directly, since that file is itself entirely safe metadata (dataset
  IDs, roles, licence-verification status, short limitation notes) with
  no raw dataset content, license text reproduction, or downloaded file of
  any kind.
- **Model candidate fixtures**: `test_catalog.py`'s `_candidate()` helper
  and the real, committed `model-candidates.v1.json` describe only
  framework/model-family/variant *names* (e.g. `"PaddleOCR"`,
  `"PP-OCRv5"`, `"mobile-lightweight"`) and licence-verification status --
  no model weight, checksum, or benchmark number, since none was
  downloaded or measured in this phase.
- **Benchmark-run fixtures**: `test_results.py`'s `_run()` helper builds a
  synthetic `BenchmarkRunV1` with placeholder hashes (`"cfg-" + "b" * 16`,
  a fixed 64-character hex string for `artifact_sha256`) and small
  numeric metric values (`{"latency_ms": 42.0}`) -- never a value derived
  from an actual model run, since none occurred.
- **Synthetic case-plan fixtures**: `test_splits.py`'s `_plan()` helper and
  the real, committed `synthetic-case-plan.v1.json` use only placeholder
  case IDs (`"synth-case-dev-01"`, etc.) -- no actual case content,
  evidence, or observation exists behind any of these IDs yet; Part 1
  defines the plan/contract only.

No Operation Nightfall data, real police case data, real dataset content,
real model weight, or production credential was used, referenced, or
approximated anywhere in this module's tests or configs.

## Phase 7 Part 2 — structured-data and local OCR benchmarking fixtures (Jasraj)

No real FIR ICDAR 2023, GoMask Voice CDR, or IBM AMLSim data is committed or
copied into this repository's tests. The unit and integration test fixtures
remain synthetic and safe:

- **OCR fixtures**: `tests/unit/structured_processing/test_benchmark_adapters.py`
  builds a small, invented FIR-style text fixture (`_FIR_TEXT`, e.g.
  `"FIR No. TEST/2026/001"`/`"Police Station: Test PS"`/`"Phone:
  9876543210"`, newline-separated to avoid the real extractor's greedy
  `police_station` regex) and drives it through the real, unmodified
  `document.fir_report.extract_fir_mentions` -- the same function
  production OCR-derived text is fed through -- to compute an honest
  field-extraction F1. `FakeOcrEngine` never touches a real image; it
  returns a fixed string, or raises a categorized `OcrEngineError` when
  handed one of three reserved marker byte-strings
  (`FakeOcrEngine.UNREADABLE`/`UNSUPPORTED_FORMAT`/`EXECUTION_FAILURE`) to
  simulate a per-document failure deterministically.
- **CDR/finance fixtures**: small, inline, invented CSV rows (a synthetic
  Indian-mobile-shaped caller/callee pair, a synthetic account/amount
  pair) mirroring `tests/unit/structured_processing/test_cdr.py`'s/
  `test_finance.py`'s own inline-bytes convention -- run through the real,
  unmodified `structured.chunked_processing.assess_schema`/
  `normalize_chunk`, the same seam production batch processing uses.
- **CLI/safety fixtures**: `tests/unit/structured_processing/
  test_benchmark_cli.py`/`test_benchmark_safety.py` point every
  `TRACEX_BENCHMARK_*` environment variable at a pytest `tmp_path`
  directory -- never a developer's real environment -- and use the same
  small invented CDR/finance CSV rows above to prove a real local run
  produces a genuinely safe result file.
- **Integration smoke fixtures**: `tests/integration/structured_processing/
  test_local_benchmark_smoke.py` self-skips (never fabricates a pass)
  unless `TRACEX_BENCHMARK_DATA_ROOT` is actually set and the relevant
  dataset directory actually exists and is non-empty -- the expected state
  on this development machine, since no real dataset was downloaded here.

No real FIR page, CDR row, phone number, financial transaction, model
weight, or host-local artifact path appears anywhere in this module's tests.

## 2026-09-20 — Gate B external benchmark data (Jasraj)

Gate B kept all source checkouts, derived inputs, model packages, caches,
and result JSON files outside Git under
`$HOME/tracex-gateb-artifacts/jasraj`. The repository contains only safe
provenance and aggregate measurements.

- **IBM AMLSim:** official source `https://github.com/IBM/AMLSim`, commit
  `7338a4bcb1af9bcfea2201ad7daccfe2a4d569ca`, Apache-2.0. The selected
  repository sample `sample/20K_fanin200.tgz` has SHA-256
  `e2479ab7a1ecfcd369a7738c855364825561bf9d888d2ad53174e0ed10fd1f1b`.
  Its extracted `transactions.csv` has SHA-256
  `e56ed0df72cdf4f4b872cb0493800f72fd98e802627515a2484098eee998f36a`,
  header `sourceNodeId,targetNodeId,value,time`, and 118,250 data rows.
- **FIR Dataset ICDAR 2023:** official source
  `https://github.com/LegalDocumentProcessing/FIR_Dataset_ICDAR2023`, commit
  `cbaceec3b584e8f3ab7c6ab96e5b68eeef8a2235`. Its README includes the MIT
  permission grant. `FIR_details.json` has SHA-256
  `693a1e11bd116e2458898e43c48785853b6d6e99439f315b761063c0bb2a357a`
  and contains 2,447 annotated regions over 544 referenced images. Gate B
  produced 2,447 annotation-box PNG crops and a local
  `benchmark_manifest.jsonl` with SHA-256
  `bcad272548b14f1be0ea9cd870b08139cf14b5cc53568a8b45b514813f55baf0`.
  Each record maps a crop to the annotation's real transcription and source
  metadata. It deliberately omits `expected_fields`, because the source
  supplies value text rather than this project's label-bearing field map.
- **GoMask Voice CDR:** the official marketplace entry is
  `https://gomask.ai/marketplace/datasets/voice-call-detail-records-cdr-circuit-switched`,
  advertised as 501 rows by 21 columns and last updated 2026-01-06. The
  download requires an account and credits. The official terms
  (`https://gomask.ai/terms`, effective 2025-08-20) and EULA
  (`https://gomask.ai/eula`) make the applicable use rights depend on the
  account plan; the Free Plan is limited to evaluation. No file was
  downloaded, hashed, or replaced with a different dataset.
- **PaddleOCR:** the four official paddle3.0.0 inference archives and their
  extracted model directories remain under the external model cache. Their
  official source URLs are
  `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_infer.tar`,
  `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_rec_infer.tar`,
  `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_det_infer.tar`,
  and
  `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_rec_infer.tar`.
  Archive SHA-256 values are mobile detector
  `50446e5d01ac2a73d5319c89513281f6578414c888c602f9af13f93feefffc58`,
  mobile recognizer
  `566b9512b34e34a9f0db54d87b51fa5a0b9ed2cf1ab7e49728cc0b8b5a64f414`,
  server detector
  `22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d`,
  and server recognizer
  `d99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a`.
  The corresponding `inference.pdiparams` SHA-256 values are
  `afa1820cb16c1fd0dad589d0f8b389139061c1ef6d68019685fd07be997dda5b`,
  `2460da90875937c94db97eba74ae3d9e5d4c4c57c42f1f41531c09a26bcc771a`,
  `183146fe9d9910352f68482f623bcbbb9fa7b9e8fa1463b9ad288cef00524d2d`,
  and `63853f062a5f4089befc16f565a68277618e0da5cb45468b49d11079de0ada77`
  in the same order. The official model documentation is
  `https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md`;
  PaddleOCR is Apache-2.0.

All source access, checkout, and downloads in this Gate B record occurred on
2026-09-20 IST. Git archive SHA-256 values for the exact AMLSim and FIR
checkouts are respectively
`3fa95d6f548631a84d6f3d9f23aca4c1865ee130b5d74ca858dabe6ee2e99bbf`
and `5040563c22ae8d3a61dd905768d859cb6c3b8ba087f9296491382927d39a788e`.

## Phase 7 Part 3 — visual benchmark foundation fixtures (Gaurav)

No real VIRAT Ground, UFPR-ALPR, or Safe/Unsafe Behaviour data was used
in this module's own unit tests -- every unit-test fixture below remains
synthetic even after Gate B (see the dated Gate B section further down
for the separate, real external data used only outside the committed
test suite):

- **Detection/tracking fixtures**: `tests/unit/media_processing/
  test_visual_benchmark_metrics.py`/`test_visual_benchmark_adapters.py`
  build small, invented `ObjectDetection`/`TrackSegment`/`GroundTruthBox`/
  `GroundTruthTrack` values directly (arbitrary pixel coordinates like
  `(10, 10, 50, 50)`, generic labels like `"person"`) -- no real image,
  video frame, or surveillance footage is decoded anywhere in these
  tests. Frames needed at all (for `FakeDetectorEngine`/
  `FakeVisualTextEngine`, which key their failure-injection markers off a
  frame's shape) reuse the existing, pre-established
  `tests/fixtures/media_processing/synthetic.py::make_solid_frame` --
  a synthetic, solid-color, in-memory numpy array, not a real photograph.
- **Visual-text/plate-OCR fixtures**: a small, invented plate-like string
  (`"ABC1234"`) is used purely to prove `character_error_rate`/
  `field_extraction_prf` compute correctly -- it does not correspond to
  any real vehicle, plate, or person.
- **Safety/CLI fixtures**: `tests/unit/media_processing/
  test_visual_benchmark_safety.py`/`test_visual_benchmark_cli.py` build
  synthetic `DatasetManifestEntryV1`/`ModelCandidateV1` records directly
  to exercise the licence-clearance gate against both a pending and a
  cleared status; two of these tests use the real, now-cleared
  `virat_ground`/`yolo11n`/`yolo11s`/`bytetrack` catalogue entries
  directly (proving the real Gate B licence change), while the "still
  blocked" tests use `safe_unsafe_behaviour`/`ufpr_alpr`, both still
  genuinely `pending_verification`. Every `TRACEX_BENCHMARK_*`
  environment variable in these unit tests still points at a pytest
  `tmp_path` directory -- never a developer's real environment.
- **Integration smoke fixtures**: `tests/integration/media_processing/
  test_visual_benchmark_smoke.py` self-skips (never fabricates a pass)
  unless `TRACEX_BENCHMARK_DATA_ROOT` is actually set and the relevant
  dataset directory actually exists and is non-empty. Run against the
  real Gate B `$HOME/tracex-gateb-artifacts/gaurav/data` root during this
  task's own verification, the detection and tracking smoke tests
  genuinely passed against the real `virat_ground` clip described below;
  the visual-text smoke test still self-skips, since no `ufpr_alpr` data
  exists locally (deferred -- see below).

No real surveillance footage, vehicle plate, face, person-identifying
payload, model weight, or MacBook-local path appears anywhere in this
module's *committed* tests.

## 2026-09-20 — Gate B external benchmark data (Gaurav)

Gate B kept all source checkouts, derived crops/frames, model weights,
caches, and result JSON files outside Git under
`$HOME/tracex-gateb-artifacts/gaurav`. The repository contains only safe
provenance and aggregate measurements (in `configs/benchmarks/` and
below).

- **VIRAT Ground**: official Kitware Data mirror, "VIRAT Video Dataset
  Release 2.0" collection
  (`https://data.kitware.com/#collection/56f56db28d777f753209ba9f/folder/56f57e748d777f753209bed6`,
  confirmed `public: true` via the Girder API), governed by the VIRAT
  Video Dataset Usage Agreement
  (`https://viratdata.org/resources/VIRAT-Video-Data-Set-Protection-Agreement-1-4-11.pdf`,
  read directly -- a genuine click-through "I Agree" protection agreement
  requiring every individual with access to sign it; access for this run
  was confirmed already accepted by the project owner before any
  download). Selected exactly one small clip from the `videos_original`
  folder: `VIRAT_S_000201_03_000640_000672.mp4` (item
  `56f585f08d777f753209ca77`, 32,878,685 bytes / ~31.4 MiB), SHA-256
  `1821ca07735092f13238418ef459759a8a2dcb962d6a59c4e74a3c2434564668`.
  Its matching official annotation file from the `annotations` folder,
  `VIRAT_S_000201_03_000640_000672.viratdata.objects.txt` (item
  `56f57ea18d777f753209bf6c`), SHA-256
  `61d29be09037d16bfba41be53ee84361c1882367cd2383204945470396d0bf38`,
  contains real per-frame bounding boxes for two objects across the
  clip's full 943 frames (one stationary car throughout, one person
  appearing from frame ~600 onward). 19 frames were sampled at a fixed
  stride (every 50th frame, 0 through 900) and decoded via `ffmpeg`; a
  local `benchmark_manifest.jsonl`/`tracking_manifest.jsonl` pair was
  built directly from the real annotation values (VIRAT's own
  `left, top, width, height` box format converted to this harness's
  `x1, y1, x2, y2`) -- no synthetic or invented ground truth.
- **Ultralytics YOLO11n/YOLO11s**: official Ultralytics assets release
  `v8.4.0` (confirmed via the installed `ultralytics==8.4.156` package's
  own `get_github_assets()` call, not assumed), downloaded directly from
  `https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt`
  (5,613,764 bytes, SHA-256
  `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1`) and
  `.../yolo11s.pt` (19,313,732 bytes, SHA-256
  `85a76fe86dd8afe384648546b56a7a78580c7cb7b404fc595f97969322d502d5`).
  The `ultralytics` package itself is AGPL-3.0, confirmed by reading
  `https://raw.githubusercontent.com/ultralytics/ultralytics/main/LICENSE`
  directly.
- **ByteTrack**: no separate download -- the real, executed code path is
  Ultralytics' own bundled `ultralytics.trackers.byte_tracker.BYTETracker`
  (the same class `model.track(..., tracker='bytetrack.yaml')` uses
  internally), governed by the same `ultralytics` package's AGPL-3.0
  licence, not the separately-licensed upstream ifzhang/ByteTrack MIT
  repository.
- **UFPR-ALPR**: deferred/unavailable. UFPR-ALPR requires a formal
  academic access-request process before any download; no evidence of
  prior access exists on this machine, and per this task's own rule
  ("do not attempt to bypass the academic-request process"), no file was
  downloaded and no substitute dataset was used.
- **Safe/Unsafe Behaviour**: deferred/unavailable. The frozen
  `dataset-manifest.v1.json` entry's own `source_reference` is literally
  `"pending_verification"` -- no source URL is pinned at all -- so no
  source was invented or substituted.

All source access and downloads in this Gate B record occurred on
2026-09-20. Real host profile: Arch Linux, kernel 7.2.4-arch1-2, x86_64,
Intel Core i5-13420H (12 logical CPUs), 15 GiB RAM, Python 3.12.13 (via
`uv`); `nvidia-smi` present but reporting no driver -- confirmed no GPU
backend was available, so every real result below genuinely ran on CPU.

## Phase 7 Part 4 — audio and social/chat benchmark foundation fixtures (Sarthak)

No real Common Voice Indic, AMI Meeting Corpus, or VAST data exists
anywhere in this repository's tests -- none was downloaded onto this
development machine, per this task's explicit rule:

- **ASR/VAD/language-ID fixtures**: `tests/unit/communication_processing/
  test_audio_social_benchmark_metrics.py`/`test_audio_social_benchmark_
  adapters.py` build small, invented `TimeInterval`/`SpeakerTurn`/
  `TranscriptSegmentInput`/`DiarizationSegmentInput` values directly
  (arbitrary millisecond ranges, generic recording-local labels like
  `"speaker_0"`/`"spk_A"`, invented short phrases like `"hello world"`) --
  no real audio recording, transcript, or speaker turn is decoded or read
  anywhere in these tests. `audio_bytes`/`audio_bytes=b"synthetic"`-style
  fields are placeholder bytes only, never real WAV content, since the
  `Fake*Engine`s never actually decode them.
- **Social/chat fixtures**: a small, invented, synthetic Indian-mobile-
  shaped phone number (`"9876543210"`, the same synthetic value used
  throughout this repository's other test suites) embedded in an invented
  message string (`"call me at 9876543210"`) is used to prove
  `DeterministicSocialExtractionEngine` genuinely reuses the real,
  unmodified `social/identifiers.py::extract_mentioned_identifiers` --
  none of it corresponds to a real message, phone number, or person.
- **Safety/CLI fixtures**: `tests/unit/communication_processing/
  test_audio_social_benchmark_safety.py`/`test_audio_social_benchmark_
  cli.py` build synthetic `DatasetManifestEntryV1`/`ModelCandidateV1`
  records directly (to exercise the licence/conditional-status gate
  against both blocked and cleared states) and point every
  `TRACEX_BENCHMARK_*` environment variable at a pytest `tmp_path`
  directory -- never a developer's real environment.
- **Integration smoke fixtures**: `tests/integration/
  communication_processing/test_audio_social_benchmark_smoke.py`
  self-skips (never fabricates a pass) unless `TRACEX_BENCHMARK_DATA_ROOT`
  is actually set and the relevant dataset directory actually exists and
  is non-empty -- the expected state on this development machine. During
  this task's own verification, a synthetic local `vast_social_text`
  directory (one invented JSONL line, the same phone-number fixture
  above) was used to confirm the real CLI genuinely reaches a `succeeded`
  result for the one pair not blocked by licence today -- that synthetic
  directory was created under the session scratchpad and removed
  immediately after, never committed.

No real speech, transcript, chat message, participant name, phone number,
handle, speaker label, model weight, or MacBook-local path appears
anywhere in this module's tests.
