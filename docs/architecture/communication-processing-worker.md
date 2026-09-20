# Communication-Processing Worker (Phase 2 — Sarthak)

This phase adds a **one-shot worker CLI** on top of `app/modules/communication_processing/`'s existing (Phase 1) `process_job` pure function, following the exact orchestration pattern Jasraj's `structured_processing` worker established (`docs/architecture/structured-processing-worker.md`): claim a job through Nipun's internal worker API, resolve that job's evidence via the same claim-token-bound secure input stream, build the typed `InputPayload` `process_job` expects, call `process_job` completely unchanged, and submit the result. No daemon, polling loop, Celery, or scheduler exists anywhere in this repository.

## The full flow

```
uv run python -m app.modules.communication_processing.worker --once
  -> POST /api/v1/internal/worker-jobs/claim (one compatible processor at a time)
  -> GET  /api/v1/internal/worker-jobs/{job_id}/input   (claim-token-bound, SHA-256 verified)
  -> _build_input_payload(job, resolved)   (new: raw bytes -> typed InputPayload)
  -> process_job(job, input_payload)       (Phase 1, unchanged)
  -> POST /api/v1/internal/worker-jobs/{job_id}/result
  -> exit 0
```

A run that finds no eligible job, a run that submits a terminal `SUCCEEDED`/`FAILED`/`DEFERRED` result, and a run that defers because the input-stream endpoint is genuinely unreachable are all *successful* CLI exits (`0`) — only a genuine auth/transport/API failure exits `1`. See `worker.main`'s docstring.

## Supported processors

`worker.SUPPORTED_PROCESSORS` lists every `(processor_name, processor_version)` this worker's `process_job` dispatch table handles, all pre-existing from Phase 1 (`worker._PROFILES`) and unchanged by this phase:

| Processor | Input shape | Emits | Never does |
|---|---|---|---|
| `audio_metadata_v1` / `1.0.0` | Raw WAV bytes | `audio_metadata` (duration, sample rate, channels, codec — read via the stdlib `wave` module) | Transcription, diarization, any waveform analysis |
| `transcript_import_v1` / `1.0.0` | JSON interchange: `{"segments": [{start_ms, end_ms, text, language_hint?, confidence, source_segment_id}, ...]}` | `transcript_segment` (one per segment) | Run ASR on raw audio — this profile only *imports* externally-produced transcript metadata |
| `diarization_import_v1` / `1.0.0` | JSON interchange: `{"segments": [{start_ms, end_ms, speaker_label, confidence, source_segment_id}, ...]}` | `diarization_speaker_turn` (one per segment) | Run real diarization, or claim `speaker_label` is a resolved identity |
| `whatsapp_export_v1` / `1.0.0` | WhatsApp plain-text export | `chat_message` (one per message) | — |
| `telegram_export_v1` / `1.0.0` | Telegram Desktop JSON export | `chat_message` | — |
| `instagram_export_v1` / `1.0.0` | Instagram message JSON export | `chat_message` | — |
| `generic_social_json_v1` / `1.0.0` | Generic social-intelligence JSON record export | `chat_message` | — |

The social/chat parsers (`social/{whatsapp,telegram,instagram,json_records}.py`), `audio/metadata.py` (WAV extraction), and `audio/routing.py` (format-support decisions) are all **Phase 1, unchanged**. This phase's new parsing logic is exactly two JSON-interchange deserializers: `audio/transcript_import.py::parse_transcript_import_payload` and `audio/diarization_import.py::parse_diarization_import_payload` — both added to their already-owning files (not a new `parsers/` package), since Phase 1 only ever accepted these two profiles' segments as pre-built Python dataclasses passed directly to `process_job`, never as raw uploaded bytes. Malformed-shape errors (`malformed_json_payload`: not UTF-8, not JSON, wrong top-level shape, missing/mistyped segment field) are checked before any timing/bounds validation runs (`invalid_transcript_segment`/`invalid_diarization_segment`), which is unchanged Phase 1 logic.

## Routing boundary — a missing route, reported, not implemented

`evidence_lifecycle/routing.py` (unmodified by this phase) only ever routes **two** of this worker's seven profiles from a real evidence upload:

