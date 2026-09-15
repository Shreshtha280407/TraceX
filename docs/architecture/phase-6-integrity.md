# Phase 6: Integrity Foundation and Protected Access

Owner: Nipun. Status: Part 1 (this document) complete — durable, case-scoped
integrity events, deterministic Merkle checkpoints, and local Ed25519
signing/verification. Review-decision and hypothesis-action recording
(Shreshtha), authorization/audit protection for integrity operations
(Aditya), and modality-specific provenance leaves (Jasraj/Gaurav/Sarthak)
are later Phase 6 work — see "Handoff to later branches" below.

## Part 2: protected API, append-only records, and reconciliation

Part 2 adds only case-scoped verification access. `GET
/api/v1/cases/{case_id}/integrity/checkpoints`, checkpoint detail, `POST
.../{checkpoint_id}/verify`, and `GET .../{checkpoint_id}/export` all use
the normal authenticated case-action dependency. Missing membership,
insufficient role/clearance, and a nonexistent case produce the existing
generic 403; unauthenticated callers receive the existing 401. Authorization
runs before any checkpoint lookup, so a checkpoint ID cannot be used to
probe another case.

`integrity_read` and `integrity_verify` are granted to owners, managers,
investigators, analysts, and reviewers. `integrity_export` is intentionally
more restrictive: owners and managers only. Viewers receive no integrity
action. Successful operations are recorded as safe `integrity_operation`
audit telemetry; authorization failures retain the existing safe
`case_access_denied` event. Both contain actor, case, action, outcome, and
request ID only.

List and detail return checkpoint range/count, root, format, timestamps and
public signature-verification metadata. They never return a raw event feed.
Export uses the Part 1 verification bundle unchanged: safe leaf identifiers
and hashes plus public verification material only; no private key, source
body, object URI, transcript, OCR text, credential, or exception detail.
Verification is public-key-only and never loads or invokes a signing key.
Pages are bounded to 100 checkpoints.

The `c42d3e4f5a6b` migration adds PostgreSQL row triggers that reject every
`UPDATE` and `DELETE` on `integrity_events`, `merkle_checkpoints`, and
`checkpoint_signatures`; ordinary inserts remain valid. This is strong
application/database protection, not an absolute claim against a PostgreSQL
superuser, who can disable triggers or alter the database.

Integrity recording still follows the primary write and logs/continues on a
failure. The structured signal has only case ID, event kind, subject type/ID,
correlation/request ID, `reconciliation_pending`, and an exception class.
An operator can run `uv run python -m app.modules.integrity.reconcile_cli
--case-id <uuid>`. It scans the existing durable evidence records for that
case, reconstructs exactly the safe original submission, and records only a
missing idempotency key. Repeated runs are idempotent; it never reads object
storage, source bodies, or another case. It currently covers evidence and
correlation source records. Observation batches are intentionally not replayed
until a durable ordered observation-ID representation exists; UUID sort order
would alter the frozen original payload hash. Later producer owners must
register their durable replay adapter when they add new integrity leaves.

Live Docker/infrastructure validation is intentionally deferred to Phase 6
Part 5 (Shreshtha); Part 2 does not start containers.

## What this proves, and what it explicitly does not

**This phase proves that TraceX can detect tampering with recorded
integrity metadata.** A safe, non-secret fingerprint of a durable write
(evidence registration, observation-batch acceptance, correlation
completion) is recorded as an append-only `IntegrityEvent`. A case-scoped,
deterministic Merkle tree is built over a contiguous range of those events'
per-case sequence numbers, sealed into a `MerkleCheckpoint`, and signed
locally with Ed25519 into a `CheckpointSignature`. Independent verification
recomputes the tree from the current database state and checks it against
the signed root — any altered, deleted, or reordered leaf, or any altered
root or signature, is detected.

