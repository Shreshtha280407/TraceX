# Image OCR Provenance — Raster-Image Bounding-Box Adapter (Phase 3)

## Overview

`app/modules/media_processing/ocr_adapter.py` is the single, named entry point
for all raster-image and video-frame OCR within the media-processing pipeline.
It wraps the local Tesseract binary (via `TesseractTextRecognizer`) and produces
fully typed, ordered line-level `OcrBoxResult` objects that carry:

- Recognized text
- Per-line confidence in `[0, 1]`
- Pixel bounding box in the **original source image's coordinate space**
- Normalized bbox in `[0, 1] × [0, 1]` (same `BoundingBoxNormalized` type used by detection observations)
- Source image dimensions (width, height)
- OCR engine name and version string (from the installed Tesseract binary)
- Language, config hash (deterministic from `OcrAdapterConfig`), and preprocessing version

## Coordinate Semantics

### Pixel → Normalized

The raw Tesseract output (via `recognize_regions`) returns `PixelBoundingBox`
values in the **post-preprocessing** frame. The adapter inverts any preprocessing
applied before normalization:

```
original image
  │
  ├─ EXIF orientation correction (Pillow `ImageOps.exif_transpose`)
  │    └─ produces "preprocessed" frame passed to Tesseract
  │         └─ Tesseract returns pixel bbox in preprocessed space
  │              └─ adapter inverts orientation transform
  │                   └─ original-space pixel bbox
  │                        └─ to_normalized(box, original_width, original_height)
  │                             └─ BoundingBoxNormalized
```

`OcrBoxResult.pixel_box` and `bbox_xyxy_normalized` always refer to the **original
image** (the one whose bytes arrived via `ResolvedMediaInput`).

### Normalization formula

For a box at `(x_min, y_min, x_max, y_max)` pixels in an image of `W × H` pixels:

```
normalized_x_min = x_min / W
normalized_y_min = y_min / H
normalized_x_max = x_max / W
normalized_y_max = y_max / H
```

