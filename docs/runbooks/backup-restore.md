# Runbook: Backup and Restore

Gap-Closure WP-8 (G18). Procedures for backing up and restoring TraceX's
three stateful services (PostgreSQL, Neo4j, MinIO) in a `docker compose`
deployment (`compose.yaml`). No new backup tooling ships with this
change — every command below is a standard operation against the same
images/volumes `compose.yaml` already defines; this runbook exists
because no such procedure was previously documented anywhere in this
repository.

**Test every restore procedure against a disposable environment before
you need it for real.** This runbook is not a substitute for a real
disaster-recovery drill.

## What needs backing up

| Volume/service | Contains | Backup method |
|---|---|---|
| `postgres-data` | Every relational table: `users`, `cases`, `evidence_records`, `worker_jobs`, `integrity_events`, `merkle_checkpoints`, `checkpoint_signatures`, `signing_keys_public`, `case_notes`, entity/review/hypothesis tables, etc. | `pg_dump` (preferred) or volume snapshot |
| `neo4j-data` | The projected case graph (`Entity`/`Event`/`Observation`/... nodes and relationships) — a rebuildable *projection* of PostgreSQL data, not a second source of truth | `neo4j-admin database dump` (preferred) or volume snapshot |
| `minio-data` | Raw evidence object bytes | `mc mirror` (preferred) or volume snapshot |
| `media-models-data` | Bootstrapped, checksum-verified model weights | Not backup-worthy — re-derivable via `docker compose --profile gpu-worker run --rm media-model-bootstrap` |

Neo4j's graph is **derived** from PostgreSQL (every projection write is
replayable from the durable outboxes — `graph_projection_jobs`,
`graph_update_events`, `review_hypothesis_projection_events`). In a real
incident where only one of the two can be restored in time, PostgreSQL is
the priority; a lost Neo4j graph can in principle be rebuilt by replaying
projection from PostgreSQL's own durable records, though no single CLI
command does that rebuild end-to-end today — treat "restore Neo4j too"
as the normal path, not "PostgreSQL alone is sufficient."

## Backing up

### PostgreSQL (preferred: `pg_dump`)

```bash
docker compose exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --format=custom --file=/tmp/tracex-backup.dump
docker compose cp postgres:/tmp/tracex-backup.dump ./backups/tracex-$(date +%Y%m%dT%H%M%S).dump
```

`--format=custom` produces a compressed, `pg_restore`-only file (not
plain SQL) — smaller, and supports selective/parallel restore later.

### Neo4j (preferred: `neo4j-admin database dump`)

Neo4j's offline dump requires the database to be stopped first (Neo4j
Community edition has no online/hot dump):

```bash
docker compose stop neo4j
docker compose run --rm neo4j neo4j-admin database dump neo4j --to-path=/data/backups
docker compose cp neo4j:/data/backups/neo4j.dump ./backups/tracex-graph-$(date +%Y%m%dT%H%M%S).dump
docker compose start neo4j
```

### MinIO (preferred: `mc mirror`)

```bash
docker run --rm --network tracex_tracex-internal \
  -e MC_HOST_tracex="http://${MINIO_ACCESS_KEY}:${MINIO_SECRET_KEY}@minio:9000" \
  -v "$(pwd)/backups/minio-$(date +%Y%m%dT%H%M%S):/backup" \
  minio/mc mirror tracex/"${MINIO_BUCKET:-tracex-evidence}" /backup
```

### Fallback: raw volume snapshot (any service)

When a service-native dump tool isn't available/practical, snapshot the
Docker volume directly (works for any of the four volumes above):

```bash
docker run --rm -v tracex_postgres-data:/source:ro \
  -v "$(pwd)/backups:/backup" alpine \
  tar czf /backup/postgres-data-$(date +%Y%m%dT%H%M%S).tar.gz -C /source .
```

Substitute the volume name (`tracex_neo4j-data`, `tracex_minio-data`) as
needed — Compose prefixes volume names with the project name (`tracex`,
from `compose.yaml`'s top-level `name:`), confirm with `docker volume ls`
if your project name differs from the default.

## Restoring

**Stop the affected service(s) before restoring** — restoring into a
live, writing database risks a corrupt or inconsistent result.

### PostgreSQL

```bash
docker compose stop api  # stop writers first
docker compose cp ./backups/tracex-<timestamp>.dump postgres:/tmp/restore.dump
docker compose exec postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --clean --if-exists /tmp/restore.dump
docker compose start api
```

### Neo4j

```bash
docker compose stop neo4j
docker compose cp ./backups/tracex-graph-<timestamp>.dump neo4j:/data/backups/neo4j.dump
docker compose run --rm neo4j neo4j-admin database load neo4j \
  --from-path=/data/backups --overwrite-destination=true
docker compose start neo4j
```

### MinIO

```bash
docker run --rm --network tracex_tracex-internal \
  -e MC_HOST_tracex="http://${MINIO_ACCESS_KEY}:${MINIO_SECRET_KEY}@minio:9000" \
  -v "$(pwd)/backups/minio-<timestamp>:/backup" \
  minio/mc mirror /backup tracex/"${MINIO_BUCKET:-tracex-evidence}"
```

### Raw volume restore (fallback)

```bash
docker compose down postgres   # or neo4j/minio
docker volume rm tracex_postgres-data
docker volume create tracex_postgres-data
docker run --rm -v tracex_postgres-data:/target \
  -v "$(pwd)/backups:/backup" alpine \
  tar xzf /backup/postgres-data-<timestamp>.tar.gz -C /target
docker compose up -d postgres
```

## After any restore: re-verify integrity

A restore changes what "live" data looks like — always confirm it landed
correctly before trusting it, using the existing integrity-checkpoint
chain (see `docs/runbooks/integrity-verification.md` for full detail):

```bash
uv run python -m app.modules.integrity.cli verify --case-id <uuid> --checkpoint-id <uuid>
```

- A checkpoint sealed **before** the restore point should still verify
  successfully — if it doesn't, the restore itself is suspect (wrong
  backup, corrupted dump, or a restore that didn't fully complete).
- A checkpoint sealed **after** the restore point (i.e. events that
  existed in the live database but were never in the backup you restored
  from) will legitimately fail verification — this is *expected data
  loss from the restore*, not tampering. Cross-check restored `merkle_
  checkpoints.end_sequence` per case against what you expected to
  recover, and treat any gap as data that needs to be re-ingested or
  accepted as lost, not silently ignored.
- If PostgreSQL and Neo4j were restored from backups taken at different
  times, the graph may now reference `case_id`/`entity_id` values that no
  longer exist in PostgreSQL (or vice versa) — restore both from
  backups taken as close together in time as practical, and treat a
  significant time gap between them as a known-inconsistent state to
  reconcile, not a fact to build on.
