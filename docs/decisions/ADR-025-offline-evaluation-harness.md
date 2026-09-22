# ADR-025: Offline entity-resolution evaluation harness

## Status

Accepted (Phase 7 Closure — WP-7B).

## Context

`graph/intelligence/evaluation.py` (Phase 5) shipped as a permanent stub:
`deferred_evaluation_report` always returned every metric as `None` and
`deferred=True`, with no consumer anywhere in the codebase. Gap register
G1 asks for a real offline evaluator, explicitly **code only** — the
truth data itself is authored in the sibling `TraceX-Synthetic-Data`
repository, out of this codebase's scope.

## Decisions

### Scope: entity-resolution candidate quality only

The evaluator scores exactly what `graph/intelligence`'s existing
retrieval/candidate-generation cascade (unchanged, reused as-is from
WP-2/Phase 5) produces: precision, recall, false-link-rate, false-merge-
rate, and precision/recall@k for `entity_resolution_candidates` against a
human-authored truth spec of entity pairs. `temporal_boundary_
correctness` is **always `None`** — no event/temporal truth schema was
designed in this WP; reporting a fabricated number for a metric with no
real backing data would be worse than reporting none. A future WP that
wants that metric needs its own truth schema for event time windows,
which is a separate, larger design decision this WP declined to make
unilaterally.

### `TRACEX_SYNTHETIC_DATA_ROOT`, not a `Settings` field

Mirrors `structured_processing.benchmark_validation`'s established
`TRACEX_BENCHMARK_DATA_ROOT` pattern exactly: an explicit environment
variable, read directly via `os.environ.get`, never baked into the main
app's `Settings` (which would make it a required/validated part of the
live API's configuration — wrong, since this is an offline-only,
CLI-invoked capability). Unset means "skip cleanly" (`resolve_synthetic_
data_root` returns `None`, no exception); set-but-missing-the-specific-
case's-file means "real configuration error" (`TruthLoadError` — the
operator configured evaluation and it's broken, which is worth failing
loudly on, unlike the common "nothing configured yet" case).

### The import boundary is a grep-based test, not just a docstring

`truth_loader.py`'s docstring states offline evaluation must never be
reachable from `app/api/*.py`, any `*_api.py` route file, or `app/
main.py`. `test_no_api_route_ever_imports_the_evaluator_or_truth_loader`
enforces this by statically scanning every such file's imports — the
same "a rule stated only in a docstring is not a guarantee" reasoning
this codebase already applies elsewhere (e.g. the append-only database
triggers backing "no route can mutate an integrity record," not just a
comment saying so). `intelligence_worker.py --evaluate` is the one
sanctioned entry point — a CLI file, not a route file, so it's exempt
from (and doesn't need to be exempt from — it simply isn't matched by)
the boundary check.

### The `max_hops` (path search) constraint from WP-6 does not apply here — no Cypher

Unlike WP-6's graph-path endpoint, this evaluator does no Neo4j reads at
all: entity-resolution candidates and their review decisions live in
PostgreSQL (`entity_resolution_candidates`/`entity_review_decisions`,
via the existing `EntityRepository`), so no query-injection-shaped
constraint applies here.

## Truth data schema (for the `TraceX-Synthetic-Data` repository)

This is **not implemented in this codebase** — documented here so the
sibling repository's authors know exactly what `load_truth_spec` expects.
See `docs/qa/known-limitations.md`'s "WP-7B" section for the pointer the
final gap-closure report also carries.

At `<TRACEX_SYNTHETIC_DATA_ROOT>/<case_id>/entity_resolution_truth.json`:

```json
{
  "schema_version": "entity_resolution_truth.v1",
  "case_id": "<the case's UUID, must match the directory name>",
  "entity_pairs": [
    {
      "left_entity_id": "<UUID>",
      "right_entity_id": "<UUID>",
      "label": "same"
    },
    {
      "left_entity_id": "<UUID>",
      "right_entity_id": "<UUID>",
      "label": "different"
    }
  ]
}
```

- `entity_id` values must be real `Entity.entity_id`s that exist for this
  case after running the ingestion pipeline against the synthetic
  evidence for that case (i.e. authored against the actual deterministic
  `entity_id` the system will derive, not an arbitrary placeholder).
- `label` is exactly `"same"` or `"different"` — no partial-confidence
  labels; this v1 schema is a binary ground truth per pair.
- A pair the system never generates a candidate for at all is
  automatically scored as an implicit "different" prediction — the truth
  file does not need (and should not attempt) to enumerate every possible
  non-pair; only pairs worth asserting a truth label for need an entry.
- `schema_version` must be exactly `"entity_resolution_truth.v1"`; any
  other value is a hard `TruthLoadError`, not a best-effort parse.

## Deferred / explicitly out of scope

- Authoring any real truth data (WP-7A, the sibling repository's job).
- `temporal_boundary_correctness` (no schema designed).
- A live-infra integration test (the evaluator only touches PostgreSQL
  via `EntityRepository`, already covered by that repository's existing
  live tests for correctness; this WP's new logic is fully covered by
  fast, deterministic unit tests using a fake repository).
- Batch/multi-case evaluation runs, report aggregation across cases, or
  a comparison-against-previous-run mode — `--evaluate` scores exactly
  one case per invocation, matching `--generate`'s existing single-case
  scope.

## Consequences

- `intelligence_worker.py --evaluate --case-id <uuid>` is now a real,
  runnable capability once truth data exists, printing a JSON report to
  stdout for a caller (script, CI job) to consume.
- The frozen Gate C `RULES_CONFIG_HASH` (`graph/intelligence/scoring.py`)
  is threaded through every report, so a future comparison across rule
  changes can tell whether two reports are even comparable.
