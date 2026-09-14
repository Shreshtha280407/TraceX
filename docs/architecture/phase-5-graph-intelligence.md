# Phase 5 graph intelligence and transparent rules baseline

Owner: Shreshtha. Status: in progress.

## Boundary

This module uses Nipun's durable `CorrelationSubmission` and `correlation.upserted.v1` replay seam.
It returns reviewable candidates only; it never resolves or merges identities.
PostgreSQL is authoritative and Neo4j is an idempotent, bounded metadata projection.

| Input | Output | Provenance | Time policy |
|---|---|---|---|
| Stable phone, email, account, device, vehicle, platform handle | exact candidate | case, observation, evidence, locator, normalization version | no event inferred |
| Alias/transliteration/handle | bounded candidate | source-backed observation/evidence references | no event inferred |
| Local hashed pgvector snapshot | weak candidate | provider/configuration/source snapshot hashes | no event inferred |
| Bounded windows | hot-window contribution | both evidence paths and start/end | both ranges required |
| Durable correlation | `Correlation` node | Nipun EvidencePathSnapshot | no timeless entity edge |

Document, CDR, finance, media, audio, and social claims retain existing mappings.
Calls, transfers, sightings, meetings, and messages remain event-first and time-bounded.

## Phase 4 -> Phase 5 dependency map and wiring

A reconciliation audit of this module found every individual algorithm
(exact/weak retrieval, scoring, the semantic Cypher handler, analytics, the
temporal motif) genuinely implemented, deterministic, and unit-tested --
but with no adapter turning real, persisted Phase 4 canonical observations
into their input shapes, and no reachable process ever chaining them
together or registering the handler into Nipun's replay seam. This phase
closes exactly that gap, adding no new algorithmic surface:

