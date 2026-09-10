# Audio, Social/Chat, and Communication-Link Processing (Sarthak Phase 1)

`app/modules/communication_processing/` turns three safe audio input roles (metadata, imported transcript segments, imported diarization segments) and four documented local social/chat export formats into canonical `ObservationV1` objects, plus separately produces deterministic, review-only communication-link candidates. Full alias/transliteration design lives in `docs/architecture/multilingual-alias-candidates-v1.md`. Design rationale lives in `docs/decisions/ADR-005-provenance-first-communication-processing.md`.

## Supported input boundaries

Audio processing accepts exactly three roles (`models.InputPayload`):

- `audio_metadata` — local WAV bytes, inspected directly.
- `transcript_segments` — already-produced transcript segments, imported and validated.
- `diarization_segments` — already-produced diarization segments, imported and validated.

Social/chat processing accepts exactly four documented local export shapes: WhatsApp plain-text, Telegram Desktop JSON, Instagram message JSON, and a generic social-intelligence JSON record export. No platform API, web scraper, credential handling, decryption, HTML parser, browser automation, attachment download, or real-time polling exists anywhere in this module — every parser operates on bytes the caller already has.

## Import of transcript/diarization output vs. real ASR/diarization

This module **never runs ASR or diarization**. `audio/transcript_import.py` and `audio/diarization_import.py` only validate and normalize segments a caller already produced through some approved external process (entirely out of scope for this phase — see CLAUDE.md and the explicit non-goals below). If a job asks for a transcript or diarization result (`transcript_import_v1`/`diarization_import_v1`) but is only given raw audio bytes (`AudioMetadataInput`) instead of already-produced segments, `worker.process_job` returns `WorkerStatus.DEFERRED` with a checkpoint naming which capability is missing (`deferred_requires_asr`/`deferred_requires_diarization`) — **never** `SUCCEEDED` with fabricated text or speaker turns. See `audio/routing.py`'s `AudioRoutingDecision` enum for the complete set of routing outcomes.

## Exact provenance rules

Every observation's `source_locator` uses the field that actually locates it in its source:

| Source | Locator fields used |
|---|---|
| WAV metadata | `time_start_ms=0`, `time_end_ms=<duration in ms>` — the fact describes the whole file's time range |
| Transcript segment | `time_start_ms`, `time_end_ms` (from the segment itself) + `json_path` (positional index into the supplied segment list) |
| Diarization segment | `time_start_ms`, `time_end_ms` + `json_path` |
| WhatsApp message | `span_start`/`span_end` — exact character offsets into the decoded export text |
| Telegram/Instagram/generic-JSON message | `json_path` — exact array index, e.g. `$.messages[3]` |

`ObservationV1.event_time` is set from a message's `timestamp_utc` *only* when the source gave an unambiguous absolute timestamp (see the timezone policy below) — never guessed, and never set at all for transcript/diarization segments (their `start_ms`/`end_ms` are offsets relative to the audio file, not calendar time).

## Source-local speaker labels vs. identities

