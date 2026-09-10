# Test Data

## No real evidence

Nothing in this repository — fixtures, tests, or documentation examples — uses real, sensitive, or production investigative material. TraceX handles genuinely sensitive data once later phases start real ingestion; until then, every value below is synthetic and safe to commit.

## Synthetic fixtures

All contract test data is generated in-process by `tests/fixtures/factories.py` (`make_evidence_record`, `make_observation`, `make_entity`, `make_event`, `make_worker_job`, `make_worker_result`, plus the smaller `make_source_locator`/`make_extractor` builders). Each factory returns a fully-valid instance with a fixed synthetic timestamp (`FIXED_TIME = 2026-01-01T12:00:00Z`) and placeholder values — e.g. `"John Doe"` / `"analyst-1"` / `sha256="a"*64` — so tests are deterministic and never depend on real-world identifiers. Individual tests override specific fields via keyword arguments to construct edge cases (invalid confidence, empty locator, malformed idempotency key, etc.) without duplicating every other required field.

## Test-only configuration values

`tests/conftest.py` sets safe, syntactically-valid placeholder environment variables (fake DSNs pointing at `localhost`, passwords literally named `test-only`) before any test module imports `app.main`, so the suite never depends on a developer having created a real `.env` first. These values are never used to open a real connection in the default (`uv run pytest`) run — see `docs/qa/known-limitations.md`.

## Live-infrastructure test data

`tests/integration/test_readiness_live.py` reads real connection details from a developer's own `.env` (git-ignored, never committed) when present, and only exercises whichever of PostgreSQL/Neo4j/Redis/MinIO are actually reachable via `docker compose up -d postgres neo4j redis minio`. No fixture data is written to these services in Phase 1 — the checks are pure connectivity/liveness probes (`SELECT 1`, `RETURN 1`, `PING`, `bucket_exists`), not data round-trips, since Phase 1 defines no domain tables or evidence writes.

## Graph fixtures (`tests/fixtures/graph/factories.py`)

`make_linked_graph_fixture()` builds a single, internally-consistent, synthetic `EvidenceRecordV1 -> ObservationV1 -> EntityV1 -> EventV1` chain on top of the base `tests/fixtures/factories.py` builders — wiring `case_id`/`evidence_id`/`observation_id`/`entity_id` together so `app/modules/graph/projection.py` can apply the whole chain cleanly (the base factories are each independently random by default and don't guarantee that). Accepts `case_id=`/`entity_id=` overrides so tests can deliberately reuse an ID across two fixtures — e.g. `tests/integration/graph/test_case_isolation.py` projects the same `entity_id` into two different cases to prove they never share graph identity. Same rule as everywhere else in this repo: every value is synthetic, no real evidence or identifiers.

`tests/integration/graph/` writes real (synthetic) nodes into a live Neo4j instance and always tears them down afterward — every test that projects data does so under a freshly-generated `case_id` and `DETACH DELETE`s everything under it in a fixture/test teardown, so repeated runs never accumulate leftover graph data.