**It does not claim public-blockchain anchoring.** There is no distributed
ledger, no external timestamping authority, and no proof that a checkpoint
existed at a given time independent of this database and this signing key.
The guarantee is *internal consistency*: given the current database state,
a checkpoint's signed root either matches a correct recomputation or it
does not. If an attacker can both alter `integrity_events`/
`merkle_checkpoints` rows *and* obtain the private signing key, they can
forge a consistent-looking history — exactly the threat model any
local-only signature has, and why `docs/decisions/
ADR-013-phase-6-integrity-checkpoints.md` names this explicitly rather than
implying a stronger guarantee. See "Non-goals" below for the full list of
what later phases (or a different system) would need to add for a stronger
claim.

## Module layout

`app/modules/integrity/` — new, following `app/modules/graph/
integration_models.py` + `integration_repository.py`'s established
"internal, versioned integration record, not a frozen `app.contracts` V1
model" pattern:

| File | Responsibility |
|---|---|
| `models.py` | `IntegrityEventKind`, `IntegrityEventSubmission`/`IntegrityEventRecord`, `MerkleCheckpointRecord`, `CheckpointSignatureRecord`, `VerificationBundle`/`VerificationResult`. All frozen (`extra="forbid", frozen=True`), mirroring `graph.models.GraphModel`. |
| `hashing.py` | The frozen, domain-separated SHA-256 Merkle algorithm (`leaf_hash`, `parent_hash`, `build_merkle_root`). Pure functions, no I/O. |
| `signing.py` | Ed25519 signing/verification (`load_signing_key`, `generate_signing_key_b64`, `verify`). Private key material never leaves this file except as a signature. |
| `repository.py` | `sa.Table` definitions, the idempotent-submit/fingerprint-comparison persistence pattern (mirrors `graph.integration_repository.GraphCorrelationIntegrationRepository.submit()`), `IntegrityValidationError`. |
| `service.py` | `IntegrityService` — the one interface later branches need: `record_integrity_event`, `build_checkpoint`, `verify_checkpoint`, `export_verification_bundle`. |
| `dependencies.py` | FastAPI dependency providers (`get_integrity_service`), mirroring `evidence_lifecycle.dependencies`'s lazy-engine-at-import pattern. |
| `api.py` | Protected, case-scoped checkpoint metadata, verification, and safe export routes. |
| `cli.py` | The operator CLI: `generate-key`, `build-checkpoint`, `verify`, `export`. |

## What an `IntegrityEvent` safely captures

Fields: `integrity_event_id`, `case_id`, `event_kind`, `subject_type`,
`subject_id`, `canonical_payload_sha256`, `payload_schema_version`,
`source_created_at`, `idempotency_key`, `sequence_number`, `created_at`.

Supported `IntegrityEventKind` values: `evidence_registered`,
`observation_published`, `correlation_completed`, `review_decision`,
`hypothesis_action`. The final two are **forward-compatible only** — the
enum and schema accept them, but no producer emits them yet and no
review/hypothesis workflow exists in this repository. Shreshtha's later
Phase 6 work is expected to emit them through the same `record_integrity_event`
facade, unchanged.

### Why raw content never reaches storage

