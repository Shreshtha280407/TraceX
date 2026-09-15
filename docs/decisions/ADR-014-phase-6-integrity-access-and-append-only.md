# ADR-014: Protected integrity access and append-only enforcement

Status: accepted. Owner: Aditya. Date: 2026-09-15.

Integrity checkpoint operations are case-scoped. The access-control policy
has separate `integrity_read`, `integrity_verify`, and `integrity_export`
actions: owners/managers may export; investigators, analysts, and reviewers
may read/verify; viewers have none. Authentication and authorization happen
before a checkpoint is queried. Thus membership failures and cross-case
attempts use the established indistinguishable 403 response and do not
reveal checkpoint existence.

The API exposes safe checkpoint metadata only. Export retains Part 1's
portable verification bundle: public key material and safe leaf hashes/IDs,
never a private key, source body, object URI, transcript, OCR text, token,
or internal exception detail. Verification uses stored public material only
and does not sign.

PostgreSQL triggers reject `UPDATE` and `DELETE` on integrity events,
Merkle checkpoints, and checkpoint signatures. Inserts remain allowed. This
is a normal-role application/database trust boundary; a database superuser
can disable triggers and is therefore not covered by an absolute tamper-proof
claim.

Primary writes remain available when a later integrity write fails. The safe
failure signal is structured and a bounded operator command reconciles a
case by rebuilding evidence-registration submissions only from durable safe
metadata. Retry is idempotent. No scheduler or background worker is added.
