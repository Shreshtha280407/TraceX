# Communication Processing (Phase 3 — Sarthak)

`app/modules/communication_processing/` turns approved audio and social/chat
evidence into provenance-rich canonical `ObservationV1` micro-batches through
the existing secure worker lifecycle. This document is the Phase 3 capstone
reference; it assumes and does not repeat the Phase 1 design
(`docs/architecture/audio-social-and-communication-processing-v1.md`) or the
Phase 2 worker CLI (`docs/architecture/communication-processing-worker.md`) —
read those first for the parts unchanged here. The concrete decisions and
their reasoning live in `docs/architecture/phase-3-decisions.md`'s Sarthak
section; this document is the "what/where," that one is the "why."

## What's new in Phase 3

1. **Micro-batch submission** through Nipun's `POST /{job_id}/observations`,
   instead of bundling every observation into one terminal result.
2. **A documented default chat timezone** for a naive timestamp with no
   explicit signal.
3. **Mentioned-identifier extraction** (`phone_number`/`email_address`/
   `url`/`username_or_handle`) from chat message text.
4. **Sender-name transliteration candidates**, wired as an observation
   attribute via the pre-existing (Phase 1) `aliases/transliteration.py`.
5. **A typed ASR/diarization adapter boundary** — still no real model; the
   absence is now a documented `Protocol`, not an implicit one.

Everything else (WAV-only audio, the four chat export parsers, the
transliteration algorithm itself, `linking/deterministic.py`, worker
identity/security, case isolation) is unchanged from Phase 1/2 and is not
repeated here.

## Supported profiles and worker/source-type routing

Unchanged from Phase 2 — all seven processors are reachable through a real
evidence upload:

| `SourceType` | Routes to | Emits |
|---|---|---|
| `audio` | `audio_metadata_v1` | `audio_metadata` |
| `audio_transcript` | `transcript_import_v1` | `transcript_segment` |
| `audio_diarization` | `diarization_import_v1` | `diarization_speaker_turn` |
| `chat` | `generic_social_json_v1` | `chat_message` (+ mentioned identifiers) |
| `whatsapp_chat` | `whatsapp_export_v1` | `chat_message` (+ mentioned identifiers) |
| `telegram_chat` | `telegram_export_v1` | `chat_message` (+ mentioned identifiers) |
| `instagram_chat` | `instagram_export_v1` | `chat_message` (+ mentioned identifiers) |

See `docs/architecture/communication-processing-worker.md`'s "Routing
boundary" for the routing-table source of truth
(`evidence_lifecycle/routing.py`) and `docs/qa/known-limitations.md` for the
Phase 2 fix that made all seven reachable.

## Micro-batch submission

`worker.run_communication_job_with_batches` (new; `run_once` calls it instead
of the old `process_job` + single `/result` path) reuses `_dispatch` (Phase
1/2, completely unchanged) to build the full `list[RawMention]` for a job,
then submits it through `client.submit_batch` in bounded chunks
(`Settings.communication_batch_size`, default 200), before submitting exactly
one terminal `WorkerResultV1` with `observations=[]` — the observations were
already delivered. `process_job` itself is untouched and still callable
directly (e.g. by a test, or a future in-process caller that wants one
synchronous result rather than a stream of batches).

- **Deterministic `batch_id`/`idempotency_key`**: both are pure functions of
  `(job_id, batch_sequence)` (`batching.py`, mirroring
  `structured_processing.batching` exactly) — a retried HTTP call for the
  same logical batch always reuses the same two tokens, so the server's own
  idempotent-replay rule works with no retry-state tracking in this module.
- **Lease renewal**: every 5 batches, `client.renew_lease` is called
  best-effort (a failure there is not fatal to the job — the next batch
  submission or the terminal result will still be attempted).
