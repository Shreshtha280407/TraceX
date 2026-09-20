# Media-Processing Worker (Phase 2 completion — Gaurav; Phase 2 closeout — Nipun)

Gaurav's phase added a **one-shot worker CLI** on top of `app/modules/media_processing/`'s existing (Gaurav Phase 1) `process_job` pure function, following the exact orchestration pattern Jasraj's `structured_processing` worker established (`docs/architecture/structured-processing-worker.md`) and Sarthak's `communication_processing` worker reused: claim a job through Nipun's internal worker API, resolve that job's evidence via the same claim-token-bound secure input stream, verify its SHA-256, shim the minimal `EvidenceRecordV1` `process_job` expects, call `process_job` completely unchanged, and submit the result.

**This phase (Phase 2 closeout)** replaces the two remaining prototype gaps that phase left, deliberately, for later: (1) `process_job` now runs a **real, local** object detector, OCR engine, and deterministic tracker — no more fake-only analysis components — and (2) both this worker and the graph projector gain a **continuous `--loop` mode** alongside their existing `--once` CLI, with bounded backoff, graceful shutdown, and lease-renewal heartbeats for real production-style operation. See `docs/architecture/phase-2-decisions.md`'s "Real local media inference closeout" for the concrete decisions.

## The full flow

```
uv run python -m app.modules.media_processing.worker --once   (or --loop)
  -> POST /api/v1/internal/worker-jobs/claim (media_detection_v1, then media_metadata_v1 -- see "Supported processors")
  -> GET  /api/v1/internal/worker-jobs/{job_id}/input   (claim-token-bound, SHA-256 verified before decode)
  -> _shim_evidence_record(job, resolved)   (reconstructs the minimal EvidenceRecordV1)
  -> process_job(job, evidence, StaticBytesResolver(resolved.data), detector=..., tracker=..., ocr=...)
  -> POST /api/v1/internal/worker-jobs/{job_id}/result
  -> exit 0 (--once) / loop again (--loop)
```

A run that finds no eligible job, a run that submits a terminal `SUCCEEDED`/`FAILED` result, and a run that defers because the input-stream endpoint is genuinely unreachable are all *successful* CLI exits (`0`, or a normal loop iteration) — only a genuine auth/transport/API failure exits `1`. See `worker.main`'s docstring.

## Supported processors — `media_detection_v1` is now the live route

`worker.SUPPORTED_PROCESSORS` is `(("media_detection_v1", "1.0.0"), ("media_metadata_v1", "1.0.0"))`, tried in that order:

| Processor | Input | Emits | Live-reachable? |
|---|---|---|---|
| `media_detection_v1` / `1.0.0` | Any supported image/video | `media_metadata` always, plus `object_detection`/`text_region_detection`, `anonymous_track_segment`, `ocr_text_mention` when a detector/tracker/OCR are configured | **Yes** — `evidence_lifecycle/routing.py` now routes every `SourceType.IMAGE`/`SourceType.VIDEO` upload here |
| `media_metadata_v1` / `1.0.0` | Any supported image/video | `media_metadata` only | No longer reachable via a real upload; still fully supported for a directly-constructed/legacy job |

`process_job` still requires a real `ObjectDetector` to run `media_detection_v1` at all (`analysis_not_configured` without one) — but a real one now exists (`analysis/onnx_detector.py`) and is what `worker._build_analysis_components` builds from `Settings` at CLI startup. `_effective_processors(detector)` computes the *actual* claim set a running instance uses: the full `SUPPORTED_PROCESSORS` tuple when a detector loaded successfully, or just `media_metadata_v1` alone when it didn't (see "Model asset bootstrap" below) — so an instance with no model asset bootstrapped never claims, and then inevitably fails, a detection job a properly-configured instance could have handled. `--require-analysis` turns a missing detector into a hard startup failure instead, for a deployment that wants to guarantee real detection before it runs at all.

## Real local detector (`analysis/onnx_detector.py`)

`OnnxObjectDetector` runs YOLOX-s (COCO 80-class, anchor-free) via `onnxruntime` — no `ultralytics`/`torch`/cloud API, ever.