- `intelligence/sourcing.py` -- `descriptor_from_observation` maps real
  canonical `ObservationV1` rows into `ObservationDescriptor`s. Documented,
  bounded scope: Sarthak's `chat_message`/`phone_number`/`email_address`/
  `username_or_handle`; Jasraj's `phone_number_mention`/
  `email_address_mention`/`vehicle_identifier_mention`/
  `financial_identifier_mention` (`upi_id`/`account_number` hints, both ->
  `account`)/`financial_account_mention`/`cdr_device_identifier_mention`
  (IMEI)/`cdr_subscriber_identifier_mention` (IMSI)/`cdr_call_record`
  (caller number only)/`financial_transaction_record` (sender account
  only). Everything else -- `transcript_segment`, `diarization_speaker_turn`,
  raw OCR text, media sightings, `transaction_reference`/`cell_tower_id` --
  is explicitly skipped, never guessed at (see the module's own docstring
  for the full, current list and each exclusion's reasoning).
  `build_motif_edges` similarly adapts CDR/finance records into the
  temporal-motif's edge shape via a deterministic, internal-only party key
  (never an entity, never persisted) -- see "Analytics and motif" below for
  its documented cross-modal limitation. `edges_from_candidate_links` adapts
  Nipun's already-persisted `correlation_candidate_links` rows into the
  analytics edge shape.
- `intelligence/pipeline.py` -- `build_case_correlation_submission` (pure)
  chains `descriptor_from_observation` -> `retrieve_candidates` ->
  `add_temporal_hot_window_reason` -> `score_candidates` ->
  `build_correlation_submission` for one case's observations.
  `run_case_correlation_pass` (I/O) additionally fetches those observations
  from PostgreSQL and submits the result through
  `GraphCorrelationIntegrationRepository.submit()` -- Nipun's existing,
  unmodified durable seam; there is no second persistence route.
  `run_case_analytics_snapshot`/`run_case_motif_snapshot` similarly wire
  real, already-persisted candidate links / real observations into
  `analyse()`/`detect_communication_transfer_movement_motifs()`.
- `app/modules/graph/intelligence_worker.py` (new CLI) --
  `--replay-once`/`--replay-loop` compose `make_correlation_projection_handler`
  with `replay_graph_updates`, the minimal registration wiring that makes
  the semantic `correlation.upserted.v1` handler reachable from a real
  process for the first time; `--generate --case-id <uuid>` runs one
  correlation pass for an explicit case. Neither changes Nipun's outbox
  semantics, table meaning, or replay/idempotency contract.

## Invariants

Exact blocking uses only documented stable categories after explicit normalization.
Aliases, transliterations, and vectors are retrieval reasons and never verification.
Candidates retain observations, evidence, locator references, named contributions, contradictions, and hashes.
Mixed-case input is rejected before retrieval, scoring, correlation, vector search, analytics, or Cypher projection.
The semantic handler starts at case-scoped `Evidence-[:YIELDED_OBSERVATION]->Observation` paths.
Missing graph provenance makes Cypher a no-op; `MERGE (case_id, projection_key)` makes replay idempotent.
Neo4j receives no raw source, object URI, credential, claim token, SQL, or driver error.

## Preliminary rules and pgvector

`phase5_preliminary_rules_v1`: exact identifier `+60`, platform handle `+45`, exact alias `+12`,
normalized alias `+8`, transliteration `+6`, local vector `+3`, hot window `+10`, contradiction `-40`.
These weights are preliminary review guidance, never probability, guilt, truth, or automatic merge.
The configuration and feature snapshot use canonical SHA-256 hashes.
**Known normalization limitation**: `normalise_identifier`'s phone rule strips non-digit characters and
prepends `+` but never reconciles a country code, so a CDR's E.164 `+919876543210` and a bare-digit
`9876543210` extracted elsewhere are correctly treated as distinct rather than incorrectly merged --
a real, documented gap, not a silent guess. See `docs/qa/known-limitations.md`.
The vector is a 32-dimensional deterministic token hash, not a semantic trained model.
pgvector searches filter case before ordering and record provider/version/configuration/source snapshot hashes.
`VectorSearchUnavailable` is explicit; no substitute semantic result is fabricated.
`PgvectorCandidateStore` availability is checked reactively (a `DBAPIError` during `upsert`/`search`
raises `VectorSearchUnavailable`), not proactively via a startup probe; a dedicated live test
(`tests/integration/graph/test_intelligence_vector_store_live.py`) confirms the `vector` extension is
genuinely installed and exercises real `upsert`/`search`/case-isolation against it, not merely a mocked
engine. `retrieve_candidates`'s own weak-vector reason uses a separate, always-in-process deterministic
hashed-token vector (`hashed_token_vector`) -- `PgvectorCandidateStore` is for persisted, cross-run
vector search specifically, not a dependency of the in-memory retrieval path.

## Analytics and motif

The explicit one-case snapshot returns deterministic PageRank, unweighted Brandes betweenness, WCC, and seeded
Leiden modularity communities through `igraph`/`leidenalg`, computed over the case's own already-persisted,
already-scored candidate-link graph (`sourcing.edges_from_candidate_links`) -- nodes are observation IDs,
edges are reviewable candidate links, never a hypothetical entity/identity graph.
The motif is a source-backed, ordered communication → transfer → movement/meeting chain within one hour,
adapted from real CDR/finance canonical observations via a deterministic, internal-only party-correlation
key (`sourcing._party_key`/`_account_party_key`) that recognizes a shared phone/account identifier across
event types -- including a UPI/mobile-linked account string that is itself phone-shaped -- without ever
creating or persisting an entity. **Known cross-modal limitation**: a media sighting/meeting-candidate
observation carries no phone/account/vehicle identifier comparable to a CDR/finance party in this phase, so
the "movement/meeting" third hop only fires when a `meeting_candidate` observation's own
`contributing_observation_ids` cites a call/transfer this same adapter run already recognized -- a real,
evidence-backed link, never a fabricated one, but one that will not fire for arbitrary unrelated media
sightings. See `docs/qa/known-limitations.md`.
Analytics and motifs are review-priority signals only.

## Evaluation and deferral

`deferred_evaluation_report` is a versioned, hashable future-offline reporting shape only.
Operation Nightfall truth evaluation, measured Precision@K/Recall@K/false-link rate, P99 bridge validation,
final rules-weight freeze, full real-dataset validation, and LAN end-to-end validation will run after all
Phase 5 contributors have merged their work.
Trained ranking, entity resolution, automatic merges, hypotheses, criminality conclusions, and external AI
APIs are deferred. Algorithm quality and operational performance remain unmeasured.

## Phase 5B secure read boundary

Every externally reachable Phase 5 graph/correlation read uses the established
`require_graph_read` dependency and `CaseAction.GRAPH_READ`; it returns a typed
`AuthorizedCasePrincipal` carrying the authenticated principal, requested case,
required action, and active membership only after the decision permits access.
The decision is recorded as a safe allow/deny audit event containing request ID,
principal reference, case ID, and action only. A non-member, inactive member,
insufficient role/clearance, and unknown case deliberately receive the same
non-enumerating denial.

The graph observation Cypher binds `$case_id` on each root/traversal node; the
durable correlation/candidate/event repository includes `case_id` in every
external read predicate; pgvector retrieval binds `case_id` before ordering or
returning candidates. There is no Phase 5 graph-read cache today, so no
cross-case cache key exists. The public surface intentionally does not expose
feature snapshots, analytics, motifs, or direct vector search routes.

Neo4j and PostgreSQL dependency failures return fixed `503` errors without a
driver message, query text, topology, DSN, or case-existence signal. Candidate
links and correlations remain review-only propositions throughout this boundary.

## Phase 5B source-signal contract (Jasraj)

The Phase 5 sourcing boundary consumes only source-backed role-specific producer
attributes: CDR `caller_number`/`callee_number` and finance
`sender_account`/`receiver_account`. Each producer also emits the equivalent
ordered `participants` array with the explicit roles `caller`/`callee` or
`sender`/`receiver`; it is an evidence-local representation, not an entity
assertion. `sourcing.py` continues to use those legacy role-specific keys for
its bounded descriptor and motif adapters. A future adapter extension that
needs one descriptor per participant must emit separate evidence-local
descriptors with the same observation/evidence/locator provenance; it must not
invent a second party or alter `ObservationV1`.

Valid events preserve `case_id`, `evidence_id`, source locator, extractor
identity/config hash, and canonical event time. CDR optionally preserves
duration, type/direction, call ID, and an end time only when source-supplied;
finance preserves exact decimal amount, supplied currency, reference ID, and
channel only when validated and safe. `source_signal_quality` describes source
validation/extraction only, never identity, truth, guilt, or score. A blank,
malformed, impossible, or incomplete core event is rejected before canonical
publication and therefore cannot reach descriptor retrieval, candidate links,
Neo4j, correlation persistence, or scoring.
