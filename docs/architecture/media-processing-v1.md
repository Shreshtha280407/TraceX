# Video and Image Processing Foundation (Gaurav Phase 1)

`app/modules/media_processing/` is the first media-processing layer: it turns local CCTV video and surveillance images into canonical `ObservationV1` objects and safe `WorkerResultV1` results, the same shape `app/modules/structured_processing/` produces for documents/CDR/financial sources. No real object-detection, tracking, or OCR model is implemented in this phase (see `CLAUDE.md`) — this module builds the deterministic, provenance-preserving pipeline foundation those models will later plug into behind fixed protocol boundaries.

## Why workers return `ObservationV1`, not database writes

`worker.process_job(job, evidence, resolver, **analysis)` is a function from `(WorkerJobV1, EvidenceRecordV1, SourceResolver)` to `WorkerResultV1`. It never imports `sqlalchemy`, `asyncpg`, `neo4j`, `redis`, or `minio` (enforced by `tests/unit/media_processing/test_media_safety.py`, a static AST check across every file in the module, mirroring `tests/unit/structured_processing/test_safety.py`). Persisting observations into PostgreSQL/Neo4j is a later-phase orchestrator's job, not the worker's — this keeps the module trivially testable and keeps worker credentials scoped to nothing at all.

## The `SourceResolver` boundary, and why bytes are materialized to a temp file

```python
class SourceResolver(Protocol):
    def read_bytes(self, object_uri: str) -> bytes: ...
```

Unlike `structured_processing` (which parses bytes directly in memory), `ffprobe`/`ffmpeg` require a real local file path. `source.temporary_media_file(data, suffix=...)` is the *only* place this module writes evidence bytes to disk: it writes them under `tempfile.mkstemp()`, yields the path inside a context manager, and unconditionally unlinks it in `finally` — so a probe/extraction failure never leaves the file behind (verified by `tests/integration/media_processing/test_video_pipeline.py::test_temporary_artifacts_are_cleaned_up`). No error message raised anywhere in this module ever includes that path — see "Safe errors" below.

## Media classification: content-type + extension, cross-checked

