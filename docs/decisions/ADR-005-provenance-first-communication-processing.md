# ADR-005: Provenance-First Communication Processing

- Status: Accepted (Sarthak Phase 1 audio/social/alias/communication foundation)
- Owner: Sarthak
- Date: 2026-09-10

## Context

`app/modules/communication_processing/` needs to turn audio metadata/imported transcript-diarization output and four social/chat export formats into canonical `ObservationV1`s, plus produce alias/transliteration and communication-link *candidates* for a later-phase reviewer. Several decisions had no single obviously-correct answer and are recorded here so later contributors (whoever eventually builds real ASR/diarization, entity resolution, or the review workflow these candidates feed) know what was deliberate.

## Decision 1: No actual ASR or diarization is implemented — only typed import of externally-produced results

**Decision**: `audio/transcript_import.py` and `audio/diarization_import.py` accept already-produced segments (`TranscriptSegmentInput`/`DiarizationSegmentInput`) and only validate/normalize them. If a job requests a transcript or diarization result but is only given raw audio (`AudioMetadataInput`), `worker.process_job` returns `WorkerStatus.DEFERRED` (`deferred_requires_asr`/`deferred_requires_diarization`) — never `SUCCEEDED` with invented text or speaker turns.

**Why**: The task brief is explicit that this phase must not implement Whisper/faster-whisper/any speech-to-text model, or pyannote/NeMo/SpeechBrain/voice biometrics/real diarization — and, independent of that instruction, fabricating transcript text or speaker attribution from evidence audio would be a direct integrity violation for an evidence-first system. Returning `DEFERRED` (not `FAILED`) for this specific case signals "this evidence needs a capability that doesn't exist yet in this phase," distinct from "this input is invalid" — a later-phase orchestrator can use that distinction to know which jobs to retry once a real ASR/diarization worker exists, versus which ones need a human to fix the input.

## Decision 2: Identity merging from speaker labels, aliases, handles, or phones is not just disallowed by policy — it's structurally impossible

**Decision**: `diarization_speaker_turn` observations use `entity_type_hint="speaker_label_local"`, never `"person"` or anything implying verification, and this module never combines speaker labels across two evidence files. `aliases/` never constructs or mutates `EntityV1`. `linking/models.LinkableMessageDescriptor` — the *only* input type every link-finding function in `linking/deterministic.py` accepts — has no display-name, alias, or transliteration-candidate field at all.

**Why**: CLAUDE.md's identity-merge boundary applies to every phase, not just this one. The task brief goes further and asks specifically that a link candidate never be constructable from name or transliteration similarity alone. Removing the display-name field from `LinkableMessageDescriptor` entirely (rather than including it and instructing the link-finding code "don't use this field for matching") makes that guarantee load-bearing at the type level: a future change to `linking/deterministic.py` that tried to add a name-similarity link type would have no name field to read in the first place, without first deliberately widening the descriptor — a much louder, more reviewable change than silently adding a new comparison inside an existing function.

## Decision 3: No graph writes; every result is typed Python data for a later-phase orchestrator

**Decision**: Nothing in this module imports `neo4j`, `sqlalchemy`, `asyncpg`, `redis`, `minio`, or any queue/HTTP client (statically verified, `tests/unit/communication_processing/test_module_safety.py`). `worker.process_job` returns `WorkerResultV1`; `linking/deterministic.py`'s functions return `list[CommunicationLinkCandidate]` directly, as plain typed Python values — not a `WorkerResultV1` field, since candidates are inherently cross-observation (built from many `LinkableMessageDescriptor`s that may span several jobs' worth of prior output), while `process_job` handles exactly one job's evidence at a time.

