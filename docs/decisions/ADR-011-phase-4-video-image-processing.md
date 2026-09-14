# ADR-011: deterministic local video/image processing profiles

TraceX adopts explicit `rapid` and `deep` profile records rather than implicit
sampling or hardware choices. Their hashes cover cadence, bounded chunk/frame/
OCR/batch limits, thresholds, detector/tracker identity, and decode preference.
Equivalent metadata and profile values build the same Nipun manifest; scene and
motion signals only add bounded, sorted source-relative samples.

The worker uses local ffprobe/ffmpeg and configured local detector/OCR assets.
Missing tools/models are safe unavailable outcomes; no runtime downloads or
cloud services are permitted. Detection labels, local tracks, and OCR
identifiers are unresolved candidate evidence only. Publication stays on
Nipun's authenticated idempotent route and graph projection stays asynchronous.
