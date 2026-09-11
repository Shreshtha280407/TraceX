# Neo4j Graph Foundation

How `app/modules/graph/` turns the frozen `app/contracts/` payloads into the Neo4j graph defined by `docs/architecture/graph-taxonomy-v1.md`. Read that document first for the node/relationship vocabulary; this document covers the module structure, the property-storage policy, and how to run it locally. **Phase 2.5** (`docs/architecture/graph-projection.md`) added the durable PostgreSQL-backed projection queue, the `--once` projector CLI, and the case-scoped read API that finally *call* the projection functions below from a real pipeline — this document's Phase 1 content (module structure, property policy, schema CLI) is otherwise unchanged.

## Module map

```text
app/modules/graph/
├── __init__.py
├── errors.py             # GraphError, GraphConnectionError, GraphValidationError, GraphNotFoundError
├── models.py             # GraphNodeKind, GraphRelationshipKind, AssertionKind, projection/query/job result models
├── schema.py             # idempotent constraint/index statements + apply/verify + CLI entry point
├── repository.py         # the only module that touches the `neo4j` driver
├── projection.py         # EvidenceRecordV1/ObservationV1/EntityV1/EventV1 -> graph, idempotent
├── queries.py            # safe, case-scoped, bounded reads
├── outbox_repository.py  # Phase 2.5 -- the only module that touches PostgreSQL (durable job queue)
├── projector.py          # Phase 2.5 -- claim/project/mark-outcome orchestration
├── worker.py             # Phase 2.5 -- `uv run python -m app.modules.graph.worker --once`
├── dependencies.py       # Phase 2.5 -- FastAPI DI for the read endpoint
├── schemas.py            # Phase 2.5 -- API response shapes
└── api.py                # Phase 2.5 -- GET /api/v1/cases/{case_id}/graph/observations
```

Dependency direction is strictly one-way: `projection.py` and `queries.py` depend on `repository.py` and `models.py`; `repository.py` depends on nothing else in the package. Nothing in `app/contracts/`, `app/core/`, `app/api/`, or `app/dependencies/` was changed to build this — the graph module is additive only. `outbox_repository.py` is the one Phase 2.5 exception to "graph never touches PostgreSQL": it imports `app.modules.evidence_lifecycle.repository`'s table objects directly (a sanctioned one-directional dependency — see `docs/architecture/graph-projection.md`), since this module is a backend-owned internal service, not a DB-blind extractor worker.

## Raw evidence vs. observation vs. entity vs. event, in the graph

- **`Evidence`** node: the ingested artifact's metadata (where the bytes live, their hash, their processing state) -- never the bytes themselves. `object_uri` is the pointer into MinIO; Phase 1 does not implement the actual MinIO write path (see `docs/qa/known-limitations.md`), but the graph is already shaped to hold the reference once it exists.
- **`Observation`** node: one provenance-rich fact extracted from an `Evidence` item, always traceable back to it via `YIELDED_OBSERVATION` and carrying its own `source_locator` (flattened onto the node -- see below). An observation is evidence-derived, not an evidence item itself.
- **`Entity`** node: a resolved thing (person, phone number, account, ...) that one or more observations were resolved into. Phase 1 does not implement entity resolution -- `project_entity` projects whatever `EntityV1` it is handed; nothing in this module merges, dedupes, or infers entities.
- **`Event`** node: a time-bounded occurrence connecting one or more entities via `HAS_PARTICIPANT`. There is no other way for two entities to be connected in this graph (see `graph-taxonomy-v1.md`, "Why no direct entity-to-entity edges").

## Why raw evidence payloads are not stored in Neo4j

Neo4j node properties are limited to primitives and homogeneous arrays of primitives -- there is no way to store a whole document, transcript, or video frame as a "proper" graph value, and doing so through a workaround (e.g. a giant string property) would turn the graph database into a second, worse copy of what MinIO already does well. More importantly: evidence integrity in TraceX depends on the bytes living in exactly one place (MinIO, addressed by `object_uri`/`sha256`), so hashing/chain-of-custody work in later phases has one source of truth to check against, not two that could drift. The graph holds references and derived facts; MinIO holds bytes.

## Property-storage policy: allow-listed, flattened, never `attributes`

Every node type has an **exhaustive** allow-list of properties, enforced by `EVIDENCE_ALLOWED_PROPERTIES` / `OBSERVATION_ALLOWED_PROPERTIES` / `ENTITY_ALLOWED_PROPERTIES` / `EVENT_ALLOWED_PROPERTIES` in `projection.py` and checked directly in `tests/unit/graph/test_projection.py`. Two rules shape every allow-list:

