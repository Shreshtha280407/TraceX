# Media Observation Taxonomy v1

Owner: Gaurav. Status: **versioned taxonomy** for `app/modules/media_processing/`'s `ObservationV1` output.

**This document does not modify any frozen `V1` contract.** `ObservationV1`, `WorkerJobV1`, `WorkerResultV1`, and `app/contracts/common.py`'s `SourceLocator`/`BoundingBoxNormalized`/`Extractor` are unchanged. `observation_type` remains a plain, unconstrained string at the contract level exactly as documented in `docs/architecture/phase-1-decisions.md` — the values below are this module's own convention, not new contract validation.

## Observation types

| `observation_type` | Modality | Meaning |
|---|---|---|
| `media_metadata` | video, image | Safe, directly-probed/decoded media metadata for one evidence item. |
| `object_detection` | video, image | One anonymous detection (a `person`/`vehicle`/etc. bounding box) in one frame/image. |
| `text_region_detection` | video, image | One detection whose label is specifically `text_region` — a candidate area for OCR, not yet OCR'd. |
| `anonymous_track_segment` | video only | One local, anonymous cross-frame association of detections sharing a label. |
| `ocr_text_mention` | video, image | Recognized text from one explicitly-selected crop of a `text_region_detection`. |

Every one of these is produced by `provenance.draft_to_observation`, which is the single, shared conversion point from an internal `MediaObservationDraft` to a canonical `ObservationV1` — mirroring `structured_processing.provenance.mention_to_observation`'s role for document/structured sources.

## Required provenance per type

### `media_metadata`

- **Locator**: `SourceLocator(time_start_ms=0)`. `SourceLocator` has no field that means "the whole file" — `time_start_ms=0` is this module's deliberate, documented convention for a file-level locator, used identically for both video and image (see ADR-004).
- **Attributes** (video): `media_width`, `media_height`, `container_format`, `duration_ms`, `frame_rate`, `frame_count`, `video_codec`, `has_audio`, `rotation_degrees` — the small, already-validated scalar fields of `VideoMetadata`, never a raw `ffprobe` JSON dump.
- **Attributes** (image): `media_width`, `media_height`, `format`, `color_mode`, `orientation` — the scalar fields of `ImageMetadata`.
- **Confidence**: `1.00` (directly probed/decoded, not a model output).

### `object_detection` / `text_region_detection`

- **Locator** (video): `frame_number` (when trustworthy, else `None`), `time_start_ms`, `time_end_ms`, `bbox_xyxy_normalized`.
- **Locator** (image): `bbox_xyxy_normalized` only — an image has no frame/time axis.
- **Attributes**: `detected_label`, `media_width`, `media_height`, and (video only) `sampling_strategy`.
- **Confidence**: the detector's own reported value, validated `∈ [0, 1]` before an observation is ever built from it (`provenance.is_valid_confidence`).
- A detection whose `label == "text_region"` becomes `text_region_detection`; every other label becomes `object_detection`. This is the only branching rule — there is no separate code path per label beyond it.

### `anonymous_track_segment` (video only)

- **Locator**: `time_start_ms`/`time_end_ms` spanning the track's first and last matched detection — no single bounding box, since the track spans multiple frames.
- **Attributes**: `local_track_id`, `detected_label`, `media_width`, `media_height`, `sampling_strategy`, and `track_boxes` — a bounded list (at most `SamplingRequest.max_frames` entries) of `{"time_ms": ..., "bbox_xyxy_normalized": {...}}`, each entry small and JSON-safe.
- **Confidence**: the tracker's own reported `quality` (local cross-frame association quality only), validated `∈ [0, 1]`.
- **`discriminator`**: `local_track_id` — so two tracks that happen to share identical start/end timestamps (a rare but not impossible coincidence) still get distinct observation IDs.

### `ocr_text_mention`

- **Locator**: identical shape to the `text_region_detection` it was cropped from (video: `frame_number`/`time_start_ms`/`time_end_ms`/`bbox_xyxy_normalized`; image: `bbox_xyxy_normalized` only) — the recognized text's location *is* the text region's location, so no separate crop-local geometry is recorded.
- **`extracted_entities`**: exactly one `ExtractedEntityMention(text=recognized_text, entity_type_hint="ocr_text")` — the only observation type in this module that populates `extracted_entities`, mirroring how `structured_processing` reserves it for mention-shaped (not record-shaped) observations.
- **Attributes**: `media_width`, `media_height`, and (video only) `sampling_strategy`.
- **Confidence**: the recognizer's own reported value, validated `∈ [0, 1]`.

## Why raw media bytes never appear in `attributes`

Every attribute value above is a small scalar (a label, a count, a dimension, a short recognized-text string) or a bounded small list (`track_boxes`). No observation attribute ever holds a whole frame, a whole image, raw video bytes, or a raw OCR crop — those are transient runtime artifacts (`models.Frame`/`models.ExtractedFrame`) that never cross into the `ObservationV1` boundary. This is a hard rule, not an optimization: an `ObservationV1` is meant to be small, queryable, evidence-grade metadata, not a bytes container.

## Anonymous tracking is not identity

`TrackSegment.local_track_id` (and the `local_track_id` attribute on `anonymous_track_segment` observations) is valid only inside the one evidence item and one processing run that produced it. It is never a person or vehicle identity, is never compared across evidence items or cases by this module, and must never be treated as one downstream — this is the same "no automatic identity merge" boundary `docs/architecture/graph-taxonomy-v1.md` establishes for entity resolution generally, applied here at the point tracking output is first produced. See ADR-004 for the full rationale and `tests/unit/media_processing/test_analysis_fakes.py` for the behavioral check.

## Deferred to a later phase

Face recognition, person re-identification across videos/cameras, biometric identification, cross-camera identity matching, and any correlation of `local_track_id` values across evidence items are all explicitly out of scope for this phase — see `CLAUDE.md` and `docs/qa/known-limitations.md`.