**Why**: Mirrors the boundary `app/modules/structured_processing/` and `app/modules/graph/` already sit behind — a worker/candidate-generator that never holds infrastructure credentials cannot leak or misuse them, and stays trivially unit-testable (every test in this module is a pure in-memory test; none of them need Docker, per `tests/unit/communication_processing/test_module_safety.py`'s static import check). Persisting `CommunicationLinkCandidate`s (e.g. into a review queue or a `Candidate` graph label distinct from a real relationship) is explicitly later-phase orchestration work this module doesn't attempt.

## Decision 4: Deterministic candidate IDs via canonical pair ordering

**Decision**: `linking/deterministic._build_candidate` sorts the two observation IDs into a canonical `(left, right)` order (string comparison) *before* deriving `candidate_id` via the project's `deterministic_uuid` helper, hashing `(case_id, left, right, link_type)`.

**Why**: Without canonical ordering, finding the same real-world pair `(A, B)` in one pass and `(B, A)` in another (e.g. because the input list order changed, or the same pair was discovered via two different link-finding functions with different natural iteration order) would produce two different-looking candidates for what is actually one proposed link — undermining the "deterministic for identical normalized input" requirement and letting duplicate review items accumulate. Canonical ordering makes the same real pair always resolve to the same `candidate_id`, verified directly by `tests/unit/communication_processing/test_linking.py::test_candidate_ids_are_order_independent`.

## Decision 5: `same_source_conversation` candidates are restricted to different evidence sources

**Decision**: `find_same_conversation_candidates` only proposes a candidate for two messages sharing a `conversation_id` when they also have *different* `evidence_id`s.

**Why**: Within one parsed evidence file, every message already carries the same `conversation_id` by construction (that's just what the parser extracted) — proposing a candidate for every such pair would be pure noise (and combinatorially expensive: a 1,000-message single-file conversation would otherwise generate ~500,000 redundant candidates, all of which are trivially and uninterestingly "true" already). The genuinely useful signal is cross-evidence corroboration: the same conversation appearing in two independently-submitted exports (e.g. two different participants' phone extractions of the same WhatsApp thread) is worth a reviewer's attention in a way that "these two messages I just parsed from the same file share a conversation ID" is not.

## Decision 6: Ambiguous or unsupported input fails or defers — it is never guessed at

**Decision**: This rule appears in three independent places in this module, applied consistently:

- **Timestamps**: a message timestamp is normalized to UTC only when the source is unambiguous by construction (explicit offset/`Z`, or a Unix epoch value). A naive/ambiguous timestamp keeps `timestamp_utc=None` and preserves `timestamp_raw` only.
- **Audio format**: an audio file that isn't WAV is `unsupported_audio_format` (or, if a request specifically needed ASR/diarization from it, `deferred_requires_asr`/`_diarization`) — never given fabricated technical metadata.
- **Transliteration**: a token containing even one character outside the small documented Devanagari/Gurmukhi table returns no transliteration candidate at all, not a partial or best-guess one.

**Why**: A confidently-wrong value (a mis-guessed timezone, invented audio metadata, a mangled transliteration) is worse for an evidence-first system than an honestly-absent one, because a human reviewer trusting it would be trusting a fabrication rather than a documented gap. This is the same posture `structured_processing`'s CDR/financial normalization takes (ADR-002, Decision 3: "keep the original when uncertain") — extended here to timestamps, audio format handling, and script transliteration specifically.

## Open questions for team review

- `diarization_speaker_turn`/`transcript_segment` observations use `json_path` built from the *caller-supplied list's positional index*, not any ID the original ASR/diarization system may have assigned beyond `source_segment_id` (which is preserved in `attributes` but not part of the locator). If a later phase's real ASR/diarization worker needs `json_path` to reference the *system's own* segment numbering rather than list position, this will need revisiting.
- `find_same_conversation_candidates`'s evidence-source restriction (Decision 5) means a case with only ever one evidence file per conversation will never produce this candidate type at all — confirmed intentional here, but worth the team validating against real expected case data shapes.
- The five allowed `LinkType` values are a starting set. Any new link type must go through the same "no display-name/alias field on the input, no probabilistic score" structural discipline established here, not just a documentation update.