`IntegrityEventSubmission.canonical_metadata` is the *only* place a caller
supplies structured data, and it is never persisted. `IntegrityRepository
.record_event` computes `canonical_payload_sha256 = canonical_sha256
(submission.canonical_metadata)` (reusing `app.core.canonical`, the same
canonical-JSON helper every other module's fingerprinting uses) and stores
only that hash — the metadata dict itself never reaches the database, a
log line, or an error message. Two defenses apply before that point:

1. `IntegrityEventSubmission` validates `canonical_metadata` recursively
   for forbidden key substrings (`password`, `secret`, `token`,
   `credential`, `object_uri`, `dsn`, `transcript`, `chat_body`,
   `ocr_text`, `private_key`, `raw_text`, `body_text`, `message_body`) and
   a bounded string length (2000 chars), mirroring `graph
   .integration_models._validate_feature_value`'s established pattern.
2. Every producer seam (below) builds its `canonical_metadata` from a
   deliberately minimal, structural dict — IDs, counts, hashes, enum
   values — never from a full contract object that could carry extracted
   text. This is a design discipline, not something the validator alone
   can guarantee: a contract object's field names don't reliably contain
   the forbidden substrings above, so producers must not pass one in
   directly. See each seam's `canonical_metadata` construction in
   `app/modules/evidence_lifecycle/service.py` and `app/modules/graph
   /intelligence/pipeline.py` for the exact minimal shape each uses.

Hashing raw content directly (even one-way) is deliberately avoided, not
only forbidden-content: a hash of a small, guessable value (a name, a short
phrase) is not meaningfully protected by SHA-256 against a dictionary
attack the way a hash of a large, structurally rich contract payload might
be. Restricting the hash input to structural metadata sidesteps that
question entirely.

### Immutability and append-only guarantee

There is no `update`/`delete` method on `IntegrityRepository` for
`integrity_events`. Verification's tamper-detection property depends on
this: a leaf's hash is *recomputed* from the current row at verification
time, not read from a separately stored copy, so a row altered outside the
application (direct DB edit) is caught by a leaf-hash mismatch against the
already-sealed root, exactly as it should be. See "Deliberate storage
trade-off" below for why no separate `checkpoint_leaves` table exists.

### Idempotency and sequencing

- **Exact retry**: a `(case_id, idempotency_key)` match with an identical
  fingerprint (recomputed from the persisted row's own fields) returns the
  existing record unchanged — no new row, no new sequence number consumed.
- **Conflicting reuse**: the same `(case_id, idempotency_key)` with a
  different fingerprint raises `IntegrityValidationError` before any write.
- **Sequencing**: `integrity_sequence_counters` is a small per-case counter
  table (`case_id` PK, `last_sequence`), updated via `INSERT ... ON
  CONFLICT (case_id) DO UPDATE SET last_sequence = last_sequence + 1
  RETURNING last_sequence` inside the *same* transaction as the event
  insert. A replay never touches the counter (it returns before reaching
  that code), so genuinely new events always get a contiguous 1..N
  sequence per case — required for checkpoint range contiguity. This is a
  **new** pattern, not a reuse of the one existing sequence precedent:
  `worker_progress_events_ordinal_seq` (a plain Postgres `SEQUENCE`, see
  `c1c9c1c9d9f1_observation_batch_ingestion.py`) is global, not scoped per
  case, so it can't give the per-case contiguous ordering this task
  requires.
- Postgres's row-level locking on the counter's `ON CONFLICT DO UPDATE`
  branch makes concurrent allocation for the same case safe: a second
  concurrent writer for the same `case_id` waits for the first transaction
  to commit or roll back before its own increment proceeds.

## Deterministic Merkle checkpoint rules (frozen)

See `hashing.py`'s module docstring and `docs/decisions/
ADR-013-phase-6-integrity-checkpoints.md` for the authoritative version.
Summary:

- **Hash algorithm**: SHA-256.
- **Leaf ordering**: `(case_id, sequence_number)` ascending — never
  database-return order or wall-clock order alone. `list_events_in_range`
  always `ORDER BY sequence_number ASC`.
- **Leaf hash input**: `b"tracex.integrity.leaf.v1\x1f" + canonical_bytes({
  integrity_event_id, case_id, event_kind, subject_type, subject_id,
  canonical_payload_sha256, payload_schema_version, sequence_number })`.
- **Parent hash input**: `b"tracex.integrity.node.v1\x1f" + left_hash_bytes
  + right_hash_bytes` (raw digest bytes, not hex text).
- **Odd node at a level**: duplicate the final hash. Never zero-pad.
- **Tree/hash format version**: `MERKLE_TREE_FORMAT_VERSION =
  "tracex-sha256-domain-separated-v1"`, recorded on every
  `MerkleCheckpointRecord`. Changing any rule above requires a new version
  string, never a silent edit to the existing one.
- **Range**: `[start_sequence, end_sequence]`, inclusive, and must exactly
  match a contiguous run of persisted sequence numbers for that case — a
  range with a gap (a missing sequence number) is rejected before any
  hashing happens.
- **Idempotent build**: an exact repeat of `(case_id, start_sequence,
  end_sequence)` returns the existing checkpoint and signature (`replayed
  =True`), no new row.
- **No cross-case checkpoints**: every query and table is scoped by
  `case_id`; a checkpoint for one case is never returned when queried
  under a different `case_id`.
- **No overlapping ranges within a case**: enforced by a **database-level**
  GiST exclusion constraint (`ck_merkle_checkpoints_no_overlap`, needs the
  `btree_gist` extension — see the migration), not only an
  application-level check-then-insert. This closes the race a
  check-then-insert alone would leave open between two concurrent
  checkpoint-builders for the same case.

## Ed25519 signing and verification

`app/modules/integrity/signing.py`. Key material:

- `Settings.integrity_signing_key: SecretStr | None` — base64-encoded
  32-byte raw Ed25519 private key, sourced only from environment/config.
  Never logged, returned in an API/CLI response, included in an error
  message, or committed. `.env.example` ships a blank placeholder only.
  Reuses the same blank-string-normalizes-to-`None` guard `Settings`
  already applies to `worker_token`/`worker_credential_pepper` (the
  documented Phase 2.1 `docker compose` blank-substitution bug).
- `Settings.integrity_signing_key_id: str` — a free-text label (default
  `"dev-local-ed25519-1"`) stored alongside every signature so a verifier
  can tell which configured key produced it. Not itself secret.
- `generate_signing_key_b64()` — a dev-only convenience (`cli.py
  generate-key`) that generates a random key and prints it once, for the
  operator to copy into a local, git-ignored `.env`. Never writes to disk
  or logs the value itself.

Every `CheckpointSignatureRecord` stores `key_id`, `algorithm`
(`"ed25519"`), `signature_encoding` (`"base64"`), the signature itself,
**and the signer's raw public key (`public_key_b64`)** plus its
fingerprint. Storing the public key — not secret, unlike the private key —
alongside the signature means `verify()` needs no access to `Settings` or
any external key store: verification works from the row's own public
material alone, satisfying "verification must work from public
verification material alone" without requiring the verifier to have the
operator's live configuration.

`verify()` never raises — a wrong public key, an altered root, an altered
signature, or malformed input all return `False` with no exception, so a
CLI/service caller gets a clean boolean rather than having to catch a
cryptography-library-specific exception type.

## Deterministic checkpoint verification (tamper detection)

`IntegrityService.verify_checkpoint` never trusts any previously computed
value. For a given `checkpoint_id` (case-scoped):

1. Re-fetch the checkpoint row and its signature row.
2. Re-fetch every `integrity_events` row in `[start_sequence,
   end_sequence]`, ordered by `sequence_number`.
3. Check the fetched count equals both `end_sequence - start_sequence + 1`
   and the checkpoint's own stored `leaf_count` (`leaf_count_matches`) —
   catches a deleted row (missing leaf).
4. Recompute the Merkle root from the current rows' leaf hashes
   (`root_matches`) — catches an altered `integrity_events` column, a
   swapped sequence order, or an altered `merkle_checkpoints.root_hash`
   value, all as a root mismatch.
5. Verify the stored signature against the stored root and public key
   (`signature_valid`) — catches a wrong key or an altered signature.

`ok = leaf_count_matches and root_matches and signature_valid`, with a
clear, non-secret `reason` string on failure (never a stack trace or raw
row content).

## Deliberate storage trade-off

No separate table persists each leaf's hash independently of the
`integrity_events` row it's derived from. This is intentional, not an
oversight: the task explicitly requires not "stor[ing] a full copy of
source payloads merely to make verification convenient", and a redundant
`checkpoint_leaves` table storing pre-computed leaf hashes would be exactly
that kind of convenience copy — every leaf hash is a pure, deterministic
function of already-persisted, immutable columns (`integrity_event_id`,
`case_id`, `event_kind`, `subject_type`, `subject_id`,
`canonical_payload_sha256`, `payload_schema_version`, `sequence_number`),
recomputing it is cheap, and recomputing it *is the tamper check* — a
cached copy would let a coordinated tamper of both the source row and its
cached leaf hash go undetected, which is strictly worse than not caching
at all.

The consequence: `integrity_events` rows must never be deleted or updated
outside this module's own (append-only) code paths for verification to
mean anything. Phase 6 Part 2 adds PostgreSQL triggers that reject ordinary
`UPDATE`/`DELETE` attempts on `integrity_events`, `merkle_checkpoints`, and
`checkpoint_signatures`. This does not protect against a PostgreSQL
superuser that disables triggers, an explicit operational trust boundary.

## Producer integration seams

Wired additively, behind an optional constructor/parameter default of
`None` so every pre-existing call site and test is unaffected (verified:
the full pre-existing `tests/unit/evidence_lifecycle` + `tests/unit/graph`
suite — 386 tests — passes unchanged with this phase's code in place).

| Producer boundary | File / call site | `event_kind` | Recorded only when |
|---|---|---|---|
| Evidence registration | `evidence_lifecycle.service.EvidenceLifecycleService.upload_evidence`, immediately after `repository.create_evidence_with_job` commits | `evidence_registered` | A genuinely new evidence row (never on an idempotent-replay path — those return earlier) |
| Observation-batch acceptance | `evidence_lifecycle.service.EvidenceLifecycleService.submit_observation_batch`, immediately after `repository.submit_observation_batch` commits | `observation_published` | `status=ACCEPTED` only (never on the `REPLAYED` path) |
| Correlation completion | `graph.intelligence.pipeline.run_case_correlation_pass`, immediately after `repository.submit()` returns | `correlation_completed` | `receipt.replayed is False` only |

Every seam follows the same **safe, non-blocking** contract, mirroring
`access_control.audit.record_audit_event_safely`'s "already-committed
state change" rationale exactly: the write this follows (evidence row,
observation batch, correlation) has already durably succeeded by the time
the integrity call runs, so a failure recording the integrity event is
caught and logged (`integrity.event_record_failed`) but never re-raised —
it must never turn an already-successful primary write into an error for
the caller, and must never roll back state that's already committed. This
is verified directly: `tests/integration/evidence_lifecycle
/test_integrity_producer_seam_live.py::
test_upload_still_succeeds_when_integrity_store_is_unreachable` points the
integrity recorder at a deliberately unreachable database and confirms
`upload_evidence` still succeeds.

`canonical_metadata` for each seam is a deliberately minimal, structural
dict — see the module docstring's "Why raw content never reaches storage"
section above for why this matters beyond just the validator.

Two producer boundaries are explicitly *not* wired to code that doesn't
exist yet: **review decisions** and **hypothesis actions**. Their
`IntegrityEventKind` values exist in the enum (forward-compatible), and
`record_integrity_event`/`IntegrityEventSubmission` already accept them —
Shreshtha's later work only needs to call the existing facade with
`event_kind=IntegrityEventKind.REVIEW_DECISION` (or `HYPOTHESIS_ACTION`)
and its own `subject_type`/`subject_id`/`canonical_metadata`; no new
integrity-module code should be needed.

## Verification and safe export

**No new HTTP endpoint is added in this phase.** The task's own guidance —
"Do not open a broad unauthenticated integrity-read endpoint... otherwise
provide the verified CLI/service path now and leave protected API exposure
to Aditya" — is taken at face value: `app/modules/integrity/dependencies.py`
provides FastAPI-style dependency providers (`get_integrity_service`) so a
later router can be wired in with the existing `require_case_action`-style
authorization pattern, but no router is registered in `app/main.py`, and no
route exists yet. This is a deliberate scope boundary, not an oversight.

The verification/export surface that *does* exist in this phase is a CLI,
`app/modules/integrity/cli.py`:

```bash
uv run python -m app.modules.integrity.cli generate-key
uv run python -m app.modules.integrity.cli build-checkpoint \
    --case-id <uuid> --start-sequence 1 --end-sequence 50
uv run python -m app.modules.integrity.cli verify \
    --case-id <uuid> --checkpoint-id <uuid>
uv run python -m app.modules.integrity.cli export \
    --case-id <uuid> --checkpoint-id <uuid> [--out bundle.json]
```

`verify` exits `0`/`1` on ok/not-ok, so it composes in a script or a future
CI/release gate. Neither `verify` nor `export` ever prints raw evidence
content or private key material — `export`'s `VerificationBundle` contains
only IDs, hashes, counts, enum values, and the signature's *public* key
material (see `models.py`'s `VerificationLeaf`/`VerificationBundle`).

## Handoff to later branches

- **Aditya** — wrap a future integrity-read/verify/export HTTP surface in
  the existing `require_case_action`/case-scoping authorization pattern
  (`app/modules/access_control/`), exactly like every other case-scoped
  endpoint. `IntegrityService`/`get_integrity_service` (in
  `app/modules/integrity/dependencies.py`) are ready to inject into a new
  router; none of this phase's code assumes HTTP exposure. Also: decide and
  enforce the production database grants needed for the append-only
  guarantee described in "Deliberate storage trade-off" above.
- **Jasraj** — document/CDR/finance provenance leaf inputs. Your module
  never needs to touch `app/modules/integrity/` directly; a future
  provenance-recording call (if your Part 3/4 work needs one beyond the
  existing `observation_published` seam Nipun already wired) goes through
  `IntegrityService.record_integrity_event` with your own
  `subject_type`/`subject_id`/minimal `canonical_metadata`, following the
  exact discipline in "Why raw content never reaches storage" above —
  never pass a full contract object, never include extracted text.
- **Gaurav** — media manifest/chunk/frame/geometry provenance leaf inputs,
  same facade, same discipline: IDs, counts, hashes, geometry — never raw
  frame bytes or OCR text.
- **Sarthak** — audio/social/message/chunk provenance leaf inputs, same
  facade, same discipline: IDs, counts, hashes, safe metadata — never
  transcript text or chat body.
- **Shreshtha** — review-decision and hypothesis-action recording, plus
  final Phase 6 graph/release integration. `IntegrityEventKind
  .REVIEW_DECISION`/`HYPOTHESIS_ACTION` already exist; call
  `record_integrity_event` from wherever your review/hypothesis workflow
  durably commits its decision, exactly like the three existing seams in
  this document's "Producer integration seams" section. No new integrity
  schema/migration should be needed for this — if your workflow's
  `subject_type`/`subject_id`/metadata shape doesn't fit the existing
  model, that's a signal to raise with Nipun before extending it
  unilaterally, not to build a parallel recording path.

## Non-goals (explicit)

No public blockchain anchoring; no external KMS/HSM; no automatic
signing-key rotation service; no review UI or review-decision workflow; no
hypothesis workflow or automatic hypothesis generation; no entity
merge/identity resolution; no new correlation or scoring algorithms; no ML
model selection/evaluation; no source-extractor changes; no local model
Docker containers; no three-laptop LAN deployment; no frontend work. See
`docs/decisions/ADR-013-phase-6-integrity-checkpoints.md` for the reasoning
behind the blockchain non-claim specifically.

## Known limitations

See `docs/qa/known-limitations.md`'s "Phase 6 Part 1 integrity foundation"
section for the full list, including: no DB-level immutability enforcement
(operational trust boundary, not application-enforced); a single local
Ed25519 key with no rotation workflow; no HTTP exposure yet (CLI/service
only); checkpoint building is an explicit operator/CLI action, not
automatic on a schedule or event count.
