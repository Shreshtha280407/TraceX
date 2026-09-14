# ADR-012: audio timing and communication candidates are provenance-first

Audio uses bounded source-relative chunk and VAD intervals with explicit
profile hashes. An operator-configured local-command bridge is the approved
practical backend: it accepts normalized temporary PCM WAV input and a local
model path, emits strict JSON, and never downloads weights or uses a cloud
credential. Paths and command output are not logged/projected; backend/model/
configuration identity hashes are retained instead.

Diarization runs only under deterministic deep-profile and sufficient-speech
policy; an unavailable or failed adapter is explicit and cannot invalidate
successful ASR. Script/transliteration and social handles remain review-only
source candidates. Nipun's authenticated idempotent publication and
Shreshtha's existing mapping are reused without raw transcript/export
projection, identity resolution, or direct worker persistence.