- **CPU by default, CUDA when genuinely available**: `DetectorConfig.device` (`"auto"`/`"cpu"`/`"cuda"`) selects `onnxruntime` execution providers; `"auto"` prefers `CUDAExecutionProvider` only if the installed `onnxruntime` actually reports one available (i.e. `onnxruntime-gpu` on a CUDA-capable host — swap the base `onnxruntime` dependency for it, see `pyproject.toml`'s `[project.optional-dependencies] gpu` group), and falls back to CPU otherwise; `"cuda"` without a real provider fails clearly (`ModelAssetError`) rather than silently running on CPU.
- **Checksum-verified before load**: SHA-256 of `DetectorConfig.model_path` is checked against `expected_sha256` *before* the file is ever handed to `onnxruntime` — a missing or corrupted/tampered file fails with `ModelAssetError` and never reaches the inference engine.
- **Deterministic**: same frame in, same detections out (letterbox resize → grid/stride decode → confidence-thresholded NMS → unletterbox back to original pixel coordinates) — no randomness anywhere in the pipeline.
- **Labels**: emits the model's own COCO class name as `detection.label` (`"person"`, `"car"`, `"bottle"`, etc. — not filtered down to `interfaces.SUPPORTED_DETECTION_LABELS`, which that module's own docstring documents as advisory, not enforced). No face recognition, no person re-identification — a "person" box is an anonymous detection, exactly as `analysis/fake_detector.py` always was.

### Model asset bootstrap

No model weights are bundled in this repository or downloaded during normal API/worker execution — ever. An operator runs, once (idempotent, safe to re-run):

```bash
uv run python -m app.modules.media_processing.bootstrap_models
# or, in Docker:
docker compose run --rm media-model-bootstrap
```

This downloads the pinned asset, verifies its SHA-256, and writes it to `Settings.media_detector_model_path` (default `models/media/object_detection_yolox_2022nov.onnx`, git-ignored). A checksum mismatch deletes the partial/corrupt download and exits `1` — nothing is ever left in place under the expected filename unless it verified.

