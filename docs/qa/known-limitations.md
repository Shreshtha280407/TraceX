# Known Limitations — Phase 1

These are intentional, scoped-out gaps, not oversights. Each belongs to a later phase.

- **No real ingestion pipeline.** `EvidenceRecordV1` is a contract only; there is no upload endpoint, no MinIO write path, and no real SHA-256 computation over uploaded bytes.
- **No source extractors.** No OCR, ASR/transcription, video/image analysis, CDR parser, financial parser, or social/chat parser exists. `ObservationV1` is the target shape they'll all produce.
- **No entity resolution.** `EntityV1.created_from_observation_ids` exists, but nothing in this repo actually resolves raw `ExtractedEntityMention`s into entities, merges duplicates, or deduplicates aliases.
- **No graph projection.** Neo4j is provisioned and readiness-checked, but nothing writes the entity/event graph into it yet.
- **No cross-modal correlation, candidate scoring, or hypothesis engine.**
- **No authentication or RBAC/ABAC enforcement.** `EvidenceRecordV1.classification` is a label field only; nothing enforces access control based on it.
- **No case CRUD.** There's no way to create/list/update a case through the API yet — `case_id` fields exist on every contract but nothing issues or validates them against a real case record.
- **No Merkle checkpoints or signatures.** `app/core/canonical.py` provides stable hashing as a utility; no chain, checkpoint, or Ed25519 signature is implemented.
- **No database migrations beyond the Alembic baseline.** `migrations/versions/3e8cbaa07711_baseline.py` establishes version tracking only; there are no domain tables to migrate.
- **`/readyz` checks are liveness/connectivity only.** They open a minimal connection (`SELECT 1`, `RETURN 1`, `PING`, `bucket_exists`) and nothing more — they don't validate schema state, credentials scope, or bucket policy.
- **Live integration tests depend on local environment state.** `tests/integration/test_readiness_live.py` only runs meaningfully when a developer has a real `.env` and has started `docker compose up -d postgres neo4j redis minio`; in CI or a fresh clone without that, all four of its tests report as skipped (not failed) — this is by design, not a gap in coverage of the default `uv run pytest` run, but it does mean the *default* run doesn't prove real infrastructure connectivity.
- **No load, chaos, or performance testing.** Out of scope for a Phase 1 foundation.
- **CI runs verification but isn't wired to branch protection.** `.github/workflows/ci.yml` runs `ruff format --check`, `ruff check`, `mypy app`, `pytest`, and `docker compose config` on push/PR, but enforcing it as a required check is a GitHub repository-settings change, out of scope for this task.
