# Runbook: Integrity Verification

Gap-Closure WP-8 (G18). How an operator or auditor independently verifies
TraceX's tamper-evident integrity chain, using only the CLI commands
already shipped in `app/modules/integrity/cli.py` — no code changes, no
special access beyond a configured `.env` and (for `verify`/`export`) a
reachable PostgreSQL instance.

See `docs/decisions/ADR-013-phase-6-integrity-checkpoints.md` for why
this is a locally-verifiable Merkle root + Ed25519 signature, not a
blockchain claim — this runbook is the practical "how", that ADR is the
"why".

## Prerequisites

```bash
cp .env.example .env   # if you haven't already
uv sync --all-groups
docker compose up -d postgres   # verify/export/build-checkpoint need Postgres reachable
```

All commands below run as `uv run python -m app.modules.integrity.cli <command>`.

## 1. Generating and rotating a signing key

A fresh deployment needs a signing key before any checkpoint can be built:

```bash
uv run python -m app.modules.integrity.cli generate-key
```

This prints a base64-encoded Ed25519 private key to stdout — copy it into
your local, git-ignored `.env` as `INTEGRITY_SIGNING_KEY`. **Never commit
it, log it, or paste it anywhere else.** `INTEGRITY_SIGNING_KEY_ID`
defaults to `dev-local-ed25519-1`; give it a real, unique label in a
non-dev deployment.

To rotate to a new key later:

1. Run `generate-key` again for a **new** private key.
2. Update `.env`: the new `INTEGRITY_SIGNING_KEY`, and a **new**
   `INTEGRITY_SIGNING_KEY_ID` (never reuse an old key_id for different
   key material — the registry below rejects that).
3. Register the now-configured key's public identity into the durable,
   append-only registry:
   ```bash
   uv run python -m app.modules.integrity.cli rotate-key
   ```
4. Audit every key this deployment has ever used:
   ```bash
   uv run python -m app.modules.integrity.cli list-keys
   ```
   Every checkpoint signed under an old key still verifies correctly —
   each signature stores its own signer's public key (see step 3 below);
   rotation only changes which key signs *new* checkpoints.

## 2. Sealing a checkpoint

An integrity checkpoint seals a contiguous, case-scoped range of
`integrity_events` rows into a signed Merkle root.

**Manual, one case at a time** (you already know the sequence range —
e.g. from `GET /api/v1/cases/{case_id}/audit` or your own tracking):

```bash
uv run python -m app.modules.integrity.cli build-checkpoint \
  --case-id <uuid> --start-sequence 1 --end-sequence 50
```

**Automatic, every case with pending events** (discovers ranges from the
existing `integrity_sequence_counters` table against the latest sealed
checkpoint per case — see ADR-023):

```bash
uv run python -m app.modules.integrity.cli checkpoint-once
```

For a continuously running sweep (an operator-managed process, container,
or systemd unit — not started by `docker compose up` on its own):

```bash
uv run python -m app.modules.integrity.cli checkpoint-loop --poll-interval-seconds 60
```

Sealing the same exact range twice is idempotent (returns the existing
checkpoint, `replayed: true`) — safe to re-run `checkpoint-once`/
`build-checkpoint` after a failure without risk of a duplicate or
conflicting checkpoint.

## 3. Verifying a checkpoint

```bash
uv run python -m app.modules.integrity.cli verify \
  --case-id <uuid> --checkpoint-id <uuid>
```

This **independently recomputes** the Merkle root from the current,
live `integrity_events` rows and checks it against the sealed root, then
checks the Ed25519 signature against the public key stored alongside it
— it never trusts a previously computed value. Exits non-zero on any
failure (composable in a script or CI gate). A failure means one of:

- **Leaf count mismatch** — an event in the sealed range is missing
  (should be structurally impossible given the append-only trigger, but
  this is exactly the check that would catch a direct, out-of-band
  database edit bypassing it).
- **Root mismatch** — a leaf's content changed since it was sealed.
- **Signature invalid or missing** — the checkpoint's signature doesn't
  match its own stored public key and root hash.

## 4. Exporting and archiving a verification bundle

`export` prints (or writes to a file) a portable bundle of only public
verification material — never raw evidence content, never a private key:

```bash
uv run python -m app.modules.integrity.cli export \
  --case-id <uuid> --checkpoint-id <uuid> --out bundle.json
```

`archive` (Gap-Closure WP-5) does the same, but writes durably and
write-once to a configured sink instead of stdout/a local file you manage
yourself — for an offsite/independent copy outside PostgreSQL:

```bash
# Local filesystem (INTEGRITY_MANIFEST_FILESYSTEM_ROOT, default ./data/integrity-manifests):
uv run python -m app.modules.integrity.cli archive \
  --case-id <uuid> --checkpoint-id <uuid> --sink filesystem

# MinIO (INTEGRITY_MANIFEST_BUCKET, separate from raw evidence):
uv run python -m app.modules.integrity.cli archive \
  --case-id <uuid> --checkpoint-id <uuid> --sink minio
```

`archive` refuses to overwrite an already-archived checkpoint (write-once
at the application level — see ADR-023 for why this isn't backed by real
MinIO object-lock/retention in this phase).

## 5. Repairing missing integrity events (reconciliation)

If a primary write succeeded but its best-effort integrity-event
recording failed (logged as `integrity.event_record_failed` at the time),
`IntegrityReconciliationService` durably repairs it — scanning only
already-committed source records, never inventing content. There is no
CLI wrapper for this yet; it's invoked programmatically
(`app/modules/integrity/reconciliation.py::IntegrityReconciliationService.
reconcile_case`) — see that module's docstring for the exact scan scope
(evidence, correlations, structured/modality provenance, review/
hypothesis decisions, entity-resolution decisions, case notes, as of
Gap-Closure WP-5).

## Interpreting a failed verification

A `verify` failure is a serious finding — it means the sealed record no
longer matches what's live in the database. Before assuming tampering:

1. Confirm you're checking the right `case_id`/`checkpoint_id` pair
   (verification is always case-scoped; a checkpoint never verifies
   against the wrong case).
2. Check whether a database restore/rollback happened between when the
   checkpoint was sealed and now (a legitimate restore to an earlier
   state would also fail verification against a *later* checkpoint — see
   `backup-restore.md`'s guidance on re-verifying after a restore).
3. If neither explains it, treat it as a genuine integrity incident: the
   `integrity_events`/`merkle_checkpoints`/`checkpoint_signatures` tables
   are append-only at the database level (a `BEFORE UPDATE OR DELETE`
   trigger rejects any mutation attempt through ordinary application
   roles) — a real mismatch means either that protection was bypassed by
   a fully-privileged database operator, or the underlying storage was
   altered outside PostgreSQL entirely.
