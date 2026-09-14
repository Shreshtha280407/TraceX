# Phase 4 video/image worker

Owner: Gaurav. Status: **in progress**.

The worker remains the only local media processor: it claims authenticated
jobs, fetches input through the API, probes with fixed-argument local ffprobe,
extracts bounded frames with ffmpeg, runs configured local ONNX/Tesseract
adapters, and submits canonical observations. It never receives database,
object-store, graph, or broker credentials.

`media_processing.phase4` adds versioned `rapid` and `deep` profiles,
deterministic manifest/chunk planning, bounded baseline plus scene/motion-gated
sampling, and graph-safe candidate metadata. Hardware preference remains local
ONNX provider selection with CPU fallback in `auto` mode; explicit unavailable
models/tools return the existing safe failure/deferred pathways, never faked
results. Frame numbers and source-relative milliseconds are retained only when
the probe/decoder can derive them; no UTC time is invented.

Nipun owns manifest persistence, checkpoints, artifacts, and the idempotent
`/media-chunks/publish` transaction. The media client now invokes that route;
it performs no direct persistence. Shreshtha consumes the same canonical
`object_detection`, `text_region_detection`, and `anonymous_track_segment`
taxonomy through the regular graph outbox. Labels and local tracks are
evidence-local technical candidates, never identities. Jasraj's OCR utility is
the required in-memory cleanup boundary for future ROI candidate mapping; raw
OCR text, frames, paths, URIs, and secrets must never become graph attributes.

Actual GPU, model-bundle, LAN, real-media, and quality validation remain
merge-wave work.

## Phase 5B visual-source validation

`media_processing.visual_validation` is the producer-side gate for visual
signals. It records deterministic `accepted`, `rejected`, or `incomplete`
quality metadata with safe reason codes, source locator, and extractor
identity. A rejected signal is not published; an incomplete signal is retained
for review with `correlation_ready: false` and never gains fabricated timing
precision.

Video detections/OCR validate non-negative source-relative frame/time bounds,
media duration, deterministic CFR frame mapping when available, and optional
manifest chunk time/frame boundaries. Image/video geometry remains normalized
and finite (`0 <= x1 < x2 <= 1`, `0 <= y1 < y2 <= 1`) without clamping or
unrecorded transforms. OCR text regions are bounded; their frame, box, OCR
extractor, preprocessing, and quality provenance are preserved.

Tracks remain evidence-local technical labels. Their retained frame-level box
locators and explicit lifecycle conditions (`ended_unmatched`,
`split_ambiguous`, `reappearance_unlinked`) are review aids only: none
indicates identity, a relationship, or a permitted cross-evidence stitch.