1. **`ObservationV1.attributes`, `EntityV1.attributes`, `EventV1.attributes` are never projected.** These are open-ended `dict[str, JsonValue]` bags that could hold arbitrarily large or sensitive extractor-specific payloads (this is exactly the "raw `attributes` payload" `CLAUDE.md` calls out). Anything a later phase needs from `attributes` for graph purposes should become its own explicitly-allow-listed field, reviewed on its own merits -- not inherited wholesale.
2. **Nested contract values are flattened to scalar properties**, because Neo4j node properties cannot hold nested maps. `ObservationV1.source_locator` (a `SourceLocator` object) becomes `source_locator_page`, `source_locator_span_start`, ..., `source_locator_bbox_x_min`, etc. `ObservationV1.location` / `EventV1.location` become `location_raw_text`, `location_latitude`, etc. `TimeWindow` becomes `time_window_start`/`time_window_end`. Optional sub-fields that are `None` are simply omitted from the property map (Neo4j has no concept of a property explicitly set to null).

One field breaks the "no open-ended data" rule deliberately: **`EntityV1.stable_identifiers`** (phone/PAN/account numbers, keyed per entity type) is serialized through `app.core.canonical.canonical_bytes` into a single deterministic JSON string (`stable_identifiers_json`), because it is exactly the identifier data a future entity-resolution/correlation phase needs to read back, and it is structured identifier data rather than free-text/extractor internals.

`created_from_observation_ids` (on `Entity`), `participant_entity_ids`/`evidence_refs` (on `Event`) are stored as plain string-UUID array properties, not as graph relationships -- the taxonomy does not define an `Entity->Observation` or `Event->Evidence` edge type in this phase (see `graph-taxonomy-v1.md`), and a property array carries no risk of pointing a relationship at a node that doesn't exist.

## Case isolation

Every `MERGE`/`MATCH` in `projection.py` and `queries.py` keys on the compound `(case_id, <domain>_id)` pair, never the domain ID alone, and `schema.py` backs this with a database-level composite uniqueness constraint per label (see `docs/decisions/ADR-001-graph-projection-and-case-isolation.md` for the full reasoning and the Community Edition constraint-type constraint that shaped it). `tests/integration/graph/test_case_isolation.py` projects the same `entity_id` into two different cases and confirms Neo4j holds two distinct nodes, and that a case-scoped query for one case cannot retrieve the other's data.

## Dependency ordering and deferred projection

`project_observation` requires its `evidence_id` to already be a projected `Evidence` node in the same case; `project_event` requires every `participant_entity_ids` entry to already be a projected `Entity` node in the same case. Both checks happen atomically inside the same Cypher statement that would otherwise write the node (a leading `MATCH`/`WHERE` that short-circuits every later clause to zero rows when the dependency is missing) -- there is no separate "check then write" race window. When a dependency is missing, nothing is written (not even a partial node) and the caller gets back a typed `DEFERRED` result naming exactly what's missing (`missing_evidence_id` / `missing_participant_entity_ids`), so an orchestrator can simply re-call the projection function once the dependency exists. `project_evidence` and `project_entity` have no such dependency and always apply.

## Running the schema management CLI

```bash
uv run python -m app.modules.graph.schema apply
uv run python -m app.modules.graph.schema verify
```

Both require Neo4j to be reachable (`docker compose up -d neo4j` at minimum) and read connection details from the same `Settings`/`.env` as the rest of the app. `apply` is safe to run any number of times (every statement uses `IF NOT EXISTS`); it is **not** run automatically by `app/main.py` on FastAPI startup. `verify` exits `0` when every expected constraint/index is present, `1` otherwise, and prints exactly what's missing.

## How integration tests behave without Neo4j

`tests/integration/graph/conftest.py` mirrors `tests/integration/test_readiness_live.py` exactly: the whole package skips (not fails) if there's no `.env` at the repo root, and the `repository` fixture skips (not fails) if Neo4j specifically isn't reachable through it -- e.g. `.env` exists but `docker compose up -d neo4j` hasn't been run. Every test that runs live also tears down what it created (`DETACH DELETE` scoped to its own generated `case_id`), so repeated runs never accumulate leftover graph data.

## What later Shreshtha phases add on top

Not built here (see `docs/qa/known-limitations.md` for the full list): candidate identity links and their human-review workflow, cross-modal correlation, candidate scoring, the hypothesis engine (and any `SUPPORTS` assertion kind beyond `"fact"`), and graph analytics.