- **Exactly one terminal result**: `run_communication_job_with_batches`
  always ends with one `WorkerResultV1` — `SUCCEEDED` (all batches
  submitted), `DEFERRED` (no real ASR/diarization capability for this job —
  zero batches submitted, nothing to report), or `FAILED` (a `ProcessingError`
  raised during dispatch, before any batch was ever built).
- **Observation-ID collision handling**: `worker._observations_for` gives
  every mention a stable `observation_id` via `provenance.observation_id`
  (unchanged), except when two mentions in the same call legitimately share
  `(observation_type, locator)` — see "Mentioned-identifier extraction"
  below — in which case a positional `discriminator` disambiguates them. For
  every profile that existed before this phase, this is a no-op: their
  mentions never collide, so the discriminator is always `""` and their
  `observation_id`s are byte-identical to before.

## Chat timezone policy (updated this phase)

A chat message's timestamp resolves to UTC using, in order:

1. **An explicit, unambiguous signal carried by the source itself** — a UTC
   offset/`Z` suffix on an ISO-8601 string (generic JSON, Telegram's `date`
   field when parsed as a fallback), or a Unix epoch value (Telegram's
   `date_unixtime`, Instagram's `timestamp_ms` — both UTC by definition).
   `timestamp_source_timezone` is `None` — there is no assumption to record.
2. **Otherwise, `Settings.communication_default_timezone`** (default
   `Asia/Kolkata`) — applied to an otherwise-naive local timestamp
   (WhatsApp, always naive; Telegram, when `date_unixtime` is absent;
   generic JSON, a naive ISO string). `timestamp_source_timezone` records
   the resolved zone name, so a default-derived UTC value is never
   indistinguishable from a genuinely explicit one.
3. **Unparseable**: `timestamp_utc` and `timestamp_source_timezone` both
   stay `None`. `timestamp_raw` always preserves the untouched original
   string regardless of which of the three paths was taken.

This exactly mirrors `structured_processing.structured.cdr`'s identical
policy for CDR/finance records (`Settings.structured_default_timezone`,
same default) — both modules process the same class of India-context
evidence and never silently interpret an ambiguous date in the host
machine's own local time.

Instagram is unaffected: `timestamp_ms` is always present and always
unambiguous, so it never reaches the default-timezone fallback.

## Mentioned-identifier extraction

`social/identifiers.py::extract_mentioned_identifiers` runs four fixed,
deterministic regex patterns over one chat message's text — no NLP, no
LLMs, no fuzzy matching, mirroring `structured_processing.document.
fir_report`'s regex-only approach:

| Observation type | Pattern | Documented limitation |
|---|---|---|
| `phone_number` | Indian mobile only (`[6-9]\d{9}`, optional `+91`/`0` prefix) | No landline/international formats — same limitation as `fir_report.py` |
| `email_address` | `local-part@domain.tld` | — |
| `url` | `http(s)://...` or `www....`, trailing sentence punctuation trimmed | Not a strict RFC 3986 parse |
| `username_or_handle` | A bare `@name` token, 2-32 word characters | No platform-specific username-format validation |

Each match becomes its own `RawMention`, reusing the parent message's own
locator (`message_id`/`json_path`/`conversation_id` context) — the message
*is* the accurate source location for a value found in its text; there is
nothing more precise inside one message to independently anchor a locator
to. When two matches of the same type occur in one message (e.g. two phone
numbers), `worker._observations_for`'s discriminator (above) gives each its
own stable `observation_id` despite the shared locator.

Two different observation types may legitimately reference overlapping or
identical substrings (e.g. an email address embedded inside a matched URL)
— each pattern's output stands on its own; this module never attempts
cross-pattern overlap resolution, exactly like `fir_report.py`'s own
precedent.

Every extracted value is an **extracted claim about text in one message** —
never a verified identity, never a graph edge, never merged with any
`EntityV1`.

## Sender-name transliteration candidates

`social/common.py::chat_message_to_mention` attaches
`sender_transliteration_candidates` to every `chat_message` observation
that has a sender: one candidate per whitespace-separated word in the
sender field (via the pre-existing, unmodified
`aliases/transliteration.py::generate_candidates_for_tokens` — its
algorithm operates on one token at a time, so a multi-word name is split
first rather than run through as one unmappable blob). Each candidate
carries `original_text`, `script`, `candidates` (may be empty),
`method`/`method_version`, `category`
(`exact_normalized`/`deterministic_transliteration`/`not_generated`), and
`reason` — see `docs/architecture/multilingual-alias-candidates-v1.md` for
the full transliteration design (Devanagari/Gurmukhi consonant+matra+virama
algorithm, phone/email/URL/handle exclusion, NFC normalization).

**This is not identity resolution.** A candidate is a review-only,
normalized/transliterated *representation* of the original text — never
attached to `EntityV1.aliases` automatically, never used for fuzzy matching
or similarity scoring, and never interchangeable with the original. The
sender's raw value is always preserved unchanged alongside its candidates
(`sender` attribute, bounded to `MAX_HANDLE_LENGTH`).

`sender` is bounded to `MAX_HANDLE_LENGTH` (300 characters) before use —
both for the raw attribute and as transliteration input — since some
platform parsers (Telegram, Instagram, generic JSON) read it from an
otherwise-unbounded JSON string field; this is a truncation, never a
rejection, so a pathologically long sender field can never fail the whole
message.

## ASR/diarization: still no real model, now a typed boundary

`audio/asr_adapter.py` and `audio/diarization_adapter.py` define an
`AsrAdapter`/`DiarizationAdapter` `Protocol` each, with exactly one
production implementation apiece (`UnavailableAsrAdapter`/
`UnavailableDiarizationAdapter`) that always reports its `state` as
`UNAVAILABLE` and always raises rather than fabricate a result if called.
Neither is wired into `worker.py`'s dispatch — `audio/routing.py`'s existing
`AudioRoutingDecision.DEFERRED_REQUIRES_ASR`/`DEFERRED_REQUIRES_DIARIZATION`
outcomes (Phase 1, unchanged) already defer *before* any adapter would be
reached. This phase gives that existing, correct behavior a documented,
typed shape rather than an implicit absence — see
`docs/architecture/phase-3-decisions.md` for why a real local ASR/
diarization implementation was judged out of scope (this module's own
`test_module_safety.py` bans every practical ML toolkit for it).

An explicit, clearly-labeled **fixture/test-only** adapter exists for each
(`tests/fixtures/communication_processing/{asr,diarization}_fixture_
adapter.py`) — never imported by `app/`, proving the `Protocol` is
genuinely implementable without ever presenting fixture output as a real
result (`model_name` is always an obvious fixture marker, e.g.
`"fixture_asr_adapter"`).

**Diarization segment-source tagging**: every `diarization_speaker_turn`
observation now carries a `segment_source` attribute, always
`"metadata_supplied"` today (`audio/diarization_adapter.py`'s
`SEGMENT_SOURCE_METADATA_SUPPLIED` constant) — `diarization_import.py` only
ever imports externally-produced segments; no adapter in this phase ever
reports `READY`, so no segment is ever `"model_derived"` yet. The attribute
exists so a future real adapter's output is distinguishable from an
imported one without an observation-shape change.

## Source-locator policy (unchanged, restated for completeness)

See `docs/architecture/audio-social-and-communication-processing-v1.md`'s
table — audio/transcript/diarization locators use `time_start_ms`/
`time_end_ms` (+ `json_path` for transcript/diarization); WhatsApp uses
`span_start`/`span_end` (exact character offsets into the decoded export
text); Telegram/Instagram/generic-JSON use `json_path` (exact array index).
Every mentioned-identifier observation (new this phase) reuses its parent
message's own locator unchanged.

## Original-text vs. normalized-text policy

A chat message's `text` attribute is the original message text (bounded,
truncated via `truncate_text`, never re-encoded or rewritten). A sender's
`original_text` inside each transliteration candidate is likewise the
untouched source string — NFC-normalized only for script detection and
comparison-key purposes internally, never destructively rewritten in what's
stored. `timestamp_raw` is always the untouched original timestamp string,
independent of whatever UTC value (if any) was resolved from it.

