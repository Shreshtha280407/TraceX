# Phase 4 media graph mapping and temporal semantics

Owner: Shreshtha. Status: **in progress**.

## Boundary and delivery

This mapping consumes only a canonically persisted `ObservationV1` after the normal `graph_projection_jobs` outbox row exists. Nipun's media-chunk publish transaction remains the sole producer: it persists the canonical batch, observation, transformation provenance, chunk/artifact/checkpoint metadata, and normal outbox row together. The graph worker later reads that row and performs asynchronous Neo4j `MERGE` writes. No request handler, worker, or extractor writes Neo4j.

The graph worker optionally reads the already-persisted `media_chunk_observations -> media_chunks -> media_chunk_manifests` lineage. It copies only chunk/manifest/artifact identifiers and processor/configuration versions. Object URIs, raw bytes, transcripts, chat exports, credentials, and checkpoint payloads never enter Neo4j.

## Vocabulary

All specialised objects reuse the existing case-scoped `TemporalEvent` label and `PROJECTS_EVENT` relationship; no parallel media graph is introduced.

| Canonical observation | Temporal event | Participant treatment |
| --- | --- | --- |
| `object_detection`, `text_region_detection`, `anonymous_track_segment` | `sighting` | `detected_label` is an evidence-local `SourceClaim(visual_label)`, never an entity or identity. |
| `transcript_segment`, `diarization_speaker_turn` | `speech_segment` | A diarization label is a `SourceClaim(speaker_label_local)` and existing `EntityMention`, never a verified speaker. Transcript text is never copied. |
| `chat_message` | `message` | Sender/participants are `SourceClaim(platform_handle_claim)` values only; no relation is inferred from co-occurrence. |
| explicit `meeting_candidate` | `meeting_candidate` | Requires `candidate_status=candidate` and a bounded `candidate_reason_category`; it remains `candidate_only=true`. |

Every event starts from a same-case `Evidence -> Observation` match and carries case/evidence/observation IDs, extractor name/version/config/model values, mapping version/config hash, extraction confidence, and its bounded source locator. `Sighting` bbox, frame, and media offsets; `SpeechSegment` offsets; and `Message` JSON path/message ID are duplicated as allow-listed event properties for review while the canonical observation remains authoritative.

## Time policy

- An aware canonical `event_time` is stored as an exact instant; no conversion is performed by this mapping.
- A canonical `TimeWindow` retains supplied start/end independently. A one-sided window is labelled `partial_window`; both bounds are `bounded_window`.
- `time_start_ms`/`time_end_ms` are source-relative offsets and are stored as `source_time_*_ms`; they are never converted to UTC. Frame numbers are preserved but never converted through an assumed FPS.
- The frozen contracts reject reversed absolute, millisecond, and bbox ranges before persistence. Missing temporal data produces an event with `temporal_precision=unknown`, not an invented instant. An instant outside a supplied absolute window is preserved with `temporal_conflict=true`.
- Chat timezone metadata is copied only when the canonical producer supplied its bounded `timestamp_source_timezone`; it never becomes a new UTC claim.

## Candidate and replay policy

`meeting_candidate` accepts only explicitly upstream candidate evidence. Its optional contributing and contradictory observation IDs are matched only under the same `case_id`, with review-only `SUPPORTED_BY_OBSERVATION` edges. This layer neither finds meetings, scores temporal overlap, resolves identities, nor changes a candidate to a fact.

Projection IDs derive from case, observation, Phase 4 mapping version, and a canonical fingerprint. The existing `(case_id, projection_id)` constraint and `MERGE` queries make a replayed chunk/outbox delivery idempotent. A completed chunk never mutates an earlier observation's mapping: a new canonical observation has a different observation/projection identity. Partial observations remain projectable even if their larger job later becomes `deferred` or `failed`.

## Phase 5 compatibility

Phase 5 still begins from the canonical same-case `Evidence-[:YIELDED_OBSERVATION]->Observation` path and owns its separate correlation outbox, scoring, analytics, and `Correlation` nodes. These events are evidence-backed temporal inputs only; this layer creates no correlation, timing assertion, direct entity edge, merge, or hypothesis.
