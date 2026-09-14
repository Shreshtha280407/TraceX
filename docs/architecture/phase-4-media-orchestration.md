# Phase 4 — progressive media orchestration

## Boundary

The coordinator owns durable metadata and canonical observation ingestion;
workers own decoding and intelligence:

```text
evidence/job -> immutable manifest -> chunk publication -> PostgreSQL batch/outbox
                                                -> asynchronous Neo4j projection
```

`ChunkManifest` is an internal, versioned record. Its UUID5 identity covers the
canonical work definition (case/evidence/job, source and processor versions,
input object reference, configuration hash, and ordered boundaries), not
`created_at`. The same normalized definition recreates the same manifest and
chunk IDs after restart; a changed configuration creates a new identity.
Completed chunks and completed manifests are immutable.

Boundaries may be byte, time, frame, or source-locator boundaries, but the
coordinator never invents any. Object URIs are opaque metadata; no media bytes,
presigned URL, object-store credential, or source payload is persisted or
returned by the acknowledgement.

## Publication, lineage, and replay

`POST /api/v1/internal/worker-jobs/{job_id}/media-chunks/publish` is an
authenticated internal-worker endpoint. It accepts a `MediaChunkPublication`
whose embedded `ObservationBatchSubmissionV1` uses the existing canonical batch
validation. It never marks a job terminal and never writes Neo4j.

One PostgreSQL transaction inserts the ordinary batch, observations,
transformation provenance, optional progress event, and normal
`graph_projection_jobs` outbox rows; it also records derived-artifact metadata,
links observations to the chunk, marks the chunk complete, and writes a
checkpoint. A failure rolls back every one of those effects. Existing projection
workers later consume normal queued rows, so Phase 5 needs no raw-media
exception or direct graph write.

The chunk's canonical publication hash is the duplicate-delivery key. An
identical retry returns the existing receipt and creates no observation,
artifact, checkpoint, progress, or graph-outbox duplicate. A changed payload
for a completed chunk is a safe conflict. Database constraints enforce
manifest/chunk ordering, artifact idempotency, observation links, and checkpoint
identity.

Artifacts require a parent `evidence_id`, SHA-256 hex digest, producer and
configuration versions, safe opaque URI, kind/content type, and optional source
locator/byte size. The coordinator registers worker-supplied hashes; it does
not claim to compute them. Foreign scope and secret-bearing or inline URI values
are rejected.

## Checkpoints and status

Each checkpoint contains manifest/hash, processor/configuration version,
completed ordered chunk IDs, accepted batch IDs, artifact IDs, and timestamp.
It is valid only for its exact case/job/manifest. Resume reads the latest
checkpoint and skips completed chunks; replay remains harmless.

Partial observations remain durable while a job is `running`. Existing terminal
statuses are retained: `succeeded`, `failed`, and `deferred` use the existing
terminal-result path. A worker/GPU outage must report truthful `deferred` or
`failed` status with earlier chunks retained; it must not report success or
block unrelated source routes.

## Ownership

Gaurav and Sarthak produce manifests/boundaries, canonical observations,
provenance, and artifact metadata through this boundary; they do not receive
database, Neo4j, or MinIO credentials. Shreshtha consumes canonical partial
observations through the existing Phase 5 outbox/integration paths and owns
media taxonomy/correlation semantics. Aditya owns worker identity,
authorization, lease/retry, and transport-security policy. Jasraj owns
source-specific extraction validation. Real GPU/LAN workers, media datasets,
and full end-to-end deployment validation remain merge-wave work.
