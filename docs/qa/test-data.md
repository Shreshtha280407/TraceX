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

## Document/structured-processing fixtures (`tests/fixtures/structured_processing/`)

`builders.py` constructs real, minimal file bytes for every supported format directly — no `reportlab` (not an allowed dependency for this phase): PDFs are hand-assembled as a minimal valid single-font byte stream (`build_minimal_pdf`, supports `None` per page to simulate a scanned/image-only page) plus `build_encrypted_pdf` via `pypdf`'s own writer; DOCX/XLSX (`build_docx`/`build_xlsx`) are built with the same `python-docx`/`openpyxl` libraries the app parses them with. `factory.py`'s `make_evidence_and_job` builds a matched `EvidenceRecordV1`/`WorkerJobV1` pair with a fixed synthetic timestamp, following the same pattern as `tests/fixtures/factories.py`. Every FIR-style fixture text (FIR numbers, phone numbers, emails, vehicle plates, amounts) is synthetic and invented for these tests — none of it corresponds to a real case, person, or account.

## Access-control fixtures (`tests/fixtures/access_control/`)

`factories.py`'s `make_user_record`/`make_case_record`/`make_membership_record` follow the same pattern as `tests/fixtures/factories.py`: fully-valid instances with a fixed synthetic timestamp and placeholder values (`"analyst-<random>@example.test"`, `"Test Analyst"`) that tests override via keyword arguments. `DEFAULT_PASSWORD` (`"correct-horse-battery-staple"`, the canonical XKCD example password — never a real credential) is hashed once at module load and reused across fixtures to avoid paying Argon2id's deliberate per-call cost for every test that doesn't care about the plaintext.

`fake_repository.py`'s `FakeAccessControlRepository` is an in-memory, duck-typed stand-in for `AccessControlRepository` implementing the identical async method signatures, used by every unit test in `tests/unit/access_control/` and `tests/security/access_control/` so `service.py`/`sessions.py`/`dependencies.py`/the FastAPI router can be exercised without a real PostgreSQL instance. It does **not** enforce foreign-key constraints the way real PostgreSQL does — `tests/integration/access_control/` exists specifically to catch bugs that only manifest against real constraint enforcement (see `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 7, for a bug this distinction actually caught).

`tests/integration/access_control/` writes real (synthetic) rows into a live PostgreSQL instance and always cleans them up afterward — every test that creates a user/case does so via the `cleanup_user_ids`/`cleanup_case_ids` fixtures, which `DELETE` those rows (cascading to their memberships/sessions) in teardown, so repeated runs never accumulate leftover state. It applies the Alembic migration itself, once per test session, via a genuinely separate subprocess with `tests/conftest.py`'s fake test-environment variables stripped from its environment — otherwise `migrations/env.py`'s own `get_settings()` call would read the fake `POSTGRES_DSN` `tests/conftest.py` sets for the rest of the suite instead of this suite's real `.env`. Every email used is synthetic and randomly suffixed (`unique_email()` in `conftest.py`) to avoid colliding with a previous run's leftover-but-not-yet-cleaned data.

## Audio/social/alias/communication-link fixtures (`tests/fixtures/communication_processing/`)

`builders.py` constructs everything from scratch, all synthetic: `build_wav_bytes` writes a real, valid, silent PCM WAV via the stdlib `wave` module (no audio library, no real recording); `build_fake_mp3_bytes`/`build_fake_m4a_bytes`/`build_fake_ogg_bytes` are minimal magic-byte-only stand-ins for unsupported-format routing tests; `build_whatsapp_export`/`build_telegram_export`/`build_instagram_export`/`build_generic_json_export` build the exact documented shapes each parser accepts, with invented sender names (`"Alice"`, `"Bob"`), invented message text, and invented (non-dialable, non-existent) phone-number-*shaped* strings used only to prove format-detection/exclusion logic (e.g. `9876543210` in transliteration-exclusion tests) — never a real phone number, account, or export. `factory.py`'s `make_job` builds a `WorkerJobV1` with a fixed synthetic timestamp, following the same pattern as `tests/fixtures/factories.py`.

Devanagari/Gurmukhi test fixtures (`tests/unit/communication_processing/test_aliases_transliteration.py`, `test_aliases_scripts.py`) use common, generic given names (e.g. राम "Ram", सिंह "Singh" as a surname element) purely as known-good inputs to a deterministic character-mapping algorithm — they do not refer to, and are not associated with, any real person, case, or investigation.