`diarization_speaker_turn` observations carry `speaker_label` (e.g. `SPEAKER_00`, `unknown_speaker_1`) as an `ExtractedEntityMention` with `entity_type_hint="speaker_label_local"` — deliberately not `"person"` or anything implying verification. A label is meaningful only within the one evidence file it came from; this module never combines labels across two different evidence files (each call to `diarization_segments_to_mentions` processes exactly one evidence file's segments, statelessly, with no shared state between calls) and never claims speaker recognition or voice-biometric verification. Overlapping turns (legitimate for cross-talk) are preserved exactly as supplied, never merged or silently dropped.

## Supported export shapes and parser limits

Each platform parser (`social/whatsapp.py`, `social/telegram.py`, `social/instagram.py`, `social/json_records.py`) documents its exact accepted shape in its own module docstring. All four share the safety limits in `limits.py`: export byte size, line count, message count, JSON nesting depth, participant count, and per-message text length. Formula/code/template evaluation never happens anywhere — chat text and JSON values are treated as inert data, never interpreted.

**Timezone policy** (identical across all four parsers): a timestamp is normalized to UTC (`timestamp_utc`) only when its source is unambiguous *by construction* — an explicit UTC offset/`Z` suffix (generic JSON), or a Unix epoch value (Telegram's `date_unixtime`, Instagram's `timestamp_ms` — both UTC by definition, nothing to guess). WhatsApp's export carries no timezone at all, so its `timestamp_utc` is always `None`. A naive/ambiguous timestamp is never guessed at: `timestamp_raw` is always preserved regardless.

**Privacy**: no raw message content, handle, phone number, or token is ever passed to a logger anywhere in this module (there is no logging call in the module at all — the same "don't create the risk" posture `structured_processing` uses). `errors.ProcessingError` messages describe only *what kind* of problem occurred (a field name, a limit constant), never the offending value.

## What becomes `ObservationV1`

Every audio-metadata fact, transcript segment, diarization turn, and chat message becomes exactly one `ObservationV1`, with small, bounded, allow-listed `attributes` — never a whole file, whole transcript, whole export, or whole conversation embedded in one observation. A message is not an `EntityV1`, `EventV1`, verified identity, graph relationship, guilt claim, or hypothesis.

## Alias/transliteration candidate policy

Summarized here; full design in `docs/architecture/multilingual-alias-candidates-v1.md`. `aliases/` produces conservative, deterministic, fully-tested candidates (`exact_normalized`, `deterministic_transliteration`, or `not_generated`) — never ML transliteration, fuzzy matching, or external corpora — and never merges or asserts identity from them.

## Communication-link candidate policy

`linking/` produces `CommunicationLinkCandidate`s for exactly five deterministic reasons (`same_source_conversation`, `explicit_reply_reference`, `explicit_message_reference`, `same_normalized_phone_token`, `same_exact_handle_token`), always `status=PROPOSED_FOR_REVIEW`. `LinkableMessageDescriptor` (the only input this logic ever sees) has no display-name/alias field at all — a name- or transliteration-similarity link is not just disallowed by policy, it is structurally impossible to construct here. No probabilistic score is ever computed, and no temporal-proximity link type exists in this phase. See ADR-005.

## Case isolation

Every `LinkableMessageDescriptor`-consuming function in `linking/deterministic.py` requires every descriptor in a single call to share one `case_id`; a mixed-case batch is rejected outright (`cross_case_input_rejected`), never silently split or partially processed. Every `ObservationV1` this module emits carries `job.case_id`/`job.evidence_id` directly from the `WorkerJobV1` it was built for.

## What is deliberately not written to Neo4j

Nothing in this module writes to Neo4j, PostgreSQL, Redis, MinIO, or any queue — it has no import of any such client (statically verified, `tests/unit/communication_processing/test_module_safety.py`). It returns typed Python data (`ObservationV1`, `WorkerResultV1`, `CommunicationLinkCandidate`) for a later-phase orchestrator to persist, exactly the same boundary `structured_processing` and `graph` sit behind.

## Worker defer/failure semantics

`worker.process_job(job, input_payload)` is a pure function: no side effects, no shared mutable state, no database/network/subprocess access. Only `errors.ProcessingError` is caught and converted to `WorkerResultV1(status=FAILED, error=...)`; any other exception is a bug and propagates. `DEFERRED` is used specifically for the "real capability not available yet" case (ASR/diarization requested from raw audio only); everything else that can't be processed (unsupported audio format, malformed export, wrong processor/source-type/input-role) is `FAILED` with a named, safe error code. **Idempotency**: given the same job identity and the same normalized input, every emitted `observation_id` is identical across repeated calls (see `provenance.observation_id`) — a retry or replay never produces duplicate-but-differently-identified observations.

## Explicit non-goals (this phase)

No Whisper/faster-whisper/any ASR model; no pyannote/NeMo/SpeechBrain/voice biometrics/real diarization; no audio enhancement, translation, summarization, or sentiment analysis; no NER/LLM extraction, embeddings, or fuzzy identity matching; no automatic entity resolution or alias/speaker/person merges; no graph writes, analytics, scoring, correlation engine, or hypothesis generation; no live social-platform APIs, scraping, browser automation, decryption, or media downloads; no uploads, MinIO writes, case CRUD, auth changes, or frontend.
