# ADR-004: Media Provenance and Anonymous Tracking

- Status: Accepted (Gaurav Phase 1 video/image processing foundation)
- Owner: Gaurav
- Date: 2026-09-10

## Context

`app/modules/media_processing/` turns CCTV video and surveillance images into canonical `ObservationV1` objects, the same evidentiary contract `structured_processing` uses for documents. Several decisions had no single obviously-correct answer and are recorded here so later contributors (and whoever builds a real YOLO/ByteTrack/PaddleOCR adapter on top of this) know what was deliberate.

## Decision 1: `frame_number` is omitted, not guessed, unless both `frame_count` and `frame_rate` are reliably probed

**Decision**: `video/frames._trustworthy_frame_number` returns a computed frame index only when `VideoMetadata.frame_count is not None and VideoMetadata.frame_rate is not None`; otherwise every extracted frame's `frame_number` is `None`, and only `time_start_ms`/`time_end_ms` are recorded.

**Why**: `ffmpeg -ss <seconds>` seeks by timestamp, not by frame index — for a variable-frame-rate container, or one whose frame count `ffprobe` could not determine, there is no verifiable mapping from "we asked for 1.5 seconds in" to "this is exactly frame 45." The task brief is explicit: "If exact frame number is not trustworthy, preserve timestamp and omit frame number rather than guessing." A fabricated frame number would look like exact evidence-grade provenance while actually being an approximation — worse than admitting the precision isn't available, the same posture ADR-002 takes for CDR phone-number normalization ("keep the original when uncertain, never a confidently-wrong guess").

## Decision 2: a malformed detection/track/OCR item is skipped, not fatal to the whole job

**Decision**: `worker.py` filters out any `ObjectDetection` with `confidence` outside `[0, 1]` (`provenance.is_valid_confidence`) and any detection whose box fails `image/geometry.to_normalized` (non-finite, degenerate, or out-of-range geometry) *before* constructing an observation from it — the surrounding job still succeeds, with every other valid item's observation intact. Only a job-level problem (unsupported content type, over-limit media, an unusable probe) fails the whole job via `ProcessingError`.

**Why**: The task brief requires "Do not create an observation when fake/model output is malformed or outside configured limits" — worded as a per-item rule, not a per-job one. A future real detector processing a whole video will occasionally emit one bad box among hundreds of good ones (a model artifact, a numerical edge case); discarding the entire video's worth of otherwise-valid detections because of one malformed item would be a disproportionate, evidence-destroying response to a localized problem. This mirrors `video/frames.py`'s own per-timestamp soft-failure design (one unreadable frame doesn't discard the rest of the sample) at the analysis-output layer.

## Decision 3: bounding-box conversion never clamps

**Decision**: `image/geometry.to_normalized` raises `ProcessingError(INVALID_BOUNDING_BOX)` for a non-finite value, a non-positive image dimension, or any geometry `BoundingBoxNormalized`'s own validation rejects — it never rewrites an out-of-range coordinate to the nearest valid value.

**Why**: The task brief is explicit: "Never silently clamp invalid boxes." A clamped box silently manufactures geometry the source detector never actually asserted — e.g. a box whose `x_max` was reported as `1.3` (150% past the image's right edge, likely a real detector bug or a corrupted value) becoming a *seemingly valid* `x_max = 1.0` box that looks like ordinary, trustworthy evidence. Combined with Decision 2, the failure mode for one bad box is "this one detection produces no observation," not "this detection produces a fabricated one."

## Decision 4: `media_metadata`'s locator uses the `time_start_ms=0` convention, for both video and image

**Decision**: The single `media_metadata` observation per evidence item uses `SourceLocator(time_start_ms=0)`, regardless of modality.

**Why**: `SourceLocator` (`app/contracts/common.py`) requires at least one non-`None` field and has no field meaning "the whole file" — every field it defines is modality-specific (`page`, `frame_number`, `row`, etc.). Extending the frozen `V1` contract with a new field for this one case would be a disproportionate change for a single observation type, and the shared-contract rule in `CLAUDE.md` requires exactly that kind of change to go through a new contract version, not a quiet addition. `time_start_ms=0` is a natural "start of the evidence" anchor that already exists on the contract, requires no schema change, and is documented here and in `docs/architecture/media-observation-taxonomy-v1.md` so it reads as a deliberate convention rather than an accidental artifact of reusing a time field for a non-temporal image.

## Decision 5: analysis components are explicit keyword arguments with no default — never a silently-selected fake

**Decision**: `worker.process_job`'s `detector`/`tracker`/`ocr` parameters default to `None`. `job.processor_name == "media_detection_v1"` without an explicit `detector` fails safely as `ANALYSIS_NOT_CONFIGURED`, rather than the worker quietly constructing a `FakeObjectDetector()` on the caller's behalf.

**Why**: The task brief requires fakes to "never be selected silently in production-like configuration." The only way to make that structurally true (not just a naming convention) is for the worker to never construct one itself — every fake instance in this codebase is created explicitly, by a test or by whoever wires up this module later, and is visibly present at the call site. A caller who wants metadata-only processing selects `"media_metadata_v1"` (no analysis component required at all); a caller who wants detection must pass one.

## Decision 6: `ffprobe` for probing, `ffmpeg` raw-pixel pipe for extraction — no OpenCV video decode, no per-frame temp files

**Decision**: `video/probe.py` calls `ffprobe -show_format -show_streams -print_format json`; `video/frames.py` calls `ffmpeg -ss <seconds> -frames:v 1 -f rawvideo -pix_fmt rgb24 pipe:1` per timestamp, reshaping the raw stdout bytes into a `numpy` array. `opencv-python-headless` is used only for `image/decoder.py`'s array conversion path, never for video decode.

**Why**: `ffprobe`'s structured JSON output is far more reliable for metadata extraction than reverse-engineering the same information from OpenCV's `VideoCapture` properties (which vary in accuracy by codec/container and don't expose `ffprobe`'s explicit `nb_frames`/`avg_frame_rate` fields directly). For frame extraction, piping raw pixels directly to `stdout` avoids writing (and having to clean up) one temporary image file per sampled frame — `subprocess.run(..., capture_output=True)` already gives an in-memory buffer, and `numpy.frombuffer` reshapes it with zero extra I/O. Every subprocess call uses an argument array (never `shell=True`), so no path or metadata value is ever shell-interpolated.

## Open questions for team review

- `video/frames.py` currently extracts each sampled timestamp via its own `ffmpeg` subprocess invocation — correct and simple, but not the fastest possible approach for a very large sample plan (a single `ffmpeg` process emitting all requested frames via one `select`/`fps` filter graph would spawn far fewer subprocesses). Revisit if a later phase's real-model adapter needs to sample hundreds of frames per video routinely; `limits.MAX_SAMPLED_FRAMES` bounds the worst case in the meantime.
- `capability.detect_capability()`'s GPU visibility check is `nvidia-smi`-only (no AMD/Apple-Silicon-equivalent check) — acceptable for this phase since no GPU-backed analysis code exists yet to make the distinction matter, but worth revisiting once a real local model adapter is built that could actually use a non-NVIDIA accelerator.
