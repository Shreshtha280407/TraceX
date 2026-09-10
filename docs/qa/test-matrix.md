# Phase 1 Test Matrix

Each row is a tracked QA item: what it verifies, who owns it, how to run it, and where the current result is recorded. Actual run output/history lives in `docs/qa/test-results.md` — this file defines the matrix, that file records outcomes.

| ID | Owner | Expected behavior | Test command | Status | Result |
|---|---|---|---|---|---|
| CORE-CONTRACT-001 | Nipun | Every canonical contract (`EvidenceRecordV1`, `ObservationV1`, `EntityV1`, `EventV1`, `WorkerJobV1`, `WorkerResultV1`) parses a valid fixture, round-trips through JSON without semantic change, and rejects an unsupported `schema_version`. | `uv run pytest tests/contract/test_evidence.py tests/contract/test_observation.py tests/contract/test_entity.py tests/contract/test_event.py tests/contract/test_worker.py` | Passing | See `docs/qa/test-results.md` |
| CORE-CONTRACT-002 | Nipun | Field-level contract validation rules hold: empty `source_locator` rejected; `time_end_ms < time_start_ms` rejected; confidence outside `[0,1]` rejected; invalid/degenerate normalized bounding box rejected; `EventV1` requires `event_time` or `time_window`; malformed worker idempotency key rejected; `WorkerResultV1` status/error consistency enforced. | `uv run pytest tests/contract/test_common.py tests/contract/test_observation.py tests/contract/test_event.py tests/contract/test_worker.py` | Passing | See `docs/qa/test-results.md` |
| CORE-ID-001 | Nipun | `deterministic_uuid` is stable for identical normalized input, changes when input changes, and doesn't collide across part boundaries; `canonical_bytes`/`canonical_sha256` are stable regardless of dict/key insertion order and handle UUID/datetime/enum/nested-model values deterministically. | `uv run pytest tests/unit/test_ids.py tests/unit/test_canonical.py` | Passing | See `docs/qa/test-results.md` |
| CORE-CONFIG-001 | Nipun | Missing required settings (`POSTGRES_DSN`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `REDIS_URL`, `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`) or an invalid value (e.g. malformed DSN) fails `Settings()` construction with a clear `ValidationError`, not a downstream crash. | `uv run pytest tests/unit/test_config.py` | Passing | See `docs/qa/test-results.md` |
| CORE-HEALTH-001 | Nipun | `GET /healthz` returns `200` with `{"status": "ok", "service": "tracex-api", "version": ...}`, and the FastAPI app assembles and serves routes on startup (no dependency required). | `uv run pytest tests/unit/test_api_health.py::test_healthz_returns_200 tests/unit/test_app_startup.py` | Passing | See `docs/qa/test-results.md` |
| CORE-READY-001 | Nipun | `GET /readyz` returns `200` with per-dependency `"ok"` status when all of PostgreSQL/Neo4j/Redis/MinIO are healthy (mocked), and `503` with the safe `{"error": {...}}` envelope — never a connection string, credential, or stack trace — when any dependency fails. | `uv run pytest tests/unit/test_api_health.py tests/security/test_no_secret_leakage.py` | Passing | See `docs/qa/test-results.md` |
| CORE-COMPOSE-001 | Nipun | `compose.yaml` is syntactically valid and resolvable by Docker Compose (`docker compose config`). | `uv run pytest tests/integration/test_compose_config.py` (or directly `docker compose config`) | Passing | See `docs/qa/test-results.md` |

## Coverage beyond the core matrix

- `tests/e2e/test_boot_smoke.py` — end-to-end boot + `/healthz` → `/readyz` → `/api/v1/meta/contracts` sequence against a real ASGI app instance.
- `tests/integration/test_readiness_live.py` — real connections to PostgreSQL/Neo4j/Redis/MinIO when a real `.env` and running infrastructure are present; self-skips per-dependency otherwise (never fabricates a pass).
- `tests/security/test_no_secret_leakage.py` — the generic unhandled-exception handler and `/readyz` never echo exception text or secrets, independent of any specific route.

## Full suite

```bash
uv run pytest
```