**Model provenance** (see `analysis/onnx_detector.py`'s module docstring for the full detail):

| Field | Value |
|---|---|
| Model | YOLOX-s, COCO-2017 80-class object detector |
| Source | `github.com/opencv/opencv_zoo`, commit `0b263e423d012606b83d1f81238d11c177da2b9c` (pinned, not a branch head) |
| License | Apache License 2.0 |
| SHA-256 | `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` (35,858,002 bytes) |

Chosen over exporting an Ultralytics YOLOv8n checkpoint because it's already a ready-to-use ONNX file under a permissive license from a well-known maintainer (OpenCV.org) — exporting a `.pt` checkpoint would additionally require `ultralytics`+`torch` (a large, GPU-oriented toolchain `CLAUDE.md` asks this project to avoid) as a runtime dependency just to produce the one file this adapter needs.

Missing/invalid model assets fail clearly: `OnnxObjectDetector.__post_init__` raises `ModelAssetError` (missing file, checksum mismatch, or an onnxruntime load failure) with a message pointing at this section — never a silent fallback to fabricated detections.

## Real local OCR (`analysis/tesseract_ocr.py`)

`TesseractTextRecognizer` wraps the system `tesseract` binary via `pytesseract` — a thin subprocess wrapper, no bundled weights, no network access.

### OCR runtime setup

The `tesseract-ocr` system package (this repository's `Dockerfile` installs it; `tesseract-ocr-eng` comes along as its own Recommends) must be present on the host/container. `TesseractTextRecognizer.__post_init__` checks the binary and the requested language pack (`Settings.media_ocr_language`, default `eng`) are both available, raising `OcrRuntimeError` with an actionable message otherwise (`apt-get install tesseract-ocr tesseract-ocr-<lang>` for additional languages).

### How OCR is invoked — independent of the general object detector

`recognize_regions(image)` is the primary entry point `worker.py` calls directly on the whole frame/image whenever an OCR component is configured — **not** gated behind the detector finding a `text_region`-labelled box, since the real COCO-class detector has no such class and would never produce one. `pytesseract.image_to_data` gives per-word text, confidence, and pixel bounding boxes in one pass; this adapter groups consecutive words sharing the same block/paragraph/line index (tesseract's own layout grouping, not an invented one) into one recognized region per line — generally the more useful atomic OCR observation for investigative evidence (a sign, a plate, a heading) than an isolated word. The older per-crop `recognize(crop)` path (gated on a detector's own `text_region` label, kept for exactly that future case) still exists unchanged and is a thin wrapper over the same grouping logic.

Confidence is tesseract's own `0-100` scale, divided by `100` into `[0,1]`; `Settings.media_ocr_min_confidence` (default `0.3`) filters out low-confidence noise, which natural-photo OCR produces far more of than scanned-document OCR. Recognized text is never logged — only region/word counts and confidence values ever appear in a structured log call.

## Real, deterministic tracking (`analysis/iou_tracker.py`)

`IoUTracker` is the exact same greedy, class-aware, deterministic IoU-association algorithm `analysis/fake_tracker.py`'s `FakeObjectTracker` already implemented (kept there, unchanged, as the fixed CI-only stand-in existing tests depend on) — promoted to a named, versioned, independently-configurable production component (`IoUTrackerConfig.iou_match_threshold`, default `0.1`). No ML model, no external tracking library (ByteTrack/DeepSORT): IoU association across consecutive sampled timestamps is a standard, well-documented, non-learned tracking-by-detection policy — the same core association step those libraries build on, minus their Kalman-filter motion prediction, which this module's bounded, sparse frame sampling doesn't need.

- **Deterministic**: same `detections_by_time_ms` in, same `TrackSegment`s (including `local_track_id`, via `deterministic_uuid`) out.
- **Never bridges a label change**: a "person" track can never continue as a "vehicle" track.
- **Never merges overlapping same-time detections**: two distinct objects at the same timestamp are always two distinct tracks.
- **Evidence-local only**: `local_track_id` is valid only within one `track()` call for one evidence item — never a cross-evidence, cross-case, or biometric identity claim (`CLAUDE.md`: no automatic identity merge). `TrackSegment` carries no `case_id`/`evidence_id` field at all, structurally.

## A second gap this phase's live wiring found: `video/x-matroska`

`evidence_lifecycle/routing.py` has accepted `video/x-matroska` for `SourceType.VIDEO` since Phase 1, but `media_processing/source.py`'s `MediaKind` enum had no matching entry — fixed additively in the media-worker-foundation phase (`MediaKind.VIDEO_MATROSKA`, added to `VIDEO_KINDS`); `ffprobe`/`ffmpeg` are container-format-agnostic, so no other code needed to change.

## Shimming `EvidenceRecordV1` for `process_job`

`process_job` only ever reads three fields off its `evidence: EvidenceRecordV1` parameter — `.content_type`, `.original_filename` (both routed through `source.classify_media`), and `.source_type` (cross-checked against the classified modality by `worker._check_source_type`). `worker._shim_evidence_record` reconstructs the minimal object `process_job` needs, mirroring `structured_processing.worker._shim_evidence_record`'s identical established pattern.

## Input-access boundary (unchanged from Phase 2.2)

Identical to `structured_processing`'s and `communication_processing`'s: `GET /api/v1/internal/worker-jobs/{job_id}/input`, authenticated with `WORKER_TOKEN` plus the per-job claim token, streamed in bounded chunks, never exposing an object key, bucket, endpoint, or storage credential. See `docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery" section for the authoritative endpoint shape.

## Lease-renewal heartbeat (Phase 2 closeout)

Real detection/OCR/tracking on a large video, or a `--loop` instance idle between jobs, can legitimately take longer than the default `WORKER_LEASE_SECONDS` (300s) lease window a job was claimed under. `POST /api/v1/internal/worker-jobs/{job_id}/renew` (new; see `docs/architecture/worker-job-lifecycle.md`) extends a currently-`running`, unexpired-lease job's lease — same claim-token + worker-identity authorization shape as `/result`/`/input`, same generic `401` on any failure mode (unknown job, wrong token, wrong worker, not running, already-expired lease), audited identically as `worker_job_access_denied`. Crucially, **a lease can never be extended past its own expiry**: both the server-side check and the atomic SQL `UPDATE` re-verify `lease_expires_at >= now` before renewing, so a legitimate reclaim by another worker after a real expiry always wins — renewal only ever extends a lease that hasn't lapsed yet.

`worker._lease_heartbeat` wraps the `process_job` call in `run_once` with a background thread that calls `client.renew(...)` every `Settings.media_worker_renew_interval_seconds` (default 60s) while real analysis is in progress. A missed/failed renewal is logged and swallowed, not raised into the foreground work — if the lease genuinely expires despite best-effort renewal, the eventual `submit_result` call fails loudly and honestly on its own (a reclaimed job's token no longer matches), which is the correct outcome; a heartbeat's job is to make that the rare case, not to guarantee it can never happen.

## Continuous operation: `--loop`

```bash
uv run python -m app.modules.media_processing.worker --loop
```

`run_loop` continuously claims, processes, and submits until a graceful shutdown is requested or too many consecutive failures occur:

- **Idle polling**: an empty claim sleeps `Settings.media_worker_poll_interval_seconds` (default 5s) before trying again — interruptible, so a shutdown request wakes it immediately rather than after the full interval.
- **Immediate retry on work**: a successfully processed job loops again right away (more work may be queued), never waiting out the poll interval.
- **Bounded exponential backoff on failure**: `min(poll_interval * 2**consecutive_failures, Settings.media_worker_max_backoff_seconds)` (default cap 60s) — a transient API blip doesn't spin the CPU, and a genuinely down API doesn't retry forever: `Settings.media_worker_max_consecutive_failures` (default 5) stops the loop entirely, exit code `1`, an operator-visible problem to page on, not something to silently retry.
- **Graceful SIGINT/SIGTERM shutdown**: `shutdown_event` (a `threading.Event`, set from real signal handlers in `main`) is checked *before* every claim attempt, never mid-processing — once set, no new job is claimed; whatever `run_once` iteration is already in flight always finishes to a submitted terminal/deferred result first (that is `run_once`'s own unconditional contract), so no partially-claimed job is ever abandoned. Exit code `0` on a clean shutdown.
- **Multiple instances are safe by construction**: nothing new here — the existing atomic `FOR UPDATE SKIP LOCKED` claim (`evidence_lifecycle.repository.claim_job`) already guarantees two concurrent `media-worker` processes (or `--loop`/`--once` instances mixed) can never claim the same job twice.

## Image and video processing (Gaurav Phase 1, unchanged by this phase)

See `docs/architecture/media-processing-v1.md` for the full design (decoding, probing, sampling, geometry, limits, capability detection, performance reporting). In summary:

- **Image**: Pillow-based decode with dimensions checked from the lazily-opened header *before* any pixel data is materialized (decompression-bomb-safe); emits `media_metadata` (width, height, format, color mode, EXIF orientation when present and valid).
- **Video**: `ffprobe`-based safe metadata extraction, deterministic frame sampling, and per-timestamp `ffmpeg` frame extraction (no shell interpolation, no full-video-in-memory load).
- **Detection/tracking/OCR**: typed `ObjectDetector`/`ObjectTracker`/`TextRecognizer` Protocols (`analysis/interfaces.py`), now with real, local, checksum-verified/runtime-checked implementations (above) alongside the deterministic fake test adapters (`analysis/fake_*.py`) every existing CI test still uses unchanged.

## Source-locator conventions

| Observation | Locator |
|---|---|
| `media_metadata` (image or video) | `time_start_ms=0` (a fixed convention, not a real timestamp — see ADR-004) |
| `object_detection`/`text_region_detection` (image) | `bbox_xyxy_normalized` only |
| `object_detection`/`text_region_detection` (video) | `frame_number` (when trustworthy) + `time_start_ms`/`time_end_ms` + `bbox_xyxy_normalized` |
| `anonymous_track_segment` | `time_start_ms`/`time_end_ms` spanning the whole track |
| `ocr_text_mention` (detector-gated crop) | Same locator shape as the detection it was cropped from |
| `ocr_text_mention` (whole-frame `recognize_regions`) | The recognized region's own `bbox_xyxy_normalized` (already in full-frame coordinates — no detection crop involved) |

Every bounding box is validated by `image/geometry.py::to_normalized` before an observation is ever constructed — non-finite, non-positive dimensions, or geometry failing `BoundingBoxNormalized`'s own validation is rejected (`invalid_bounding_box`), never clamped.

## Confidence and provenance (unchanged from Phase 1)

`extraction_confidence` measures extraction/statement quality only — **never** a probability of guilt or culpability (`CLAUDE.md`). `provenance.observation_id()` derives every `observation_id` deterministically from `(case_id, evidence_id, processor_name, processor_version, observation_type, locator, discriminator)`. `local_track_id` is evidence-local and deterministic — never a global or cross-evidence identity.

## Failure and defer policy

- **Unsupported content type/MIME-extension mismatch, over-limit dimensions/pixels/duration/frame rate, corrupt/unidentifiable image, unprobeable video, missing `ffmpeg`/`ffprobe`**: `WorkerStatus.FAILED` with a safe, non-secret `WorkerError`.
- **A single malformed detector/tracker/OCR output item**: skipped, not a job failure.
- **Input resolution unavailable**: `WorkerStatus.DEFERRED`, checkpoint `input_resolution_unavailable`.
- **SHA-256 mismatch**: `WorkerStatus.FAILED`, `evidence_integrity_mismatch`, `retryable=True` — before any decode.
- **Missing/invalid detector model asset, or missing OCR runtime**: caught at `_build_analysis_components` (CLI startup), never mid-job — logged as a clear warning, degrades to metadata-only claiming (or, with `--require-analysis`, a hard startup failure).
- `queued`/`running` are never submitted as a final result.

## Benchmark

```bash
uv run python -m app.modules.media_processing.benchmark <path/to/image_or_video>
```

Runs the real pipeline (real detector/OCR if configured) once against a local file and reports **measured** results only — media size/duration, sampled frame count, device used (`cpu`/`cuda`), per-stage timings, throughput, and observation counts. Never fabricates or extrapolates a promised throughput figure; see `docs/qa/test-results.md` for actual measured runs on this project's development hardware.

## Phase 7 Part 3 visual candidate-model benchmark (Gaurav)

A second, entirely separate benchmark exists alongside the one above:
`uv run python -m app.modules.media_processing.visual_benchmark_cli` (new
`visual_benchmark*.py` files) evaluates *candidate* detector/tracker/OCR
models (Ultralytics YOLO11n/YOLO11s, ByteTrack, a lightweight PaddleOCR
visual-text variant) against Phase 7 Part 1's dataset manifest and
result contracts — a model-selection benchmark, not the pipeline-
throughput benchmark above. This module's own "Non-goals (this phase)"
section below ruled out "model-accuracy benchmarking against a labeled
dataset" for the Phase 2 closeout that built the *production*
detector/OCR/tracker — Phase 7 Part 3 is that later, explicitly-approved
phase, built as a wholly additive harness that never changes
`analysis/`/`worker.py`'s production behavior. See
`docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part 3"
section for the full design, and `docs/runbooks/local-development.md` for
CLI usage and the MacBook Gate B pre-flight handoff.

## Graph integration — no code changes needed

Media observations reach the existing durable graph-projection queue automatically and unmodified, exactly as before this phase — see `docs/architecture/graph-projection.md`.

## Structured logging

`_configure_logging` mirrors every sibling worker's structlog JSON configuration. The worker token, claim tokens, and raw media bytes/OCR text are never logged.

## Non-goals (this phase)

Still ruled out per `CLAUDE.md`: face recognition, person re-identification, cross-camera/cross-case identity matching, entity resolution, `EntityV1`/`EventV1` creation, Neo4j writes from this worker itself, guilt/risk scoring, model-accuracy benchmarking against a labeled dataset, a Celery/Kafka/Kubernetes queue system, a public worker-admin API, and frontend work. Real detection/OCR/tracking and continuous operation (`--loop`) are exactly the two capabilities this phase adds — nothing broader.
