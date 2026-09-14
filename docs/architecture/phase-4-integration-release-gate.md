# Phase 4 integration release gate

Status on 2026-09-14: **In progress**. The six Phase 4 module areas are
present on `nipun`, and their frozen boundaries remain:

```text
worker claim/lease -> manifest/chunk -> canonical observation batch
  -> PostgreSQL observations + provenance + checkpoint + graph outbox
  -> asynchronous, idempotent Neo4j projector
```

The coordinator stores only metadata, commitments, and provenance. Raw media,
transcript/message contents, object-store credentials, and worker credentials
do not enter graph properties or coordinator acknowledgements. Visual,
audio, social, and OCR-derived identifiers remain evidence-local,
unresolved/review-only propositions.

## Verified evidence

- One Alembic head exists: `f4a1c9e0d2b3`.
- `uv sync --all-groups`, `ruff format --check`, `ruff check`, `mypy app`,
  and `git diff --check` pass on the release-gate tree.
- The complete local test suite passes: **1693 passed, 61 conditionally
  skipped**, with the retained Python 3.12 `audioop` deprecation warning.
  The separately required component suites also pass: evidence lifecycle
  (181), media processing (347), communication processing (348), extracted
  text (8), graph (136), security (134), and integration (9 passed, 60
  conditionally skipped).
- The repaired lease-renewal HTTP route suite passes (`14 passed`): valid
  claim/worker identity renews the durable lease; missing, wrong, foreign,
  expired, and terminal claims fail safely; audit metadata excludes tokens and
  object URIs.
- Focused Phase 4 cross-module coverage passed (`115 passed`), including
  transport limits, partial media persistence/replay, visual and communication
  orchestration, OCR utility provenance, and temporal graph mapping.
- Visual, audio, and social workers retain the canonical observation boundary;
  the graph projector remains an outbox consumer, never a direct worker write.

## Gate blockers

The following mandatory Compose evidence remains outstanding:

1. The test-only compatibility fixture schedules AnyIO worker operations
   from a child task. It repairs the prior root-task deadlock: boot, upload,
   graph API, and heartbeat HTTP checks run again; the full suite now passes.
2. Docker Engine `29.4.2` is reachable, but two fresh dedicated API-image
   builds failed during Dockerfile `uv sync --locked --no-install-project
   --no-dev`: first fetching `neo4j==6.3.0`, then `lxml==6.1.3`. Both failures
   were DNS resolution errors for `files.pythonhosted.org`. The image therefore
   never built and the dedicated API service never started. The required
   clean-PostgreSQL migration, Compose API readiness/outage-recovery, and
   synthetic live E2E gates remain unverified.
3. An existing user-owned `tracex` infrastructure stack is healthy, but its
   host API reports `/healthz` 200 and a truthful `/readyz` 503 because its own
   configured dependencies are unavailable. It is not the dedicated gate
   project and was not altered or used to claim Compose success.

No Phase 4 or Phase 5 status may be marked complete until both blockers are
cleared and the full local suite plus the dedicated Compose gate pass.