No clamping. Coordinates are serialized from Python `float` division without
application-side rounding; Pydantic JSON serialization is deterministic for a
given input. If Tesseract produces a coordinate that doesn't satisfy
`0 ≤ normalized_x_min < normalized_x_max ≤ 1`, the result is **dropped** (same
policy as `worker.py`'s detection observation builder). `to_normalized` raises
`ProcessingError(INVALID_BOUNDING_BOX)` for invalid geometry — the caller
catches it and skips that box.

### No clamping policy

This adapter never silently fixes out-of-range normalized coordinates. A box
touching or slightly exceeding the image boundary may be valid at the pixel
level (e.g., Tesseract includes the last pixel row) and `BoundingBoxNormalized`'s
own validator rejects values outside `[0, 1]` with a hard error. Future frames
with such boxes are skipped; the rest of the job proceeds.

## OCR Source Locator Rules

### Standalone image evidence

```python
SourceLocator(bbox_xyxy_normalized=normalized_bbox)
```

- `page` is **never set** — page numbers are for document pages (Jasraj's domain)
- `frame_number`, `time_start_ms`, `time_end_ms` are **never set**

### Video-frame evidence

```python
SourceLocator(
    frame_number=frame.frame_number,  # None if frame position isn't tracked
    time_start_ms=frame.time_start_ms,
    time_end_ms=frame.time_end_ms,
    bbox_xyxy_normalized=normalized_bbox,
)
```

The media worker emits a video OCR observation only when the selected frame
has `frame_number`, `time_start_ms`, and `time_end_ms`; all three derive from
probed frame-rate metadata and selected-frame extraction. It does not emit an
unqualified video OCR observation when that precision is unavailable.

## Local-Only OCR — No Auto-Download

The adapter uses the **system `tesseract` binary** (installed via `apt-get install
tesseract-ocr tesseract-ocr-eng`). It never:

- Calls any cloud OCR API
- Downloads model weights at startup or test time
- Falls back to a different engine silently

When `tesseract` is not on `PATH`, or the requested language pack is not installed,
`ImageOcrAdapter.__post_init__` raises `OcrRuntimeError` immediately — before any
job is ever claimed. This matches `OnnxObjectDetector`'s pre-startup fail-fast
pattern.

### Setting up OCR (developer setup)

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr tesseract-ocr-eng

# Verify
tesseract --version
tesseract --list-langs    # should include 'eng'
```

For Dockerized deployments, the project's `Dockerfile` already installs
`tesseract-ocr` and `tesseract-ocr-eng`.

## Micro-Batch Flow

OCR output is delivered to Nipun's `POST /api/v1/internal/worker-jobs/{job_id}/observations`
endpoint during job processing, before the terminal result. The flow is:

```
claim → resolve input → SHA-256 → decode → OCR → batch batches → terminal result
                                                 │
                                     POST /observations × N batches
                                                 │
                                     POST /result (observations=[])
```

### Batch structure

One `ObservationBatchSubmissionV1` per chunk of `batch_size` OCR results:

```python
ObservationBatchSubmissionV1(
    job_id=..., case_id=..., evidence_id=...,
    batch_id="<job_id>-ocr-b<seq>",
    batch_sequence=<seq>,
    idempotency_key="<job_id>-ocr-ik<seq>",
    observations=[ObservationV1, ...],        # ocr_text_mention per line
    transformations=[TransformationProvenanceV1(...)],  # 1 per batch
    progress=ObservationBatchProgressV1(...),
    submitted_at=...,
    is_final_batch=<job-global final marker>,
)
```

### Transformation provenance

```python
TransformationProvenanceV1(
    step_name="image_ocr_text_extraction",  # or "video_frame_ocr_text_extraction"
    step_version="media_ocr_adapter_v1+<config_hash[:16]>",
    config_hash="<sha256(language|min_conf|psm|...)[:32]>",
    model_version="tesseract_ocr_v1",
    output_observation_ids=[...],  # only this batch's observations
    safe_metadata={
        "ocr_engine": "tesseract_ocr_v1",
        "ocr_engine_version": "5.3.0",
        "ocr_language": "eng",
        "preprocessing_version": "media_ocr_adapter_v1",
        "source_width": 1920,
        "source_height": 1080,
        "observation_count": 7,
    },
)
```

`safe_metadata` never contains: `object_uri`, `filepath`, `token`, `secret`,
`password`, `credential`, `apikey`, `api_key`, `stderr`, `stacktrace`,
`traceback`, `dsn`, `private_key`. String values are bounded at 500 characters.
This is validated by `TransformationProvenanceV1`'s own model validator before
any HTTP request is made.

### Terminal result

After all batches are submitted:

```python
WorkerResultV1(
    job_id=..., case_id=..., evidence_id=...,
    status=WorkerStatus.SUCCEEDED,
    observations=[],        # empty — delivered via batch endpoint
    ...
)
```

## Idempotency and Replay

Same evidence, source locator, OCR engine/configuration, and batch sequence
produce the same canonical OCR observation identity and idempotency tokens.
The server compares `(job_id, batch_id)` and `(job_id, idempotency_key)` and
replays the exact payload rather than creating durable duplicates or extra graph
outbox work.

## Fixture vs Real OCR

`FixtureOcrAdapter` is the test-only adapter:

- Text always starts with `FIXTURE_OCR_` — unmistakable
- Output depends only on image dimensions, never pixel content
- Must always be injected explicitly (never a silent default)
- Used only in deterministic unit/orchestration tests; never substituted for real OCR in live tests

Real `ImageOcrAdapter` is used by the real worker and by PNG/JPEG labelled-fixture
tests, and — via a labelled synthetic MP4 built by looping one such frame through
`ffmpeg` (`make_text_video_bytes`) — by a real sampled video frame too. Live OCR
tests (image and video) self-skip with `real local OCR unavailable` when the local
binary/language/font (or, for video, `ffmpeg`/`ffprobe`) is absent; they do not
report a fixture result as a real OCR pass.

## Consuming the Adapter

```python
from app.modules.media_processing.ocr_adapter import ImageOcrAdapter, OcrAdapterConfig, FixtureOcrAdapter
from app.modules.media_processing.ocr_batching import OcrBatchConfig, iter_image_ocr_batches, build_terminal_result

# Production usage (in the worker)
adapter = ImageOcrAdapter(config=OcrAdapterConfig(language="eng", min_confidence=0.3))
results = adapter.run(decoded_frame, image_metadata)

# Test usage (never requiring tesseract)
adapter = FixtureOcrAdapter(confidence=0.75)
results = adapter.run(frame, meta)

# Batch submission
for batch in iter_image_ocr_batches(results, job=job, idempotency_key_prefix=f"{job_id}-ocr", ...):
    client.submit_batch(job_id=job.job_id, claim_token=claim_token, submission=batch)

terminal = build_terminal_result(job_id=job.job_id, case_id=job.case_id, evidence_id=job.evidence_id)
client.submit_result(job_id=job.job_id, claim_token=claim_token, result=terminal)
```

## Known Limitations

| Limitation | Impact | Future mitigation |
|---|---|---|
| Tesseract line-level output | Multi-word lines grouped as one observation | Add a separately versioned word-level adapter mode |
| No rotation/deskew | Tilted text recognized poorly | Pre-processing step (not yet implemented) |
| No contrast normalization | Low-contrast or watermarked images may fail | Adaptive thresholding (PIL or OpenCV) |
| JPEG compression artifacts | May lower Tesseract confidence | Higher-quality re-encode before OCR |
| English-only default | Non-Latin scripts are unavailable until the operator installs and selects a language pack | Configure `media_ocr_language` per deployment |
| Small text (< 20px height) | Tesseract may miss it entirely | Resolution upscaling (not yet implemented) |
| Original coordinate transforms | Only EXIF orientation is implemented; crop/resize/threshold transforms need explicit inverse mappings before use | Add a versioned transform chain with mathematical tests |

## Configuration

All OCR adapter configuration comes from `Settings` (see `app/core/config.py`):

| Setting | Default | Description |
|---|---|---|
| `media_ocr_language` | `"eng"` | Tesseract language pack |
| `media_ocr_min_confidence` | `0.3` | Minimum per-line confidence |
| `media_worker_renew_interval_seconds` | `60` | Lease heartbeat interval during long jobs |

Override via environment variable or `.env`. See `.env.example`.
