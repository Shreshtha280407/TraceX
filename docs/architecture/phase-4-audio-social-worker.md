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
