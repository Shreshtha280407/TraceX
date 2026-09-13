# Phase 3 final integration acceptance

Date: 2026-09-13. This closeout covers the merged Nipun, Aditya, Jasraj,
Sarthak, Gaurav, and Shreshtha Phase 3 work. It does not add entity
resolution, cross-case matching, scoring, hypotheses, real ASR/diarization,
cloud OCR/AI, new storage, or frontend work.

## Accepted flow

Document/FIR, CDR, finance, social/chat, image OCR, and selected video-frame
OCR producers use the claim-token-bound input path, SHA-256 verification, the
canonical observation-batch endpoint, durable transformation/progress records,
and one terminal `WorkerResultV1` with `observations=[]`. Accepted observations
create the existing PostgreSQL graph outbox only; workers never write Neo4j.
The projector maps case-scoped evidence and observations deterministically and
a second run finds no duplicate work.

Locators are validated at the shared boundary: documents retain page/span/bbox
context; CDR and finance retain row/column or JSON context and source-backed
time/amount data; chat retains message ID plus exact source location; image OCR
retains an original-image normalized bbox; video OCR additionally retains frame
number and valid frame time bounds. Transformation provenance references only
the submitting batch's observation IDs and its safe metadata excludes object
URIs, credentials, tokens, SQL, stack traces, and raw payloads.

## Media 422 regression

The reported third video-media batch returned 422 because two selected-frame
OCR batches had recorded `units_completed=1,2`, then the media aggregate batch
incorrectly emitted its own incomparable `1/1` progress event. The lifecycle
service correctly rejected that as a same-attempt progress regression. The
aggregate batch now has `progress=null`; frame OCR remains the meaningful,
monotonic progress stream. The regression test uses multiple selected frames
and asserts non-regressing progress, zero-based sequences, a final marker,
precise locators, and an empty terminal result. The genuine local-Tesseract
video live test passed after the fix.

## Commands and observed results

| Command/check | Result |
|---|---|
| `uv sync --all-groups` | passed |
| `uv run ruff format --check .` | passed (364 files) |
| `uv run ruff check .` | passed |
| `uv run mypy app` | passed (154 source files) |
| `uv run pytest -q` | **1656 passed, 1 skipped** in 68.80s |
| focused real-Tesseract video live test | passed (1 passed, 6 deselected) |
| `uv run alembic upgrade head` | passed; applied `d3f1a6c9b8e2` retry-limit migration |
| `/healthz`, `/readyz`, `/api/v1/meta/contracts` | all returned success from the host API against Compose PostgreSQL, Neo4j, Redis, and MinIO |

The focused live test verifies claim, stream, SHA check, real local OCR,
batch submission, empty terminal result, durable outbox rows, first projection,
and a second zero-work projection pass. Its cleanup removes only its own
case-scoped PostgreSQL/Neo4j data. The full suite also includes the real image
OCR live path and document/structured/social/graph lifecycle coverage.

## Compose status and limitation

`docker compose config` validates. PostgreSQL, Neo4j, Redis, and MinIO were
healthy. `docker compose up --build -d` could not rebuild the API because
Docker Desktop could not reach `registry-1.docker.io` (network unreachable);
this is an environment/network limitation, not a test skip or host-only
substitution. The documented host API was therefore used only for live
verification against those four Compose services. A fresh five-service Compose
build remains required before declaring a deployment image accepted.

## Remaining deliberate limits

OCR quality is bounded extraction quality, not evidentiary truth; small,
rotated, blurred, low-resolution, or unsupported-language text may be missed.
There is no real ASR/diarization model, entity merge, relationship scoring,
hypothesis engine, or graph rebuild CLI. These are intentionally later scope.
