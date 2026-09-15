# Phase 4 audio and social worker

Owner: Sarthak. Status: **in progress**.

`communication_processing.phase4` supplies deterministic rapid/deep audio
profiles, Nipun-compatible audio chunk manifests, ordered bounded VAD interval
normalization, selective diarization policy, and Unicode script hints. The
separate executable `audio.local_pipeline` normalizes authenticated-worker-
delivered PCM WAV evidence to 16 kHz mono PCM, applies deterministic energy
VAD, and processes bounded source-relative windows.

`LocalCommandAsrAdapter` and `LocalCommandDiarizationAdapter` are offline
bridges for an operator-installed executable (for example a local whisper.cpp
or diarization wrapper). The fixed no-shell arguments are `--model <path>` and
`--input <temporary-wav>` (plus ASR `--language <hint>`); the bridge emits
strict timestamped JSON on stdout. Configure local assets only with
`COMMUNICATION_ASR_COMMAND`, `COMMUNICATION_ASR_MODEL_PATH`,
`COMMUNICATION_DIARIZATION_COMMAND`, and
`COMMUNICATION_DIARIZATION_MODEL_PATH`. No asset is in Git or downloaded at
runtime. Missing paths create a truthful deferred outcome with no transcript
or speaker turn.

The worker CLI selects `COMMUNICATION_AUDIO_PROFILE=rapid|deep`; `--profile`
overrides it for one `--once` invocation. Rapid skips diarization. Deep runs it
only after the existing sufficient-speech policy and only with a ready local
bridge. Diarization failure preserves valid ASR output.

Existing local social parsers execute through the claimed-job worker and remain
the approved WhatsApp, Telegram,
Instagram, and generic JSON export paths, preserving message ID/JSON path,
timestamp timezone policy, unresolved sender/handle metadata, and bounded
transliteration candidates. Raw message/export text is reduced to a bounded
length and SHA-256 commitment in graph-facing attributes; extraction happens
inside the worker before publication.

Source-relative audio milliseconds are never converted to UTC. VAD silence is
successful. Speaker labels are source-local technical labels only. Raw audio,
transcripts, exports, URIs, and credentials never enter logs or graph-facing
properties. Every speech/turn observation carries profile, manifest/chunk,
backend/model identity, and configuration hashes. Workers publish canonical
partial observations only through Nipun's authenticated idempotent batch route.
Durable coordinator manifest creation and checkpoint persistence remain
Nipun-owned service seams; workers never write storage or a database directly.
Shreshtha receives the existing `transcript_segment`,
`diarization_speaker_turn`, and `chat_message` taxonomy unchanged.

## Phase 5B communication source-signal validation

`communication_processing.signal_validation` adds deterministic
`accepted`, `rejected`, `incomplete`, and `deferred` outcomes with bounded
reason codes, a safe locator reference, extractor identity, and an explicit
`correlation_ready` flag. No outcome contains transcript/message text, media
bytes, credentials, paths, or export payloads.

Transcript and diarization observations require non-negative ordered
source-relative millisecond ranges. When the real processing call supplies a
chunk range, a signal outside that range is rejected before publication; a
turn spanning chunks is not assigned an arbitrary chunk. The local pipeline
keeps valid ASR output if diarization fails or a diarization turn is excluded.
The worker's in-memory planning identifiers are provenance only, not a claim
that a coordinator persisted a manifest or checkpoint.

Every chat message must have a platform and deterministic message/export
locator. A missing sender or unresolved timestamp remains an incomplete,
non-correlation-ready message; the actual worker consequently does not emit
mentioned identifiers from it. Handles retain their platform namespace;
speaker labels are evidence-local technical labels; aliases and deterministic
transliterations remain candidate-only representations, never identities.
Graph-facing transcript/message fields remain a bounded length plus SHA-256
commitment, rather than raw content.