`source.classify_media(content_type, filename)` accepts seven formats: `video/mp4`, `video/quicktime`, `video/x-msvideo`, `video/x-matroska` (video — the last added in the Phase 2 completion worker build to match `evidence_lifecycle/routing.py`'s content-type set, see `docs/architecture/media-processing-worker.md`), `image/jpeg`, `image/png`, `image/webp` (image). Neither signal is trusted alone — a mismatch between them is `unsupported_content_type`, the same anti-spoofing posture `structured_processing.document.classifier` takes. `worker._check_source_type` additionally cross-checks the classified modality against `EvidenceRecordV1.source_type` (`SourceType.VIDEO`/`SourceType.IMAGE`), rejecting a mismatch the same way.

## Video: probe, sample, extract

- **Probe** (`video/probe.py`, via `ffprobe -show_format -show_streams -print_format json`): every field is defensively parsed. `duration_ms`/`width`/`height` are the only fields whose absence is a hard `media_probe_failed` — everything else (`frame_rate`, `frame_count`, `video_codec`, `rotation_degrees`) is `None` when not reliably present, never guessed. `has_audio` is a plain `bool`, computed from whichever streams were actually found once a video stream is confirmed present.
- **Sampling** (`video/sampling.py`): three deterministic strategies — `uniform_interval`, `fixed_fps`, `explicit_timestamps` — all operating in milliseconds internally. The same `VideoMetadata` + `SamplingRequest` always produces the same, deduplicated, ascending list of `SampleTimestamp`s. Exceeding `SamplingRequest.max_frames` is a hard `sampling_limit_exceeded` — this module never silently truncates a sample plan to fit.
- **Frame extraction** (`video/frames.py`): each timestamp is extracted independently via `ffmpeg -ss <seconds> -frames:v 1 -f rawvideo -pix_fmt rgb24 pipe:1` (a raw-pixel stdout pipe reshaped by `numpy` — no per-frame temp files, no shell interpolation, argument arrays only). A single failed timestamp (out of range, a corrupt region) is skipped and counted in `frames_failed` rather than failing the whole batch; only a missing `ffmpeg` binary aborts up front.

### `frame_number` is `None` unless the mapping is actually trustworthy

`_trustworthy_frame_number` only computes a `frame_number` when **both** `VideoMetadata.frame_count` and `VideoMetadata.frame_rate` were reliably probed — otherwise ffmpeg's timestamp-based seek has no verifiable relationship to a specific frame index (variable-frame-rate containers being the common failure mode), and this module preserves the timestamp while omitting `frame_number` rather than guessing one. See ADR-004.

## Image: decode, then validate dimensions before pixel decode

`image/decoder.py` uses Pillow's lazy `Image.open()` to read `width`/`height`/`format` from the header alone, checks them against `MediaLimits` (`limits.check_image_pixel_dimensions`) **before** calling `.convert("RGB")`/materializing an array — so a decompression-bomb-like image is rejected without ever decoding its pixels. Both video frame extraction and image decode converge on the same pixel-data shape, `models.Frame` (an RGB `uint8` `(height, width, 3)` `numpy` array), so every analysis interface below accepts one shape regardless of source modality.

## Bounding-box normalization never clamps

`image/geometry.to_normalized(PixelBoundingBox, image_width, image_height)` converts pixel coordinates to the `[0, 1]`-normalized `BoundingBoxNormalized` (`app/contracts/common.py`) every observation's `source_locator` requires. A non-finite value, a non-positive image dimension, or geometry that fails `BoundingBoxNormalized`'s own `x_min < x_max`/`y_min < y_max`/`[0, 1]` validation is rejected with `invalid_bounding_box` — **never clamped into range**. `worker.py` treats a rejected box as "skip this one detection," not "fail the whole job" — see "Malformed model output is skipped, not fatal" below.

## Analysis interfaces: protocols now, real models later

`analysis/interfaces.py` defines three `Protocol`s — `ObjectDetector`, `ObjectTracker`, `TextRecognizer` — and their result shapes (`ObjectDetection`, `TrackSegment`, `RecognizedText`). No real detector/tracker/OCR engine exists in this phase; `analysis/fake_{detector,tracker,ocr}.py` provide deterministic, clearly-named (`Fake...`) test-only implementations. Each fake self-reports its own interface version in its output's `attributes["model_interface_version"]` field (`fake_detector_v1`/`fake_tracker_v1`/`fake_ocr_v1`) — `worker.py` reads this and forwards it into the resulting observation's `Extractor.model_version` verbatim, with no `isinstance` special-casing anywhere. A later phase wiring in YOLO/ByteTrack/PaddleOCR only needs to implement these same three protocols; nothing else in this module changes.

`worker.process_job` never selects a fake implementation as a silent default: `detector`/`tracker`/`ocr` are keyword-only parameters defaulting to `None`, and `job.processor_name == "media_detection_v1"` without an explicit `detector` fails safely as `analysis_not_configured` rather than quietly falling back to a fake.

## Malformed model output is skipped, not fatal

A single detection with an out-of-range confidence (`provenance.is_valid_confidence`, requiring a finite value in `[0, 1]`) or invalid bounding-box geometry never fails the whole job — it is filtered out before an observation is ever constructed from it, and every *other* valid detection in the same frame/job still produces its observation. This is a deliberate split from job-level failures (unsupported content type, over-limit media, an unusable probe), which do fail the whole job via `ProcessingError` — see `tests/unit/media_processing/test_media_worker.py::test_malformed_detection_confidence_is_skipped_not_fatal` / `::test_malformed_detection_bbox_is_skipped_not_fatal`.

## Observation taxonomy and provenance

Full taxonomy and per-type provenance field reference: `docs/architecture/media-observation-taxonomy-v1.md`. Deterministic-ID and anonymous-tracking design rationale: `docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md`.

## Confidence policy

| Value | Meaning |
|---|---|
| `1.00` | `media_metadata` — read directly from a successful `ffprobe`/decode. |
| Detector's own reported value, validated `∈ [0, 1]` | `object_detection`/`text_region_detection` — a real future detector's confidence, or the fixed test-only value `FakeObjectDetector` reports (`0.99`, tagged `fake_detector_v1`, never presented as a real model's confidence). |
| Tracker's own reported `quality`, validated `∈ [0, 1]` | `anonymous_track_segment` — local cross-frame association quality only, never an identity-certainty score. |
| Recognizer's own reported value, validated `∈ [0, 1]` | `ocr_text_mention` — text-recognition quality only. |

No confidence value here is, or is derived from, a probability of guilt, culpability, or identity certainty — per `CLAUDE.md`.

## GPU/CPU capability and performance measurement

`capability.detect_capability()` safely detects `ffmpeg`/`ffprobe` availability and best-effort GPU visibility via `nvidia-smi --query-gpu=...` — every subprocess call is wrapped so a missing binary, a driver-less `nvidia-smi` (`NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`), or a timeout all collapse to "not available" rather than raising. GPU absence is the normal case in this phase (no GPU-backed analysis code exists to accelerate); this module never installs, requires, or claims to use CUDA.

`performance.Stopwatch` measures per-stage wall-clock time (`probe_ms`/`sampling_plan_ms`/`frame_extraction_ms`/`analysis_ms`/`observation_construction_ms`/`total_ms`) plus `frames_requested`/`frames_extracted`/`frames_failed`, using `time.monotonic()`. `worker.process_job` accepts an optional `stopwatch` keyword — a pure side-channel that never affects the returned `WorkerResultV1`, preserving the "a worker's only contract is `WorkerJobV1` in, `WorkerResultV1` out" rule from `CLAUDE.md`. `performance.build_performance_report` pairs measured timings with a fresh capability snapshot. **No fixed throughput figure is ever claimed** (e.g. "one hour of CCTV in N minutes") — only actual measured values, honestly reported.

## Safe errors and logging

`errors.ProcessingError` is the only exception this module raises for an expected failure mode; `worker.process_job` catches exactly this type and converts it to `WorkerResultV1(status=FAILED, error=WorkerError(code=..., message=...))`. Error messages never contain a local filesystem path, raw subprocess stdout/stderr, or raw frame/image bytes — verified behaviorally in `tests/unit/media_processing/test_media_safety.py`. Anything that isn't a `ProcessingError` is a programming bug and is deliberately allowed to propagate rather than being silently repackaged as a plausible-looking failure.

## Input limits

`limits.MediaLimits` (defaults in `limits.DEFAULT_MEDIA_LIMITS`): max input bytes, max video duration, max width/height, max frame rate, max sampled frames, max image pixels, max OCR crop count, and a subprocess timeout. Video limits are checked immediately after probing (cheap) and before sampling/extraction (expensive); image pixel-dimension limits are checked before pixel decode — both "reject over-limit content before expensive work" per the phase brief.

## Deferred to a later phase

Real YOLO/ByteTrack/PaddleOCR model integration, face recognition, person re-identification, biometric identification, automatic identity resolution, cross-camera identity matching, cross-modal correlation, Neo4j graph writes, actual file uploads/object-storage writes/hashing/Merkle roots/signatures, a video frontend/player, ML training/evaluation, and GPU Docker services are all explicitly out of scope for this phase — see `CLAUDE.md` and `docs/qa/known-limitations.md`.

## Phase 6 visual integrity provenance

After the coordinator accepts a persisted manifest/chunk publication, an
accepted video/image detection, visual OCR observation, or evidence-local
technical track receives one `visual_provenance.v1` integrity projection and
one additive `visual_observation_provenance` leaf. The projection directly
retains only persisted manifest/chunk UUIDs, extractor/version/configuration
identity, evidence SHA-256, validation state, correlation state, and bounded
technical categories. It SHA-256 commits locator/timeline, frame mapping,
normalized geometry, local track ID, OCR content, and chunk boundary/version.

The coordinator validation remains authoritative: a missing persisted scope,
cross-chunk interval, invalid geometry, impossible frame mapping, or
incomplete/rejected visual validation produces no leaf. Local track lifecycle
values such as `ended_unmatched` are technical-only and never a person,
relationship, or entity assertion. Raw frame/image/crop data, OCR text, plate
values, embeddings, faces, raw track IDs, and object URIs are excluded.
