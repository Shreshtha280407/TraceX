# Phase 4 LAN worker security and reliability

Owner: Aditya. Status: **in progress**.

Workers use the existing revocable per-worker bearer credential, processor-scoped
claim, and one-time claim token. Result, batch, media-chunk, input, renew, and
heartbeat routes require the verified worker identity plus the live job token.
Job ownership supplies case/evidence scope; invalid, revoked, wrong-worker,
wrong-job, and expired credentials/tokens return generic safe failures. Tokens
and headers are never logged or persisted raw.

`WORKER_SECURE_TRANSPORT_REQUIRED=true` rejects insecure control-plane
requests. Direct HTTPS is accepted; forwarded HTTPS is accepted only from an
immediate peer in `WORKER_TRUSTED_PROXY_IPS`. Local HTTP is an explicit false
setting, not an inferred proxy trust. Certificates and private keys remain
operator-managed and are never committed.

The control-plane guard rejects declared oversized bodies before parsing.
Typed settings bound request bytes, observations, transformations, and media
artifacts. These routes accept canonical metadata only, never raw media.

`POST /api/v1/internal/worker-jobs/{job_id}/heartbeat` renews the durable
lease, avoiding a competing lifecycle state machine. Lease/updated-at state
derives active, degraded, stale, unavailable, and recovery operations without
discarding partial batches, checkpoints, artifacts, or graph-outbox work.
Lease expiry/retry exhaustion remains authoritative; a restarted worker resumes
through Nipun's checkpoint boundary. Workers never receive PostgreSQL, Neo4j,
Redis, MinIO, object URI, or storage credentials.
