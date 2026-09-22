# ADR-024: Graph read APIs, audit event catalog, worker heartbeat registry, pagination

## Status

Accepted (Phase 7 Closure — WP-6).

## Context

Four gaps bundled into one work package: the rest of G7 (case graph
snapshot/path/analytics/motifs reads, plus missing pagination on a few
collection endpoints), G8 (no canonical catalog of audit `event_type`
values), and G16/G17 (`/readyz`'s own docstring stated "no worker-
heartbeat registry exists," and no `GET .../workers` fleet view existed
at all).

## Decisions

### Graph snapshot/path/analytics/motifs are scoped to `Entity`/`Event`, not a raw Neo4j dump

`schemas.py`'s own pre-existing module docstring states this module's
read views are "deliberately narrow: never ... any Neo4j implementation
detail (label names, node IDs)" — a real, load-bearing invariant. A
generic "dump every label and property" snapshot endpoint would violate
it. Instead, `GET /cases/{id}/graph` returns typed `GraphSnapshotEntity
View`/`GraphSnapshotEventView` records built from exactly the allow-listed
properties `projection.py`'s own `_entity_properties`/`_event_properties`
already write — the same safety boundary the rest of this module already
enforces, just applied to a new read shape. Other node kinds
(`Observation`, `EntityMention`, `SourceClaim`, `TemporalEvent`, `Case`)
are evidence-provenance detail already served by `GET .../graph/
observations`; duplicating them here would add no analyst value.

### Path search never interpolates the hop bound into the Cypher text

`queries.py`'s own docstring states "no ID, label, or other value is ever
interpolated into a query string" — an absolute rule. `GraphPathRequest.
max_hops` (validated `1..15`) cannot be passed as a Cypher parameter
inside a variable-length relationship pattern (`shortestPath((a)-[*..N]-
(b))` requires `N` to be a literal, a genuine Cypher limitation, not a
choice this codebase made). The fix: the query always searches up to a
**fixed, hardcoded** `_PATH_SEARCH_CEILING_HOPS = 15` (a literal in the
query text, matching the model's own upper bound, never derived from a
request), and `request.max_hops` is enforced by filtering the result
afterward — a real path Neo4j finds within 15 hops that exceeds the
caller's smaller requested bound is reported `found: false`, never
truncated or silently returned anyway.

### `GET /api/v1/admin/workers` narrows an existing "never a public API" invariant, doesn't remove it

`AccessControlRepository.list_worker_credentials`'s docstring previously
read "Trusted-operator inspection only — never exposed through a public
API." G16 explicitly asks for this data over HTTP. The resolution:
`require_system_admin`-gated, not public — the invariant is narrowed to
"never exposed without `system_role=admin`," not removed. `credential_
digest` is never included in the response shape at all (`WorkerLiveness
View` doesn't have the field, structurally, not just by convention).

### The heartbeat registry needs no separate heartbeat endpoint

`worker_credentials.last_seen_at` (new, nullable, migration `6f7a8b9c0d1e`)
is populated by `require_worker_principal` itself, on every successful
authentication — every worker-authenticated call already proves the
credential is alive, so a dedicated `POST .../heartbeat`-style call to
populate a *fleet-level* registry would be redundant (a *per-job* lease-
renewal heartbeat already exists and is a different, pre-existing
concept — see `internal_api.py`'s `POST /{job_id}/heartbeat`). The touch
is best-effort (`_touch_worker_last_seen_safely`, mirrors `record_audit_
event_safely`'s contract): a failure to record a heartbeat must never
turn a successful authentication into a denied one.

### `/readyz`'s `worker_process_liveness` is real when it can be, honest when it can't

Previously a hardcoded string constant, permanently `"not_observed"`.
Now computed from the heartbeat registry when Postgres itself is
reachable; still reports `"not_observed"` (with a `reason`) when it
genuinely cannot ask — Postgres being down, or the query itself failing —
never a fabricated "observed" status papering over an actual failure.

### An audit event catalog without changing `record_audit_event`'s signature

`AuditEventType` (`audit_catalog.py`) enumerates all 29 `event_type`
values currently used across the codebase, verified by grepping every
real call site — not reproduced from an unverified target ("21-name
event catalog" in the original prompt could not be reconciled; see
`known-limitations.md`). `record_audit_event`/`record_audit_event_safely`
keep their existing plain `str` signature, deliberately: changing it to
require the enum would touch ~29 call sites across multiple contributors'
modules, a large, invasive change this WP's scope and risk tolerance
didn't warrant. Instead, `tests/unit/access_control/test_audit_event_
catalog.py` is a static test that greps `app/` for every `event_type=
"..."` literal and fails on any drift in either direction (uncatalogued
literal, or dead catalog entry) — real enforcement with zero call-site
risk.

### Pagination: extended where safe, deliberately not touched where risky

`HypothesisRepository.list_hypotheses` and `CaseNoteRepository.list_notes`
gained `offset` (mirroring `queries.list_case_observations`'s existing
`limit`+`offset` convention, not a new opaque-cursor scheme). `GET
/cases/{id}/notes` also gained its first-ever `limit` (previously
unbounded). `GraphCorrelationIntegrationRepository.list_candidates`
(Phase 5, Gate-C-adjacent, shared by multiple routes including `review_
candidate`) was deliberately **not** touched — the same risk-avoidance
reasoning as ADR-021/ADR-022's treatment of Phase 5 projection code.

## Deferred / explicitly out of scope

- Cursor pagination for `list_candidates`/correlation/candidate-list
  endpoints (Phase 5, Gate-C-adjacent) — still `limit`-only.
- A live-integration test specifically exercising `list_hypotheses`'s new
  `offset` parameter against real Postgres — the repository change
  mirrors already-integration-tested `list_case_observations` logic
  exactly; covered by unit-level type-checking and the broader unit
  suite, not a dedicated live test.
- Reconciling the "21-name event catalog" figure against the original
  prompt document (unavailable when this catalog was built) — the
  catalog instead reflects the verified, current, real call-site count
  (29).

## Consequences

- `docs/architecture/audit-event-catalog.md` is now the single place to
  answer "what security-audit events can this system emit" — and cannot
  silently drift from the code (the static test fails first).
- An operator can now see graph structure, worker fleet health, and page
  through case notes/hypotheses without needing direct database access.
- `tests/unit/integrity/test_migration_head.py`'s hardcoded-head assertion
  has now needed updating in WP-5 and WP-6 both (once per new migration)
  — a real, if minor, maintenance cost worth flagging for whoever owns
  that test next; not redesigned here, since that would be scope creep
  beyond gap closure.
