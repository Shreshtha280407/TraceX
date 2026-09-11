# Media-Processing Worker (Phase 2 completion — Gaurav)

This phase adds a **one-shot worker CLI** on top of `app/modules/media_processing/`'s existing (Gaurav Phase 1) `process_job` pure function, following the exact orchestration pattern Jasraj's `structured_processing` worker established (`docs/architecture/structured-processing-worker.md`) and Sarthak's `communication_processing` worker reused: claim a job through Nipun's internal worker API, resolve that job's evidence via the same claim-token-bound secure input stream, verify its SHA-256, shim the minimal `EvidenceRecordV1` `process_job` expects, call `process_job` completely unchanged, and submit the result. No daemon, polling loop, Celery, or scheduler exists anywhere in this repository.

## The full flow

```
uv run python -m app.modules.media_processing.worker --once
  -> POST /api/v1/internal/worker-jobs/claim (media_metadata_v1 only -- see "Supported processors")
  -> GET  /api/v1/internal/worker-jobs/{job_id}/input   (claim-token-bound, SHA-256 verified before decode)
  -> _shim_evidence_record(job, resolved)   (new: reconstructs the minimal EvidenceRecordV1)
  -> process_job(job, evidence, StaticBytesResolver(resolved.data))   (Phase 1, unchanged)
  -> POST /api/v1/internal/worker-jobs/{job_id}/result
  -> exit 0
```

A run that finds no eligible job, a run that submits a terminal `SUCCEEDED`/`FAILED` result, and a run that defers because the input-stream endpoint is genuinely unreachable are all *successful* CLI exits (`0`) — only a genuine auth/transport/API failure exits `1`. See `worker.main`'s docstring.

## Supported processors — why only `media_metadata_v1` is claimed live

`worker.SUPPORTED_PROCESSORS` is deliberately `(("media_metadata_v1", "1.0.0"),)` — a single entry, unlike every sibling worker's multi-processor tuple:

| Processor | Input | Emits | Live-reachable? |
|---|---|---|---|
| `media_metadata_v1` / `1.0.0` | Any supported image/video | `media_metadata` (width/height/format/orientation for images; width/height/duration/frame rate/codec/rotation for video) | Yes — the only profile `evidence_lifecycle/routing.py` ever routes `SourceType.IMAGE`/`SourceType.VIDEO` to |
| `media_detection_v1` / `1.0.0` | Any supported image/video, plus a real `ObjectDetector` (and optionally `ObjectTracker`/`TextRecognizer`) | `object_detection`/`text_region_detection`, `anonymous_track_segment`, `ocr_text_mention` | No — see below |

`media_detection_v1` requires a real `ObjectDetector` to be injected (`process_job` raises `analysis_not_configured` without one); no real local detector/tracker/OCR engine is approved or available in this phase (`analysis/fake_*.py` are deterministic test doubles only — see `CLAUDE.md`'s non-goals and `docs/qa/known-limitations.md`). `evidence_lifecycle/routing.py` never routes a real upload to `media_detection_v1` either, so this CLI trying to claim it would only ever return "no work available" — it is simply omitted from the live claim loop. `process_job` itself fully supports `media_detection_v1` today (unit-tested via `analysis/fake_*.py`'s deterministic doubles); a future phase that wires in a real, locally-run detector needs no change to `process_job`, only a change to how `main()` constructs and injects one.

## A second gap this phase's live wiring found: `video/x-matroska`

