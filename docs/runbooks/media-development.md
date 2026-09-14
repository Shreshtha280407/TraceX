# Runbook: Media (Video/Image) Processing Development

`app/modules/media_processing/` (see `docs/architecture/media-processing-v1.md`, `docs/architecture/media-observation-taxonomy-v1.md`) needs no live infrastructure at all — every unit test is a deterministic in-memory test, and the integration suite needs only local `ffmpeg`/`ffprobe` binaries, not Docker Compose.

## Prerequisites

```bash
ffmpeg -version
ffprobe -version
```

If either is missing, install your platform's `ffmpeg` package (it always ships `ffprobe` alongside it). The unit suite runs regardless — every unit test monkeypatches `subprocess`/`shutil.which` rather than depending on real binaries. Only `tests/integration/media_processing/` needs them, and it self-skips cleanly (never fabricates a pass) if they're absent:

```bash
uv run pytest tests/integration/media_processing -v
```

## Running the module's tests

```bash
uv run pytest tests/unit/media_processing -v         # no ffmpeg/ffprobe dependency
uv run pytest tests/integration/media_processing -v  # real ffmpeg/ffprobe against a tiny synthetic MP4
```

## Trying it interactively

Image pipeline (no `ffmpeg` needed):

```python
from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector
from app.modules.media_processing.source import StaticBytesResolver
from app.modules.media_processing.worker import PROCESSOR_NAME_DETECTION, process_job

# construct a WorkerJobV1 + EvidenceRecordV1 with source_type=SourceType.IMAGE
# (see tests/fixtures/media_processing/factory.py), then:
result = process_job(
    job,
    evidence,
    StaticBytesResolver(payload=your_png_or_jpeg_bytes),
    detector=FakeObjectDetector(),
)
```

Video pipeline (needs `ffmpeg`/`ffprobe` on `PATH`):

```python
from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector
from app.modules.media_processing.analysis.fake_tracker import FakeObjectTracker
from app.modules.media_processing.source import StaticBytesResolver
from app.modules.media_processing.worker import PROCESSOR_NAME_DETECTION, process_job

# job.processor_name = PROCESSOR_NAME_DETECTION, evidence.source_type=SourceType.VIDEO
result = process_job(
    job,
    evidence,
    StaticBytesResolver(payload=your_mp4_bytes),
    detector=FakeObjectDetector(),
    tracker=FakeObjectTracker(),
)
```

Generating a tiny synthetic test video without any real footage (used by the integration suite itself):

```python
from tests.fixtures.media_processing.synthetic import make_synthetic_mp4_bytes

data = make_synthetic_mp4_bytes(width=64, height=48, duration_seconds=2.0, fps=5.0)
```

## Measuring performance honestly

```python
from app.modules.media_processing.performance import Stopwatch
from app.modules.media_processing.worker import process_job

sw = Stopwatch()
result = process_job(job, evidence, resolver, detector=..., stopwatch=sw)
print(sw.timings)  # probe_ms, sampling_plan_ms, frame_extraction_ms, analysis_ms,
# observation_construction_ms, total_ms, frames_requested/extracted/failed
```

`stopwatch` is a pure side-channel — it never affects the returned `WorkerResultV1`. Never report a fixed throughput claim from these numbers without also reporting the exact hardware/media characteristics they were measured against — see `docs/architecture/media-processing-v1.md`'s performance-measurement policy.

## Checking local GPU/codec capability

```python
from app.modules.media_processing.capability import detect_capability

print(detect_capability())
```

Safe to call with no GPU present — `cpu_only=True`/`gpu_visible=False` is the expected, normal result in this phase (no GPU-backed analysis code exists yet).

## Adding a *different* detector/tracker/OCR adapter

A real detector (`analysis/onnx_detector.py`, YOLOX-s via `onnxruntime`), OCR engine (`analysis/tesseract_ocr.py`, local `tesseract` via `pytesseract`), and tracker (`analysis/iou_tracker.py`) already exist and are what `worker._build_analysis_components` wires into the CLI by default — see `docs/architecture/media-processing-worker.md` for the full design, and `uv run python -m app.modules.media_processing.bootstrap_models` to fetch the detector's model asset. The guidance below is for swapping in a *different* model behind the same interfaces (e.g. a fine-tuned or specialized detector) — nothing else in the module needs to change for that either.

Implement the relevant protocol in `app/modules/media_processing/analysis/interfaces.py` (`ObjectDetector`/`ObjectTracker`/`TextRecognizer`) — nothing else in the module needs to change. Pass your instance explicitly as `worker.process_job`'s `detector`/`tracker`/`ocr` keyword; never make it a new default, per `docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md`, Decision 5. Self-report your adapter's version string in each result's `attributes["model_interface_version"]` (see `analysis/fake_detector.py` for the pattern) so it flows through to `Extractor.model_version` automatically.

## Troubleshooting

- **Integration tests all report "skipped"**: `ffmpeg`/`ffprobe` aren't on `PATH` for this shell. Confirm with `ffmpeg -version`; install and re-run.
- **`ffmpeg_unavailable`/`ffprobe_unavailable` from the worker at runtime**: same as above — this module checks `shutil.which` itself and fails safely rather than letting a raw `FileNotFoundError` escape.
- **A detection never produces an observation**: check `provenance.is_valid_confidence` (must be a finite value in `[0, 1]`) and the box's normalized geometry (`image/geometry.to_normalized` — must be non-degenerate and within `[0, 1]`). This module skips malformed model output rather than raising — see ADR-004, Decision 2.
- **Real footage/CCTV content**: never commit it. All fixtures under `tests/fixtures/media_processing/` are synthetic (solid-color PNGs, `ffmpeg lavfi testsrc`-generated MP4s) — see `docs/qa/test-data.md`.
# Phase 4 profile planning

Use `RAPID_PROFILE` for bounded initial inspection and `DEEP_PROFILE` only when
the configured local detector/OCR toolchain is available. The worker must use
its existing credential/claim token and `publish_media_chunk`; do not call a
database, graph, object store, or publication endpoint without that boundary.
Model bundles remain external to Git. `auto` device selection may fall back to
CPU; an explicitly unavailable model/tool is a safe deferred/failure outcome.

The existing CLI selects this through `MEDIA_PROCESSING_PROFILE=rapid|deep`
(default `rapid`). Both profiles permit a configured OCR adapter; `deep` uses
denser sampling and a larger bounded OCR-region allowance. No separate
profile-specific command or worker credential exists. For example:
`MEDIA_PROCESSING_PROFILE=deep uv run python -m app.modules.media_processing.worker --once`.