| `SourceType` | Routes to |
|---|---|
| `audio` | `audio_metadata_v1` / `1.0.0` |
| `chat` | `generic_social_json_v1` / `1.0.0` |

`transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, and `instagram_export_v1` are fully implemented, fully tested, and listed in `SUPPORTED_PROCESSORS` — but **unreachable through the live upload path today**. There is no `SourceType` or routing rule that ever assigns a real uploaded evidence file to any of these five processors; the only way to exercise them currently is a directly-constructed `WorkerJobV1` (as the unit tests do) or a future routing/`parser_profile`-selection decision this task was explicitly instructed not to make unilaterally (`CLAUDE.md`'s shared-contract rule; per this task's own "report the exact missing route" instruction).

**Recommended additive decision, for team review — not implemented here**: mirroring Phase 2.3's precedent (`generic_tabular_v1`/`generic_json_v1` reached via new `structured_tabular`/`structured_json` `SourceType`s), a plausible next step is either (a) a client-supplied `parser_profile` hint on `chat`/`audio` uploads, server-validated against an allow-list per `SourceType` (never client-chosen arbitrarily — see "Worker identity and routing" below), or (b) new `SourceType` values (e.g. `chat_whatsapp_export`, `chat_telegram_export`, `chat_instagram_export`, `audio_transcript_import`, `audio_diarization_import`) with their own routing rows. Both require sign-off from whoever owns `evidence_lifecycle/routing.py`'s shared contract before implementation.

## Source-locator conventions

| Observation type | Locator fields | Notes |
|---|---|---|
| `audio_metadata` | `time_start_ms=0`, `time_end_ms=<duration>` | The whole file's technical envelope, not a moment in it |
| `transcript_segment` | `time_start_ms`, `time_end_ms` | Non-negative, `time_end_ms >= time_start_ms`, validated before any observation is built |
| `diarization_speaker_turn` | `time_start_ms`, `time_end_ms` | Same timing validation; overlapping turns from different speakers are preserved, never merged or discarded |
| `chat_message` | `message_id` | Every message observation carries a valid `source_locator.message_id`; never fabricated when absent from the source record |

## Confidence semantics (unchanged from Phase 1)

`extraction_confidence` measures extraction/statement quality only — **never** a probability of guilt or culpability (`CLAUDE.md`). Two regimes:

- **Structural profiles** (`audio_metadata_v1`, all four social-export profiles): fixed `CONFIDENCE_STRUCTURED_COMPLETE = 1.00` — a complete record read directly from a validated file format, not a probabilistic judgment.
- **Interchange-import profiles** (`transcript_import_v1`, `diarization_import_v1`): the caller-supplied per-segment `confidence` value, passed through unchanged (`0.0`–`1.0`, validated) — this worker never invents or adjusts a confidence value for externally-produced ASR/diarization output it did not itself generate.

## Deterministic observation IDs (unchanged from Phase 1)

`provenance.observation_id()` derives every `observation_id` from `(case_id, evidence_id, profile.name, profile.version, observation_type, locator)` via `app.core.ids.deterministic_uuid`. The same source content, profile, and locator always produce the same ID — a safe retry (the worker resubmitting an identical `WorkerResultV1`) never creates duplicate observations, matching `EvidenceLifecycleService.submit_result`'s idempotency contract exactly. Changing any identity-bearing input (a different `message_id`, a different segment's `time_start_ms`) changes the ID.

## Evidence-local mentions vs. resolved identities

Every `ExtractedEntityMention`/`RawMention` this worker emits — a chat sender/recipient reference, a diarization `speaker_label` — is an **evidence-local, unresolved** reference: text plus a type hint, never a link to a real `EntityV1`. `diarization_segments_to_mentions` sets `entity_type_hint="speaker_label_local"` specifically to make this explicit; nothing in this module ever asserts `"verified"` or `"person"` identity for a diarization label. This worker never creates or mutates an `EntityV1`, never creates a graph relationship, and never merges two aliases/labels into one identity — entity resolution remains a later-phase, explicit, reviewable operation (`CLAUDE.md`).

**Section 3 (Unicode/alias/multilingual safety baseline) was already satisfied by pre-existing Phase 1 code, no change needed this phase**: `aliases/normalize.py` (NFKC + whitespace normalization, case-folded comparison keys, original display text preserved), `aliases/scripts.py`/`aliases/transliteration.py` (deterministic script/language detection, stable normalized tokens), and `linking/deterministic.py` (produces `CommunicationLinkCandidate`s with `status=proposed_for_review` — a separate mechanism, never wired into `RawMention.attributes` or any `ObservationV1`). Because alias/transliteration output never flows into an observation's attributes in the first place, the "mark alias variants `review_only`/`not_identity_proof`" requirement is satisfied by construction: there is nothing in an observation to mark.

## Worker identity, security, and provenance

Reuses Aditya's Phase 2 per-worker-credential system exactly (`docs/architecture/worker-identity-and-security.md`) — no new authentication mechanism, no shared-secret fallback, no public credential-issuance API:

- `WorkerApiClient` (this module's own `client.py`, mirroring `structured_processing.client`) authenticates as `Authorization: Bearer <WORKER_TOKEN>`; `/result` and `/input` additionally require the `X-Claim-Token` header and that the calling worker identity matches `job.claimed_by_worker_id` — enforced entirely server-side by Nipun/Aditya's existing code, unchanged here.
- `/claim` enforces processor-name scope (`allowed_processor_names` on the operator-provisioned `WorkerCredentialRecord`) before the claim query runs; an out-of-scope claim gets a generic `403`, audited server-side as `worker_processor_scope_denied` — this worker's CLI never sees or needs to know why beyond "claim failed."
- **Register a real worker credential** via the sanctioned trusted-operator CLI only, exactly as Aditya's doc specifies — this module adds no alternative:
  ```
  uv run python -m app.modules.access_control.worker_credentials create \
    --name communication-processing-worker \
    --processor audio_metadata_v1 --processor generic_social_json_v1 \
    [--processor transcript_import_v1 --processor diarization_import_v1 \
     --processor whatsapp_export_v1 --processor telegram_export_v1 --processor instagram_export_v1]
  ```
  The command prints the plaintext token exactly once; set it as `WORKER_TOKEN` for the worker process. `list`/`rotate`/`revoke` subcommands exist for operator inspection and lifecycle management; revocation is permanent by design (a revoked credential's digest can never be silently reactivated — see `docs/qa/known-limitations.md`).
- **Secure input stream + SHA-256 verification**: `run_once` fetches evidence bytes via `GET /api/v1/internal/worker-jobs/{job_id}/input` (the same claim-token-bound endpoint `structured_processing` uses), reads the evidence's recorded SHA-256 from `X-TraceX-Evidence-SHA256`, and verifies `hashlib.sha256(resolved.data).hexdigest()` against it **before** `_build_input_payload`/`process_job` ever sees the bytes. A mismatch submits `FAILED`/`evidence_integrity_mismatch` (`retryable=True`) — never silently parses unverified bytes.
- **Never logged or exposed anywhere** (`client.py`, `worker.py`, every error path): the worker token, claim token, MinIO endpoint/object key/URI/credentials, DB credentials, or raw source content (message text, phone numbers, transcript text). `client.py` logs only `job_id`s, processor names, and HTTP status codes. `ProcessingError.message`/`WorkerError.message` are restricted by convention (see `errors.py`'s docstring) to *kind of problem*, never the offending value.
- **Client never chooses the processor**: `_build_input_payload`'s dispatch key is `job.processor_name` — the already-authenticated, server-assigned value from the claimed `WorkerJobV1` — never a client-supplied field, filename, or sniffed content type.
- **Job-scoped processing**: a claimed job's claim token is valid only for that specific job (enforced server-side); this worker's CLI processes exactly the one job it claimed per `--once` invocation.

## Failure and defer policy

- **Malformed/invalid source content** (bad WAV header, malformed JSON interchange payload, invalid timing, unsupported chat export shape, oversized input): `process_job` (or `_build_input_payload`, for the two interchange profiles, which parse before `process_job` is ever called) catches `ProcessingError` and the run submits `WorkerStatus.FAILED` with a safe, non-secret `WorkerError` (code + message only).
- **`audio_metadata_v1`-only WAV routed to `transcript_import_v1`/`diarization_import_v1`** (real ASR/diarization would be required): `WorkerStatus.DEFERRED` with a checkpoint naming `AudioRoutingDecision.DEFERRED_REQUIRES_ASR`/`DEFERRED_REQUIRES_DIARIZATION` — an honest "not yet possible" outcome (real ASR/diarization models are out of scope for every phase so far), never a fabricated transcript.
- **Input resolution unavailable** (input-stream endpoint unreachable): `WorkerStatus.DEFERRED`, checkpoint `input_resolution_unavailable` (`CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE`) — mirrors `structured_processing.worker`'s identical addition exactly.
- **SHA-256 mismatch**: `WorkerStatus.FAILED`, `error.code="evidence_integrity_mismatch"`, `retryable=True` — a genuine data-integrity signal, not "not yet possible."
- `queued`/`running` are never submitted as a final result.

## What this worker emits — and what it never does

Every emitted item is a canonical `ObservationV1` with `schema_version`, `case_id`, `evidence_id`, `observation_id`, `created_at`, `extracted_entities`, `extraction_confidence`, `source_locator`, and `extractor` all populated — a raw, unresolved statement about the source, carrying its own provenance. This worker **never** creates an `EntityV1`, `EventV1`, a graph node/relationship, a candidate score, a hypothesis, or writes to Neo4j — see `CLAUDE.md`'s "No automatic identity merge, guilt conclusion, or case-unscoped retrieval" rule. Output is a durable, case-scoped `ObservationV1` row (`worker_observations` table, via the existing evidence-lifecycle result-submission path) usable by a later graph-projection module without this module ever accessing Neo4j directly or depending on Shreshtha's graph-projection work.

## Structured logging

`_configure_logging` mirrors `structured_processing.worker`'s deliberately-duplicated structlog JSON configuration (a one-shot CLI script doesn't construct the full FastAPI app just to log one line). `run_once` binds `run_id` (and `job_id` once claimed) via `structlog.contextvars` for the run's duration — the same correlation pattern used across this repository's HTTP request path.

## Non-goals (this phase)

Everything `CLAUDE.md` and Phase 1 already ruled out remains ruled out: raw MP3/WAV/MP4 ASR, speaker-diarization ML models (Whisper/NeMo/pyannote/cloud AI/LLMs/embeddings/vector search), automatic transliteration-based identity matching, automatic alias/entity merging, `EntityV1`/`EventV1` creation, Neo4j writes/graph projection/candidate scoring/hypotheses, social-media API scraping, frontend, new auth/RBAC/ABAC, new queue systems, public worker-admin APIs, changes to frozen `V1` contracts, Merkle roots/signatures. This worker adds exactly one new capability: turning a claimed communication-related `WorkerJobV1` into a submitted `WorkerResultV1` through the existing internal API, for the seven profiles above (two of which are reachable via a real upload today; see "Routing boundary").

## Phase 7 Part 4 audio/social candidate-model benchmark (Sarthak)

A second, entirely separate capability exists alongside the worker above:
`uv run python -m app.modules.communication_processing.audio_social_benchmark_cli`
(new `audio_social_benchmark*.py` files) evaluates *candidate* ASR/VAD/
diarization/language-ID/social-extraction models (faster-whisper, Silero
VAD, pyannote.audio, fastText, and this project's own existing
deterministic social/chat parsers) against Phase 7 Part 1's dataset
manifest and result contracts -- a model-selection benchmark, not this
worker's own claim/process/submit contract. This document's own
"Non-goals" section above ruled out "speaker-diarization ML models
(Whisper/NeMo/pyannote/...)" for *this worker* -- Phase 7 Part 4 is the
later, explicitly-approved phase that evaluates exactly those candidates,
built as a wholly additive harness that never changes this worker's or
any other production audio/social code. See
`docs/architecture/phase-7-evaluation-and-model-governance.md`'s "Part 4"
section for the full design, and `docs/runbooks/local-development.md` for
CLI usage and the MacBook Gate B pre-flight handoff.