`evidence_lifecycle/routing.py` has accepted `video/x-matroska` for `SourceType.VIDEO` since Phase 1 (`SOURCE_TYPE_CONTENT_TYPES[SourceType.VIDEO]`), but `media_processing/source.py`'s `MediaKind` enum had no matching entry — a real `.mkv` upload would pass routing's content-type check (routing never inspects `media_processing` at all, by design — see `evidence_lifecycle/routing.py`'s own module docstring) and then fail inside the worker with `unsupported_content_type`, discoverable only by actually classifying a claimed job's resolved content type, not by unit-testing routing or `classify_media` in isolation against each other. Fixed additively: `MediaKind.VIDEO_MATROSKA = "video/x-matroska"` (extension `.mkv`), added to `VIDEO_KINDS`. `ffprobe`/`ffmpeg` are container-format-agnostic, so no other code needed to change — `video/probe.py` and `video/frames.py` work identically for MKV, MP4, QuickTime, and AVI containers. See `tests/unit/media_processing/test_source.py` and `tests/unit/evidence_lifecycle/test_media_routing.py`.

## Shimming `EvidenceRecordV1` for `process_job`

`process_job` only ever reads three fields off its `evidence: EvidenceRecordV1` parameter — `.content_type`, `.original_filename` (both routed through `source.classify_media`), and `.source_type` (cross-checked against the classified modality by `worker._check_source_type`). `worker._shim_evidence_record` reconstructs the minimal object `process_job` needs: real values where this worker actually has them (`case_id`/`evidence_id`/`source_type` from the job, `object_uri=job.input_object_uri`, `sha256` computed from the bytes actually resolved, `uploaded_at=job.requested_at`, `content_type`/`original_filename` from the resolved input), and clearly-commented unused placeholders for the rest (`classification`, `uploaded_by`, `processing_status`, `parser_profile` — fields this worker has no authenticated way to obtain and `process_job` never reads). This is the exact same pattern `structured_processing.worker._shim_evidence_record` already established for the identical problem (a real worker run never receives a claimed job's full evidence record — only `content_type`/`original_filename`/the recorded SHA-256, via the claim-token-bound input endpoint's response headers, plus `source_type` from the claimed `WorkerJobV1` itself). Reusing it here — rather than introducing a leaner, media-specific metadata type — keeps one honest, reviewed answer to "what does a worker do when `process_job` wants more than a live run can know" across every sibling module, instead of three different answers to the same question.

## Input-access boundary (unchanged from Phase 2.2)

Identical to `structured_processing`'s and `communication_processing`'s: `GET /api/v1/internal/worker-jobs/{job_id}/input`, authenticated with `WORKER_TOKEN` plus the per-job claim token, streamed in bounded chunks, never exposing an object key, bucket, endpoint, or storage credential. See `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section for the authoritative endpoint shape.

- `client.WorkerApiClient.fetch_input` parses the real response: filename from `Content-Disposition` (RFC 6266), the evidence's recorded SHA-256 from `X-TraceX-Evidence-SHA256` into `ResolvedMediaInput.expected_sha256`, and enforces a worker-side size bound (`DEFAULT_MEDIA_LIMITS.max_input_bytes`, 500 MiB) on the response before it's handed anywhere else — independent of, and prior to, `process_job`'s own `check_input_size`.
- `run_once` verifies `expected_sha256` against the actually-received bytes (`hashlib.sha256(resolved.data).hexdigest()`) **before** any image decode or video probe is attempted — a mismatch submits `FAILED`/`evidence_integrity_mismatch` (`retryable=True`), never silently decoding bytes that don't match what the API reported.
- `InputResolutionUnavailableError`/`CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE` degrade a claimed job to an honest `DEFERRED` if the input endpoint is ever unreachable, rather than crashing the CLI.
- `StaticInputResolver` (unit tests) and `LiveInputResolver` (the real client) mirror `communication_processing.input_resolver`'s identical `WorkerInputResolver` Protocol split exactly.

## Image and video processing (Gaurav Phase 1, unchanged by this phase)

See `docs/architecture/media-processing-v1.md` for the full design (decoding, probing, sampling, geometry, limits, capability detection, performance reporting) — none of it changed by this phase's orchestration work. In summary:

- **Image**: Pillow-based decode with dimensions checked from the lazily-opened header *before* any pixel data is materialized (decompression-bomb-safe); emits `media_metadata` (width, height, format, color mode, EXIF orientation when present and valid).
- **Video**: `ffprobe`-based safe metadata extraction (duration, dimensions, frame rate, codec, rotation, audio presence — every optional field `None` rather than guessed when not reliably present), deterministic frame sampling (`uniform_interval`/`fixed_fps`/`explicit_timestamps`, always the same ascending, deduplicated timestamps for the same input), and per-timestamp `ffmpeg` frame extraction (no shell interpolation, no full-video-in-memory load, one corrupt timestamp skipped rather than failing the whole batch).
- **Detection/tracking/OCR**: typed `ObjectDetector`/`ObjectTracker`/`TextRecognizer` Protocols (`analysis/interfaces.py`) with deterministic fake test adapters only (`analysis/fake_*.py`) — no real model, no GPU requirement, no cloud AI API, ever. `capability.detect_capability()` best-effort auto-detects `nvidia-smi`/`ffmpeg`/`ffprobe` availability without requiring a GPU for correctness.

## Source-locator conventions

| Observation | Locator |
|---|---|
| `media_metadata` (image or video) | `time_start_ms=0` (a fixed convention, not a real timestamp — see ADR-004) |
| `object_detection`/`text_region_detection` (image) | `bbox_xyxy_normalized` only |
| `object_detection`/`text_region_detection` (video) | `frame_number` (when trustworthy) + `time_start_ms`/`time_end_ms` + `bbox_xyxy_normalized` |
| `anonymous_track_segment` | `time_start_ms`/`time_end_ms` spanning the whole track |
| `ocr_text_mention` | Same locator shape as the detection it was cropped from |

Every bounding box is validated by `image/geometry.py::to_normalized` before an observation is ever constructed — a non-finite value, non-positive image dimensions, or geometry failing `BoundingBoxNormalized`'s own `[0,1]`/`x_min<x_max`/`y_min<y_max` validation is rejected (`invalid_bounding_box`), never clamped into range. No locator is ever empty: `media_metadata` always carries `time_start_ms=0`, every detection/OCR/track observation always carries a real, validated locator.

## Confidence and provenance (unchanged from Phase 1)

`extraction_confidence` measures extraction/statement quality only — **never** a probability of guilt or culpability (`CLAUDE.md`). `provenance.observation_id()` derives every `observation_id` deterministically from `(case_id, evidence_id, processor_name, processor_version, observation_type, locator, discriminator)` — reprocessing the same evidence with the same processor version always yields the same ID, so a safe retry never creates duplicate observations (matches `EvidenceLifecycleService.submit_result`'s idempotency contract). `local_track_id` (the `anonymous_track_segment` discriminator) is evidence-local and deterministic — never a global or cross-evidence identity, per `CLAUDE.md`'s "no automatic identity merge" rule.

## Deterministic tracking association

`analysis/fake_tracker.py`'s deterministic double associates same-labelled detections across sampled frames by class-aware nearest-box continuity (documented in its own module docstring) — a real tracker adapter dropped in behind the same `ObjectTracker` Protocol would need to preserve the same guarantee this module's tests already check: given the same sequence of per-frame detections, the same set of `local_track_id`s and `boxes_by_time_ms` is produced every time. No cross-evidence, cross-case, or biometric identity matching exists anywhere in this module, and none is planned for it — entity resolution (if `anonymous_track_segment`/`ocr_text_mention` observations are ever linked to a resolved identity) is exclusively later-phase, human-reviewed work outside this module.

## Failure and defer policy

- **Unsupported content type/MIME-extension mismatch, over-limit dimensions/pixels/duration/frame rate, corrupt/unidentifiable image, unprobeable video, missing `ffmpeg`/`ffprobe`**: `process_job` catches `ProcessingError` and returns `WorkerStatus.FAILED` with a safe, non-secret `WorkerError` — never a raw file path, subprocess stdout/stderr, or media content. See `tests/unit/media_processing/test_media_safety.py`.
- **A single malformed detector/tracker/OCR output item** (out-of-range confidence, invalid bounding-box geometry): skipped, not a job failure — one bad model output never discards an otherwise-good result.
- **Input resolution unavailable**: `WorkerStatus.DEFERRED`, checkpoint `input_resolution_unavailable`.
- **SHA-256 mismatch**: `WorkerStatus.FAILED`, `WorkerError(code="evidence_integrity_mismatch", retryable=True)` — before any decode.
- `queued`/`running` are never submitted as a final result.

## Graph integration — no code changes needed

Media observations reach Shreshtha's existing durable graph-projection queue automatically and unmodified: `EvidenceLifecycleRepository.submit_result` enqueues one `graph_projection_jobs` row per accepted observation in the same transaction, regardless of which processing module produced it. `ocr_text_mention` observations (the only media observation type that ever sets `extracted_entities`) become an evidence-local `EntityMention` via `graph.projection.project_observation_mentions` — generic, unmodified, and already exercised for `structured_processing`/`communication_processing` output. `media_metadata`/`object_detection`/`text_region_detection`/`anonymous_track_segment` observations carry no `extracted_entities` and so create no mention — correct, since they describe technical/anonymous facts, not identity claims. No `EntityV1`/`EntityMention` is ever auto-merged or resolved into a real identity by any part of this pipeline. Verified live end to end (real upload → real worker `--once` → real graph projector `--once`, twice, to confirm no duplication → real Neo4j query + real HTTP `/graph/observations` read) in `tests/integration/media_processing/test_media_worker_live.py` and documented in `docs/qa/test-results.md`.

## Structured logging

`_configure_logging` mirrors every sibling worker's structlog JSON configuration (deliberately duplicated, not imported). `run_once` binds a `run_id` (and a `job_id` once claimed) via `structlog.contextvars`. The worker token, claim tokens, and raw media bytes/OCR text are never logged — `client.py` only ever logs `job_id`s, processor names, and status codes.

## Non-goals (this phase)

Everything `CLAUDE.md` already rules out remains ruled out: a real detector/tracker/OCR model, GPU requirement, cloud AI API calls, face recognition, person re-identification, cross-camera/cross-case identity matching, entity resolution, `EntityV1`/`EventV1` creation, Neo4j writes from the worker itself, guilt/risk scoring, model-accuracy benchmarking, a worker daemon/polling loop/scheduler, and frontend work. This phase adds exactly one new capability: turning a claimed image/video `WorkerJobV1` into a submitted `WorkerResultV1` through the real internal API, for `media_metadata_v1`.