## Malformed-input handling policy

- **Audio**: a corrupt/unsupported file fails safely and deterministically
  (`invalid_wav`/`unsupported_audio_format`), never partial/fabricated
  output — unchanged from Phase 1 (`audio/routing.py`).
- **Chat exports**: a genuinely unparseable export (not valid JSON/UTF-8,
  wrong top-level shape, zero recognizable message lines) fails the whole
  job with a named `ProcessingError` (`malformed_chat_export`). Within an
  otherwise-valid export, an individual entry missing required fields or of
  the wrong type is skipped (continue-on-error at the row/message level) —
  unchanged from Phase 1; this phase did not change parser return shapes
  (considered adding a `skipped_count` wrapper type, rejected: high blast
  radius across dozens of existing tests that index/iterate parsers' bare
  `list[ChatMessageRecord]` return value directly — see
  `docs/architecture/phase-3-decisions.md`).
- **Mentioned-identifier extraction**: bounded per message
  (`MAX_IDENTIFIER_MATCHES_PER_MESSAGE` per observation type) — a
  pathological message crafted to repeat a matchable token thousands of
  times can't inflate one message into an unbounded number of observations.

## Safe provenance metadata

Every batch's `TransformationProvenanceV1` (`worker._TRANSFORMATION_STEP_NAMES`
maps each of the seven profiles to a safe, named step, e.g.
`whatsapp_export_parsing`) records: processor name/version
(`build_extractor`), `config_hash` (`profile_config_hash`), batch
sequence/ordinal, and `output_observation_ids` — restricted to that same
batch's own observations, never a dangling cross-batch reference (enforced
at the contract level, unchanged from Nipun's Phase 3 work). It never
records raw transcript/chat text, an object URI, a claim token, or a
worker/API credential — the frozen `TransformationProvenanceV1.safe_metadata`
validator (Nipun's Phase 3 work, unchanged) rejects secret-shaped keys and
overlong string values before any HTTP request even reaches the service.

## Test fixture provenance

All audio/chat fixtures used by this module's tests are synthetic,
hand-constructed, and non-sensitive (`tests/fixtures/communication_
processing/builders.py`, `factory.py`) — no real evidence, no real personal
data, no data sourced from any external corpus. The ASR/diarization fixture
adapters (above) return exactly the segments they were constructed with in
a test — never a measured or benchmarked real-model result, since no real
model exists in this phase to measure. No precision/recall/model-performance
figure is claimed anywhere in this module's docs or test output for that
reason.

## Known limitations and deferred work

See `docs/qa/known-limitations.md` for the full, dated list. Summary
relevant to this phase:

- No real local ASR or diarization model (unchanged from Phase 1/2; now a
  typed `Protocol` boundary instead of an implicit absence — see above).
- Only WAV is supported for local audio inspection (unchanged; MP3/M4A/AAC/
  OGG/FLAC/WMA/Opus all route to `unsupported_audio_format`) — extending
  this would require either reversing this module's own `subprocess` import
  ban or expanding `media_processing`'s own audio-decoding capability
  (Gaurav's module, out of scope for this task).
- `username_or_handle` extraction has no platform-specific validation (a
  bare `@name` token is accepted regardless of whether it's a plausible
  handle on any real platform).
- No live-model benchmark exists for ASR/diarization extraction quality,
  since no real model runs in this phase.
- No attachment-reference observation type exists yet — none of the four
  chat parsers read or model any attachment-related field. A genuinely new
  capability, judged out of scope this phase; flagged for team review.
