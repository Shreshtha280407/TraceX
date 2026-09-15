# ADR-013: Phase 6 integrity checkpoints — Merkle + Ed25519, not a blockchain

Status: **accepted**. Owner: Nipun. Related: `docs/architecture/
phase-6-integrity.md` (full design), `docs/qa/known-limitations.md`
("Phase 6 Part 1 integrity foundation").

## Context

Phase 6's objective is a tamper-evident backend foundation: durable
evidence/observation/correlation (and later review/hypothesis) writes
should produce a canonical, safe integrity leaf; leaves should be sealed
into a case-scoped, deterministic Merkle checkpoint; a checkpoint's root
should be signed; and both membership and the signature should be
independently verifiable, without ever requiring raw source content or a
private key to be present at verification time.

The task explicitly frames this as **detecting tampering with recorded
integrity metadata**, not as a public-blockchain anchoring claim. This ADR
records why that framing is correct and durable, not just a Phase 6
Part 1 simplification to be "upgraded to a real blockchain" later.

## Decision

1. **SHA-256** as the sole hash algorithm, used with **domain separation**:
   a leaf hash and a parent (internal node) hash use different fixed byte
   prefixes (`b"tracex.integrity.leaf.v1\x1f"` /
   `b"tracex.integrity.node.v1\x1f"`) before hashing, so a leaf's hash can
   never collide with a parent's hash for the same bytes — a well-known
   defense against a second-preimage attack on unbalanced Merkle trees
   (CVE-style "leaf/node confusion").
2. **Leaf order is `(case_id, sequence_number)`, never database-return
   order or wall-clock order.** `sequence_number` is a per-case,
   atomically-allocated, gap-free counter (see `phase-6-integrity.md`'s
   "Idempotency and sequencing"), not the event's own `created_at` —
   wall-clock timestamps can collide, can be affected by clock skew across
   processes, and (more importantly) are never the caller-facing ordering
   key producers reason about; the sequence number is.
3. **Odd-node behavior: duplicate the final hash at that level.** This is
   the same rule Bitcoin's Merkle tree uses, chosen for simplicity and
   because it's a well-understood, well-analyzed convention — not chosen
   to imply any actual relationship to a blockchain.
4. **Checkpoints are case-scoped and range-bounded**
   (`[start_sequence, end_sequence]`), never a single running tree over
   an unbounded, cross-case event log. A checkpoint is an explicit,
   operator/CLI-triggered action, not an automatic per-event commit — this
   keeps the signing/verification unit small and independently reviewable,
   and keeps a checkpoint's meaning simple ("this exact, named range of
   this exact case's events was sealed at this time").
5. **Ed25519, signed locally, key material from environment/config only.**
   No external KMS/HSM, no rotation service, no multi-signer scheme. The
   signer's raw public key is stored alongside its signature (see
   `phase-6-integrity.md`'s "Ed25519 signing and verification"), so
   verification never needs the private key, `Settings`, or any live
   configuration — only the checkpoint/signature row itself.

## Why this is explicitly *not* a blockchain claim

A blockchain's meaningful security properties — a decentralized,
economically-costly-to-rewrite consensus history; independent verification
by parties who don't trust the operator or one another; resistance to a
single administrator silently rewriting history — depend on distribution
across independent, mutually-distrusting nodes. None of that exists here:

- There is **one** database and **one** signing key, both under the
  operating organization's control. An actor with both database write
  access and the private signing key can alter `integrity_events` rows
  *and* re-sign a new, consistent-looking checkpoint over the altered
  data. A real blockchain's anchoring value comes precisely from making
  that infeasible; a local Merkle+signature scheme cannot make that claim,
  and this document does not make it.
- There is no timestamping authority independent of this system's own
  clock. A signature proves "the holder of this key attested to this root
  at approximately this time, as measured by this system," not "this root
  existed at this UTC instant, provably, to a party outside this system."
- There is no cross-organization or cross-instance verification. A
  verifier needs read access to this database (or an exported bundle) and
  the corresponding public key — there is no public ledger a third party
  can check against independently of trusting this deployment.

**What this design *does* prove, and proves well**, is the narrower,
genuinely useful guarantee the task asked for: **internal tamper
detection**. Any of the following is detected by `verify_checkpoint`,
purely through recomputation against the live database:

- A stored leaf's underlying `integrity_events` column was altered after
  the checkpoint was sealed (leaf-hash mismatch → root mismatch).
- A leaf was deleted (leaf-count mismatch).
- Two leaves' effective order was altered (sequence-number swap → root
  mismatch, since the recomputed tree is order-sensitive).
- The checkpoint's own `root_hash` column was altered directly (recomputed
  root, from the still-correct events, no longer matches the tampered
  stored root).
- The signature was altered, or a different key signed it (signature
  verification fails against the stored public key).

This is a real, valuable guarantee for an evidentiary system: it means an
investigator, auditor, or court can be shown that a specific, named
checkpoint's data is *internally consistent* with its signature as of the
moment of verification, and any deviation from the sealed state is
detectable. It is simply a different (and smaller) guarantee than a public
blockchain's, and this ADR exists so no later phase, demo, or external
description overstates it.

## What would be needed for a stronger claim (explicitly out of scope)

Recorded here so the gap is a documented, deliberate deferral, not an
unstated one:

- An external, independent timestamping service (e.g., RFC 3161) to
  anchor a checkpoint root to a time no single administrator controls.
- Publishing checkpoint roots to a public, append-only, distributed store
  (which is what "blockchain anchoring" would actually mean) so a third
  party can verify a root's existence without trusting this deployment at
  all.
- Splitting signing authority across multiple independent key holders
  (threshold/multi-signature), so no single compromised or coerced actor
  can re-sign an altered history alone.
- External KMS/HSM-backed key storage so the private key is never
  directly readable even by an administrator with database access.

None of these are built in Phase 6 Part 1 — they are explicitly named
non-goals in `docs/architecture/phase-6-integrity.md`, not implementation
gaps to silently work around.

## Alternatives considered

- **A hash chain (each event's hash includes the previous event's hash)
  instead of a Merkle tree.** Rejected: a hash chain makes verifying a
  *subset* or proving membership of one event without replaying the
  entire chain from the start much more expensive, and doesn't naturally
  support the "checkpoint over a bounded range" shape this task asks for.
  A Merkle tree's `O(log n)` membership-proof shape is the standard choice
  for exactly this use case.
- **HMAC instead of Ed25519.** Rejected: HMAC is symmetric — verification
  would require the same secret used to sign, meaning any verifier capable
  of checking a signature could also forge one. Ed25519 (asymmetric) lets
  verification use only public material, matching the task's explicit
  "verification must work from public verification material alone"
  requirement.
- **A single global (not per-case) sequence/Merkle log.** Rejected: this
  project's core evidentiary-integrity requirement is case isolation
  (`CLAUDE.md`'s "No automatic identity merge, guilt conclusion, or
  case-unscoped retrieval") — a cross-case log would make it structurally
  possible for a checkpoint verification path to leak that another case
  even exists, and would complicate the exact case-scoping guarantee every
  other module in this codebase already enforces.
