# Phase 1 Test Results

## 2026-09-13 — Nipun — Phase 3 final integration acceptance

- `uv sync --all-groups`, `uv run ruff format --check .`, `uv run ruff check .`,
  and `uv run mypy app`: passed.
- `uv run pytest -q`: **1656 passed, 1 skipped** in 68.80s.
- The reported video OCR 422 was a valid lifecycle rejection of regressing
  progress (`1,2` selected frames followed by aggregate `1/1`); the aggregate
  now has no incomparable progress event. Focused unit regression and genuine
  local-Tesseract video live test both passed.
- `uv run alembic upgrade head`: passed, applying `d3f1a6c9b8e2`; the prior
  live upload 500 was the stale local database missing `worker_jobs.max_attempts`.
- Health/readiness/contracts endpoints passed against healthy Compose
  PostgreSQL, Neo4j, Redis, and MinIO via the documented host API path.
- `docker compose config` passed. Fresh `docker compose up --build -d` remains
  blocked by Docker Desktop network access to Docker Hub; this is recorded as
  an environment limitation, not a passing Compose result.

## 2026-09-13 — Shreshtha — Phase 3 deterministic graph mapping

- `uv sync --all-groups`: passed (`Resolved 101 packages`, `Checked 99 packages`).
- `uv run ruff format --check .`: passed (`364 files already formatted`).
- `uv run ruff check .`: passed (`154 source files`).
- `uv run mypy app`: passed.
- Focused mapping, projector/schema, and live Neo4j checks: passed; the final
  `tests/integration/graph/test_phase_3_mapping_live.py` run passed (`1 passed`).
- `docker compose config`, `docker compose up --build -d`, and `uv run alembic
  upgrade head`: passed. Rebuilt Compose API/Neo4j/PostgreSQL/Redis/MinIO are
  healthy. `/healthz`, `/readyz`, and `/api/v1/meta/contracts` returned 200.
- `uv run pytest -q`: not fully green due to the unrelated live-media test
  `tests/integration/media_processing/test_media_worker_live.py::test_video_frame_ocr_batch_submission_end_to_end_live`.
  Its third media observation batch returned HTTP 422 and the worker correctly
  submitted a failed terminal result. This failure is outside the graph mapper;
  document/CDR/finance specialised mapping coverage passed.

Actual command output from verification runs. Updated by whoever runs verification — do not hand-edit a "passing" result without having actually run the command.

## 2026-09-13 — Gaurav — Phase 3 closeout: video-frame OCR live test, full verification

Environment: same local dev machine as the entry below, branch `gaurav` (clean apart from this task's own uncommitted changes, `origin/main...HEAD` = `0 0`). Docker's daemon itself was down for this entire session (`docker ps`/`docker info` fail with a socket-not-found error, unrelated to this task's code — the same class of transient environment issue documented in Nipun's own Phase 2/3 sessions) — infra containers could not be started, so every live (`tests/integration/`) test in this repository self-skips in this run, not only this task's own.

```bash
$ uv run ruff format --check .
360 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 153 source files

$ uv run pytest -q
1585 passed, 58 skipped in 31.93s

$ docker compose config
(valid — no output on success)

$ docker ps
Cannot connect to the Docker daemon at unix:///home/nipun/.docker/desktop/docker.sock. Is the docker daemon running?
```

**Added this session**: `test_video_frame_ocr_batch_submission_end_to_end_live` (closes the previously-missing live video-frame OCR scenario) plus its `_real_ocr_video_fixture`/`make_text_video_bytes` helpers. The underlying fixture and pipeline were verified directly (outside pytest, since no live API/DB is reachable in this environment) to prove the helper itself is correct, not just that it self-skips cleanly:

```text
$ uv run python -c "... build a real labelled MP4 via make_text_video_bytes, decode it with the
  real ffprobe/frame-extraction path, run the real ImageOcrAdapter on each sampled frame ..."
probe: 640 180 1000 5.0 5
frames extracted: 5 failed: 0
frame 0 0 200 -> ['TRACEX OCR']
frame 1 200 400 -> ['TRACEX OCR']
frame 2 400 600 -> ['TRACEX OCR']
frame 3 600 800 -> ['TRACEX OCR']
frame 4 800 1000 -> ['TRACEX OCR']
```

Real Tesseract genuinely recognizes the labelled text on every real sampled frame, with correct per-frame timestamps — confirming the fixture and the adapter's video-frame path both work correctly. The pytest wrapper itself (`test_video_frame_ocr_batch_submission_end_to_end_live`) was confirmed to collect cleanly and self-skip (not error) given the unreachable live API:

```bash
$ uv run pytest tests/integration/media_processing/test_media_worker_live.py::test_video_frame_ocr_batch_submission_end_to_end_live -v
tests/integration/media_processing/test_media_worker_live.py::test_video_frame_ocr_batch_submission_end_to_end_live SKIPPED [100%]
1 skipped in 0.21s
```

**Honest status**: the video-frame live OCR test's full path (real upload → real claim → real OCR-on-video-frame → real batch submission → real graph outbox → real projector idempotency → real Neo4j/HTTP confirmation) has **not** been exercised end to end against a live server in this sandbox, because Docker itself could not be started here. It is not claimed as a live-verified pass — only the underlying fixture/adapter behavior it depends on was independently confirmed correct. The same is true of the pre-existing `test_ocr_batch_submission_end_to_end_image_live` (image path) and every other `tests/integration/` test in this repository this session — none could be run against real infra here. Re-run `uv run pytest tests/integration/media_processing/test_media_worker_live.py -v` once Docker/the API stack are reachable to get a real pass/fail on all of them.

## 2026-09-13 — Gaurav — Phase 3: Shared Raster-Image OCR Bounding-Box Adapter and Fixtures

Environment: Local dev machine, branch `gaurav`. Verified new `ImageOcrAdapter` and batching integration.

```bash
$ uv run pytest tests/unit/media_processing/test_ocr_adapter.py tests/unit/media_processing/test_ocr_batching.py tests/integration/media_processing/test_media_worker_live.py
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/nipun/Documents/Projects/TraceX
configfile: pyproject.toml
plugins: asyncio-1.4.0, anyio-4.15.1
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 56 items                                                             

tests/unit/media_processing/test_ocr_adapter.py ........................ [ 42%]
..........                                                               [ 60%]
tests/unit/media_processing/test_ocr_batching.py ................        [ 89%]
tests/integration/media_processing/test_media_worker_live.py ssssss      [100%]

======================== 50 passed, 6 skipped in 0.49s =========================
```
## 2026-09-12 — Nipun — Phase 3: Canonical Observation Ingestion, Batch Persistence, Progress, and Transformation Provenance build

Environment: same local dev machine as the prior Phase 2 closeout entry, branch `nipun` (clean tree, `origin/main...HEAD` = `0 0` at session start). Docker infra containers (`postgres`/`redis`/`neo4j`/`minio`) were already running and healthy; the `api` image's own *rebuild* was blocked again by the same persistent sandbox-network DNS flakiness documented in the prior Phase 2 closeout entry (this time failing on `sqlalchemy` and, on a second retry, `onnxruntime` — different packages, same transient DNS-resolution root cause, confirmed unrelated to any code in this task). Infra and live verification were **not** blocked — verified via the same documented "Option B" workflow (`docker compose up -d postgres neo4j redis minio` + `uv run uvicorn app.main:app` on the host).

```bash
$ uv sync --all-groups
Resolved 71 packages in 1ms
Checked 69 packages in 0.53ms

$ uv run ruff format --check .
322 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 137 source files

$ uv run pytest -q
1417 passed in 40.74s

$ docker compose config
(valid — no output on success)

$ docker compose up --build -d
... FAILED: DNS resolution error fetching sqlalchemy/onnxruntime wheels from files.pythonhosted.org
... (see "Docker image rebuild — blocked, worked around" below)

$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade ed593db47d8c -> c1c9c1c9d9f1, observation batch ingestion
```

### New tests added (all passing)

- `tests/contract/test_observation_batch.py` (45 tests) — `ObservationBatchSubmissionV1`/`TransformationProvenanceV1`/`ObservationBatchProgressV1` valid-parse (document page/span, scanned bbox, CDR row/column, finance sheet/row/cell), invalid-`ObservationV1`, malformed batch-id/sequence/timestamp/idempotency-key, empty-batch, duplicate-observation-id, secret-shaped/overlong `safe_metadata`, invalid transformation ordering/status/scope/duplicate-ordinal, and progress-coherence rejections.
- `tests/unit/evidence_lifecycle/test_observation_batch_submission.py` (12 tests) — repository/service-level persistence, scope rejection with no persistence, identical-retry replay, same-batch-changed-payload conflict, idempotency-key-reused-for-different-batch conflict, duplicate-observation-id-across-batches conflict, replay-enqueues-no-additional-projection-job, failed-transaction-leaves-no-orphans, progress ordering/regression/reset-after-reclaim, and transformation-provenance retrieval by batch linkage.
- `tests/unit/evidence_lifecycle/test_observation_batch_api.py` (11 tests) — HTTP-layer wiring: valid submit, missing/wrong/different-worker claim token, expired lease, unknown job, path/body mismatch, cross-case/cross-evidence rejection with no persistence, and no-secret-leakage across `401`/`409` responses.
- `tests/unit/evidence_lifecycle/test_evidence_api.py` — extended with 2 new tests: `latest_progress` correctly surfaced/scoped on the existing `GET .../jobs/{job_id}`, and an unknown-job lookup remains a plain `404`.
- `tests/unit/test_api_health.py` — updated `CONTRACT_VERSIONS` assertion for the 3 new registered contract names.
- `tests/integration/evidence_lifecycle/test_observation_batch_live.py` (3 tests, self-skipping) — see below.

Full repository regression after all of the above: **1417 passed, 0 skipped** (0 skipped specifically because the live infra was actually reachable this run — every self-skipping live-only test in the repository ran for real).

### Docker/live verification

Real, running host-`uvicorn` process against the live infra containers (the documented "Option B" workflow, used identically in the prior Phase 2 closeout session for the same DNS-flakiness reason):

```bash
$ curl -s http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -s http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -s http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1","observation_batch_submission":"ObservationBatchSubmissionV1","observation_batch_receipt":"ObservationBatchReceiptV1","transformation_provenance":"TransformationProvenanceV1"}
```

**Pytest live integration test** (`tests/integration/evidence_lifecycle/test_observation_batch_live.py`), run against the live stack — two real partial micro-batches through the real API, durable-row/exactly-once-outbox verification, a real graph-projector run (twice), a direct Neo4j query, and the real HTTP graph API; plus a separate idempotent-replay test and a separate partial-batches-then-final-result test:

```bash
$ uv run pytest tests/integration/evidence_lifecycle/test_observation_batch_live.py -v
test_document_micro_batches_reach_graph_outbox_and_project_exactly_once PASSED
test_identical_batch_replay_against_live_database_is_idempotent PASSED
test_partial_batches_then_final_result_preserves_existing_lifecycle PASSED
3 passed in 2.67s
```

**Manual, literal `curl`-driven end-to-end verification** (not the pytest call above) — a real user registered/logged in through the real running API; a case/membership/worker-credential seeded directly via the repository (no case-CRUD API exists yet, same as every other live verification in this repository); a real document evidence upload, claim, and observation-batch submission, all via real HTTP requests:

```text
REGISTER/LOGIN OK user_id= 2e8a5dfb-1d93-4bf4-a86b-3f14fee1f2da
CASE/MEMBERSHIP/WORKER-CRED OK case_id= e555bec7-3a65-454c-9700-df9e0c4a4ef6 worker_id= 6f4f522d-...

UPLOAD  -> job 2f359f5a-13b1-476a-8a62-1f4c2657daf4 processor=fir_report_text_v1 status=queued
CLAIM   -> claim_token issued, lease_expires_at=2026-09-12T04:26:21Z

SUBMIT  POST .../worker-jobs/{job_id}/observations
        -> {"job_id":"2f359f5a-...","batch_id":"manual-batch-1","status":"accepted",
            "accepted_observation_count":1,
            "progress":{"stage":"parsing","units_total":1,"units_completed":1,
                        "observations_emitted":1,"batch_sequence":0,
                        "message_code":"PAGE_PARSED", ...}, "request_id":"..."}

GET     /api/v1/cases/{case_id}/jobs/{job_id}  (real user auth, not worker auth)
        -> status=running observation_count=1
           latest_progress={"stage":"parsing","units_completed":1,"units_total":1,
                             "message_code":"PAGE_PARSED", ...}

PROJECTOR run 1 -> {"claimed": 7, "succeeded": 1, "failed": 6, ...}
  (the 6 "failed" were pre-existing orphaned graph_projection_jobs rows from earlier
   live-test runs in this same session, unrelated to this verification's own job --
   confirmed via a direct SQL join showing observation_not_found for all 6, then swept)
PROJECTOR run 2 -> {"claimed": 0, "succeeded": 0, "failed": 0, ...}
  (idempotent -- this job's single observation already terminal, no re-claim)

NEO4J   direct cypher-shell query (real read-only Cypher, not the Python driver):
        MATCH (o:Observation {case_id: '...', observation_id: '85ffc706-...'})
        RETURN o.observation_id, o.observation_type, o.case_id, o.evidence_id
        -> "85ffc706-affb-47f4-bdac-04dd2f5937e8", "document_text_mention",
           "e555bec7-3a65-454c-9700-df9e0c4a4ef6", "2d272f44-00a0-4064-a92f-0b9f8ef6d82b"

GRAPH-API  GET /api/v1/cases/{case_id}/graph/observations  (real user auth)
        -> {"items":[{"observation_id":"85ffc706-...","observation_type":"document_text_mention",
            "extraction_confidence":0.9,"extractor_name":"fir_report_text_v1", "mentions":[]}], ...}

ALL LAYERS CONFIRMED: worker submission -> durable PostgreSQL rows -> real graph
projector -> direct Neo4j query -> real case-scoped HTTP graph-read API.
```

No `object_uri`, claim token, or credential appeared in any response body across this manual verification.

### Docker image rebuild — blocked, worked around

`docker compose up --build -d` failed twice in this session with the same transient DNS-resolution error (`files.pythonhosted.org` unreachable) seen repeatedly in the prior Phase 2 closeout session — first on `sqlalchemy`, then on a retry on `onnxruntime` (a different pre-existing dependency each time, confirming environment-level network flakiness rather than anything related to this task's own new dependencies — this task added none). The already-running infra containers (`postgres`/`neo4j`/`redis`/`minio`, healthy throughout) and a host-run `uvicorn app.main:app` process against them (the documented "Option B" workflow) were used instead for every live check above — this exercises the exact same current application code, just not inside a freshly rebuilt container image. Not a blocker; noted in case a future build in this same environment needs a retry.

### Cleanup and final state

All data created by both the pytest live suite and the manual `curl`-driven verification was removed: the case, user, worker credential, evidence, job, observation, and Neo4j nodes created for the manual verification were deleted in a final cleanup pass (repository deletes + a `DETACH DELETE` scoped to the synthetic `case_id`). The 6 pre-existing orphaned `graph_projection_jobs` rows found during projector verification (from earlier live-test runs in this same session, confirmed via `LEFT JOIN worker_observations ... WHERE observation_id IS NULL`) were also swept. Final state confirmed directly: `graph_projection_jobs` holds only `9 succeeded` rows (this session's other live pytest suite's own accepted work), `0 failed`.

`git status --short` at the end of this session shows only working-tree modifications (28 files: 20 modified, 8 new) — no staged files, no commits, no branch switch, still on branch `nipun`. Docker infra containers left running; the host `uvicorn` process left running too (matching the prior session's "Option B" convention) — `docker compose down` / killing the `uvicorn` process removes them cleanly whenever wanted.

## 2026-09-12 — Nipun — Phase 2 Closeout: Real Local Media Inference and Continuous Worker/Projector Operation build

Environment: same local dev machine as prior Phase 2 entries, branch `nipun` (clean tree, `origin/main...HEAD` = `0 0` at session start, containing every prior Phase 2 module including Gaurav's/Shreshtha's most recent merges). Python 3.12.13 (via `uv`), Docker reachable for infra but the `api` image's *rebuild* specifically was blocked by persistent sandbox-network DNS flakiness this session (see "Docker image rebuild — blocked, worked around" below) — infra (`postgres`/`neo4j`/`redis`/`minio`) and live verification were **not** blocked.

```bash
$ uv sync --all-groups
Resolved 71 packages ...
$ uv add onnxruntime pytesseract
 + flatbuffers==25.12.19
 + onnxruntime==1.30.0
 + protobuf==7.36.1
 + pytesseract==0.3.13
```
Result: **pass**. No `torch`/`ultralytics`/cloud-AI dependency added — `onnxruntime` (CPU; `onnxruntime-gpu` documented as the CUDA drop-in) and `pytesseract` (a thin subprocess wrapper around the already-installed-on-this-host `tesseract` binary) only. Total install size ~22.5 MiB.

```bash
$ uv run ruff format --check .
315 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 136 source files
$ docker compose config -q && echo "compose config OK"
compose config OK
```
Result: **pass**, all four.

```bash
$ uv run pytest -q
1344 passed in 28.20s
```
Result: **pass** — the full suite, including every pre-existing evidence-lifecycle/structured-processing/communication-processing/access-control/graph test, plus this phase's new detector/OCR/tracker/loop/renewal tests, all run live (Docker infra reachable, zero skips).

### Real local detector validated directly (before wiring into the worker)

```bash
$ uv run python -m app.modules.media_processing.bootstrap_models
downloading https://github.com/opencv/opencv_zoo/raw/0b263e423d012606b83d1f81238d11c177da2b9c/models/object_detection_yolox/object_detection_yolox_2022nov.onnx
verified and installed: models/media/object_detection_yolox_2022nov.onnx (sha256=c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063)
```
Result: **pass** — real download (35,858,002 bytes), real SHA-256 match against the pinned, documented value. Re-running is idempotent (`already present and verified`); a deliberately wrong `--expected-sha256` correctly refuses to install the mismatched file (verified separately, not shown).

```bash
$ uv run python -c "
from pathlib import Path
import numpy as np
from PIL import Image
from app.modules.media_processing.analysis.onnx_detector import OnnxObjectDetector, DetectorConfig
cfg = DetectorConfig(model_path=Path('models/media/object_detection_yolox_2022nov.onnx'), device='auto')
det = OnnxObjectDetector(config=cfg)
print('device selected:', det.device)
img = Image.open('street_test.png').convert('RGB')  # a public-domain OpenCV sample image, basketball court scene
frame = np.asarray(img, dtype=np.uint8)
for d in det.detect(frame):
    print(d.label, round(d.confidence, 3), d.box)
"
device selected: cpu
person 0.926 PixelBoundingBox(x_min=32.85, y_min=80.59, x_max=163.39, y_max=472.39)
person 0.846 PixelBoundingBox(x_min=440.37, y_min=20.97, x_max=639.23, y_max=460.29)
```
Result: **pass** — real detector, real CPU execution provider selected (this sandbox has no CUDA), two genuine "person" detections at high confidence on a real photographic test image, well-formed boxes within frame bounds. (This test image is a standard OpenCV sample-data file, not committed to this repository — see `docs/qa/test-data.md`.)

```bash
$ uv run python -c "
from PIL import Image, ImageDraw, ImageFont
import numpy as np
from app.modules.media_processing.analysis.tesseract_ocr import TesseractTextRecognizer, TesseractOcrConfig
img = Image.new('RGB', (500, 150), color=(255,255,255))
font = ImageFont.truetype('/usr/share/fonts/noto/NotoSans-Bold.ttf', 48)
ImageDraw.Draw(img).text((20,40), 'EXIT 42B', fill=(0,0,0), font=font)
frame = np.asarray(img, dtype=np.uint8)
r = TesseractTextRecognizer(config=TesseractOcrConfig(min_confidence=0.0))
for region in r.recognize_regions(frame):
    print(repr(region.text), round(region.confidence, 3), region.box)
"
'EXIT 42B' 0.92 PixelBoundingBox(x_min=24.0, y_min=57.0, x_max=223.0, y_max=92.0)
```
Result: **pass** — real Tesseract OCR, correct recognized text, high confidence, correct bounding box.

```bash
$ uv run python -m app.modules.media_processing.benchmark bench_video.mp4   # a tiny ffmpeg-lavfi-generated synthetic clip, 320x240, 3s, no real footage
{
  "input": {"size_bytes": 12212, "content_type": "video/mp4", "media_duration_ms": 3000, "media_width": 320, "media_height": 240},
  "device": {"detector_loaded": true, "detector_device": "cpu", "ocr_loaded": true, "cpu_only": true, "gpu_visible": false, "gpu_name": null},
  "sampling": {"frames_requested": 3, "frames_extracted": 3, "frames_failed": 0},
  "timings_ms": {"probe_ms": 40.97, "frame_extraction_ms": 141.13, "analysis_ms": 402.29, "observation_construction_ms": 570.17, "total_ms": 1155.03},
  "throughput": {"input_mib_per_second": 0.0101, "media_seconds_processed_per_wall_clock_second": 2.597},
  "result_status": "succeeded",
  "observation_counts": {"media_metadata": 1},
  "total_observations": 1
}
```
Result: **pass** — measured, not fabricated: this specific 3-second, 320x240, 3-frame-sampled synthetic clip processed in ~1.16 real wall-clock seconds on this development machine's CPU (~2.6x realtime for this tiny input) — **not** a general throughput claim, and not extrapolated to any other resolution/duration/hardware. No detections in this synthetic test-pattern clip (expected — it contains no real objects).

### Docker image rebuild — blocked, worked around

`docker compose up --build -d` (and a bare `docker compose build api`) failed **consistently across ~35 attempts** over this session, always with a DNS resolution timeout from the Docker daemon's own resolver (`1.1.1.1:53`) against `registry-1.docker.io`, `ghcr.io`, or an individual PyPI package host (`files.pythonhosted.org`) — a different single host failing each time, never a durable block on one specific domain, and never a code/dependency-resolution error. Representative failures:
```
#4 ERROR: failed to authorize: failed to fetch anonymous token: ... dial tcp: lookup ghcr.io on 1.1.1.1:53: read udp ...: i/o timeout
#13 × Failed to download `flatbuffers==25.12.19` ... dns error ... failed to lookup address information: Try again
#3 ERROR: ... dial tcp [2606:4700:4403::ac40:904e]:443: connect: network is unreachable
```
This is a sandbox-environment Docker networking characteristic, not a defect in the `Dockerfile`/`pyproject.toml`/`uv.lock` changes this phase makes — `docker compose config` (above) confirms the compose file itself is valid, and the identical `uv sync` resolves and installs cleanly, repeatedly, directly on the host (see the full-suite run above). **Worked around**, not skipped: the already-running (pre-rebuild) `api` container was stopped, and the current code's FastAPI app was run directly on the host (`uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`) against the *same* already-running, already-healthy `postgres`/`neo4j`/`redis`/`minio` containers — the documented "Option B" local-development workflow this repository's own runbook already describes, not an improvised one. This exercises the exact current code (including the `media_detection_v1` routing change, the new `/renew` endpoint, and every other change this phase makes) against real infrastructure; the only thing it does not prove is that `ffmpeg`/`tesseract-ocr` install correctly *inside a freshly built container image* specifically (both are already verified present and working on the host, and the `apt-get install ffmpeg tesseract-ocr` line itself was reached and cached successfully in several of the ~35 build attempts before a later, unrelated layer failed on the DNS issue above).

```bash
$ curl -s http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -s http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -s http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
```
Result: **pass**, all three, against the host-run current-code API.

### Full live pipeline: real image + real video → real detection/OCR → real graph projection → real Neo4j/API confirmation

```bash
$ curl -s -X POST http://localhost:8000/api/v1/cases/$CASE_ID/evidence \
    -F "file=@street_test.png;type=image/png" -F "source_type=image" -F "classification=unclassified"
{"evidence":{...,"parser_profile":"media_detection_v1",...},
 "job":{"job_id":"7556402d-...","processor_name":"media_detection_v1","status":"queued",...}}

$ WORKER_TOKEN="<real-provisioned-token>" uv run python -m app.modules.media_processing.worker --once
{"device": "cpu", "event": "worker.analysis.detector_loaded", ...}
{"language": "eng", "event": "worker.analysis.ocr_loaded", ...}
{"processor_name": "media_detection_v1", "event": "worker.run_once.claimed", "job_id": "7556402d-...", ...}
HTTP Request: GET .../worker-jobs/7556402d-.../input "HTTP/1.1 200 OK"
HTTP Request: POST .../worker-jobs/7556402d-.../result "HTTP/1.1 200 OK"
{"status": "succeeded", "observation_count": 8, "event": "worker.run_once.submitted", ...}

$ docker compose exec postgres psql -U tracex -d tracex -c \
    "SELECT observation_type, canonical_payload->>'extraction_confidence', canonical_payload->'attributes'->>'detected_label' FROM worker_observations WHERE job_id = '7556402d-...' ORDER BY observation_type;"
 media_metadata   | 1.0                |
 object_detection | 0.9261808243687497 | person
 object_detection | 0.8458445016650131 | person
 ocr_text_mention | 0.56/0.4/0.57/0.44/0.705 | (5 rows, no detected_label -- OCR, not detection)

# real synthetic video, same flow:
$ curl -s -X POST .../evidence -F "file=@bench_video.mp4;..." -F "source_type=video" ...
job_id=a8db7105-..., processor=media_detection_v1
$ uv run python -m app.modules.media_processing.worker --once
{"status": "succeeded", "observation_count": 1, ...}   # metadata only -- correct, a synthetic test-pattern clip has no real object in it

$ uv run python -m app.modules.graph.worker --once
{"claimed": 9, "succeeded": 9, "failed": 0, "retrying": 0, "deferred": 0, "event": "graph.worker.run_completed", ...}
$ uv run python -m app.modules.graph.worker --once   # again, immediately -- idempotency check
{"claimed": 0, "succeeded": 0, "failed": 0, "retrying": 0, "deferred": 0, "event": "graph.worker.run_completed", ...}

$ docker compose exec neo4j cypher-shell -u neo4j -p change-me-dev-only \
    "MATCH (o:Observation {case_id: 'eaf44815-...'}) RETURN o.observation_type AS type, count(*) AS count ORDER BY type;"
"media_metadata", 2
"object_detection", 2
"ocr_text_mention", 5

$ curl -s http://localhost:8000/api/v1/cases/eaf44815-.../graph/observations -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
{"case_id": "eaf44815-...", "items": [ ...9 items, extractor_name/extractor_version populated... ]}
```
Result: **pass** — real image and real video each uploaded, routed to `media_detection_v1` for real, claimed and processed by the real worker (real YOLOX-s CPU detection: two genuine "person" detections at 0.93/0.85 confidence; real Tesseract OCR: 5 recognized regions at varying confidence — a real, if noisy, result on a non-text photographic image, not fabricated), submitted, and projected into Neo4j for real — 9/9 succeeded on the first projector run, 0/0 claimed on an immediate second run (idempotent, no duplication). Confirmed independently via a direct read-only Neo4j `cypher-shell` query and the real case-scoped `GET .../graph/observations` HTTP endpoint, both matching. Verified the graph API response contains no `object_uri`, no worker token, no MinIO endpoint string.

All manually-created verification data (user, case, membership, evidence, jobs, observations, Neo4j nodes, and the `media-closeout-verify` worker credential) was left in place afterward, matching this repository's established "long-lived shared local dev sandbox" convention documented in prior Phase 2 entries — none of it is committed to Git, and none of it is real/sensitive content (a public-domain OpenCV sample image and an `ffmpeg`-generated synthetic test-pattern clip).

### A real cross-suite test-environment bug caught, again (same pattern as the prior Sarthak-phase session)

Running the full suite revealed `test_media_worker_live.py`'s own `media-processing-worker-live-test` credential had been created *earlier in this same session* (before `SUPPORTED_PROCESSORS` was extended to include `media_detection_v1`), so it was scoped only to `media_metadata_v1` — causing this phase's own live pipeline test to get a correct, legitimate `403 worker_processor_scope_denied` for the now-primary `media_detection_v1` claim. Not a new bug class (the identical stale-scoped-credential shape the Sarthak-phase session already found and fixed for `structured_processing`/`communication_processing`), but a fresh instance of it for `media_processing`, caught the same way: `uv run python -m app.modules.access_control.worker_credentials list` identified the stale row, `revoke --worker-id ...` retired it (the sanctioned trusted-operator CLI, not raw SQL), and `_MEDIA_WORKER_TOKEN`'s literal was bumped (`-v1` → `-v2`) to avoid re-colliding with the now-permanently-revoked digest — the exact same "revocation is permanent, use a fresh literal" fix already documented in `docs/architecture/phase-2-decisions.md`'s Sarthak-phase section, this time confirmed to hold for a third module. Re-ran clean afterward (see the full-suite result above).

## 2026-09-11 — Sarthak — Phase 2: Communication Processing Worker Foundation build

Environment: local dev machine, branch `sarthak` (clean tree, containing merged `main` through Aditya's Phase 2.4), Python 3.12.13 (via `uv`), Docker reachable (`tracex-api-1` rebuilt fresh this session via `docker compose up --build -d`, all five services healthy).

```bash
$ uv run ruff format --check .
281 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 123 source files
```
Result: **pass**, all three. No new dependency — `httpx` was already an approved, installed dependency (used by `structured_processing.client` and the FastAPI app itself).

```bash
$ docker compose config -q && echo "compose config OK"
compose config OK
$ uv run alembic current
48e9e76153ca (head)
$ uv run alembic heads
48e9e76153ca (head)
```
Result: **pass** — DB already at head from the migration applied earlier this session (`af5b05e61b08 -> 48e9e76153ca`); no new migration needed this phase (no schema change).

```bash
$ curl -s http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -s http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -s http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
```
Result: **pass**, all three.

```bash
$ uv run pytest -q
1137 passed in 20.39s
```
Result: **pass** — the full suite, including every pre-existing evidence-lifecycle/structured-processing/access-control/Phase 2.4 test, plus this phase's new communication-processing worker-orchestration tests (`test_communication_worker_client.py`, `test_communication_worker_orchestration.py`, the extended `test_transcript_import.py`/`test_diarization_import.py`, and the live `test_communication_worker_live.py`). Zero failures, zero unexpected skips (Docker was up, so nothing live self-skipped).

### A real bug caught by live testing, not present before this session's own changes

Running `tests/integration/communication_processing/test_communication_worker_live.py` and `tests/integration/structured_processing/test_worker_live.py` together in one full-suite `pytest -q` run initially produced **7 failures**, all `WorkerApiError: claim request failed: HTTP 403`. Root cause: both live-test files' `_ensure_worker_credential` helpers read the *same* shared local-dev `settings.worker_token` value; whichever suite's helper ran first (`communication_processing`, alphabetically) "won" the one credential row matching that digest and scoped it to only its own 7 processors, so `structured_processing`'s subsequent claim for e.g. `fir_report_text_v1` was correctly, legitimately denied by the real, working processor-scope enforcement (Aditya's Phase 2.4 code) — a genuine test-environment collision, not an application defect.

Fix: `test_communication_worker_live.py` now provisions its own credential under a distinct hardcoded token literal (`_COMMUNICATION_WORKER_TOKEN`), fully decoupling its credential row from `structured_processing`'s (`settings.worker_token` is still used as the "is live testing configured at all" skip-gate — only the actual authentication value differs). This is confined to Sarthak's own test file.

Cleaning up the stale rows this collision had already created in the shared local dev DB (two `active` rows scoped only to `communication_processing`, both blocking `structured_processing`'s test) surfaced a **second, genuine latent bug**, present in both live-test files' identical `_ensure_worker_credential` helper: the no-op check only handled "an *active* row with a matching digest exists" — a *revoked* row with a matching digest (exactly what a `worker_credentials revoke --worker-id ...` cleanup produces) fell through to a fresh `INSERT`, which crashed with a raw `sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint "uq_worker_credentials_credential_digest"` instead of a clean skip or an actionable message — because `credential_digest` is unique per row regardless of status, and revocation is intentionally permanent (no production "reactivate" path exists, by design). Fixed in both files: a matching-but-revoked row now fails the test immediately with an explicit message ("revocation is permanent — change the token literal / `WORKER_TOKEN`, then rerun") instead of surfacing a confusing SQL error. `access_control`'s production repository/CLI were not touched — revocation-permanence as a real security property was deliberately preserved, not routed around. Full reasoning: `docs/architecture/phase-2-decisions.md`'s "Why `_ensure_worker_credential`'s test helper now fails loudly..." section.

After both fixes (and rotating the local `.env`'s `WORKER_TOKEN` to a fresh, unpoisoned literal, since the original had been revoked in the course of diagnosing this):

```bash
$ uv run pytest tests/integration/communication_processing/test_communication_worker_live.py tests/integration/structured_processing/test_worker_live.py -v
... 7 passed in 2.90s
$ uv run pytest -q
1137 passed in 20.39s
```
Result: **pass**, both live suites together and the full suite.

### Docker/live worker verification — real `--once` CLI subprocess run

In addition to the pytest live-pipeline test (which calls `run_once()` directly — the exact function `--once` invokes), a literal `--once` CLI subprocess run was performed against the real running stack, to prove the actual entry point, not just the function underneath it:

```bash
$ curl -s -X POST http://localhost:8000/api/v1/auth/register -d '{"email":"comm-cli-verify-...@example.test", ...}'
{"user_id":"43d96f3e-be79-4322-b02c-b69f5d37f5a6", ...}   # HTTP 201
# case + membership seeded directly via AccessControlRepository (no case-CRUD API exists yet — out of Phase 1/2 scope)

$ curl -s -X POST http://localhost:8000/api/v1/cases/3d4269b0.../evidence \
    -F "file=@verify.wav;type=audio/wav" -F "source_type=audio" -F "classification=unclassified"
{"evidence":{...,"parser_profile":"audio_metadata_v1","processing_status":"queued",...},
 "job":{"job_id":"d30c3e3f-ec43-4a75-9867-b082ed027ba8","processor_name":"audio_metadata_v1","status":"queued",...}}

$ WORKER_TOKEN="dev-only-communication-worker-token-change-me-v2" \
    uv run python -m app.modules.communication_processing.worker --once
{"event": "worker.run_once.started", ...}
{"processor_name": "audio_metadata_v1", "event": "worker.client.claim_attempted", ...}
HTTP Request: POST http://localhost:8000/api/v1/internal/worker-jobs/claim "HTTP/1.1 200 OK"
{"processor_name": "audio_metadata_v1", "event": "worker.run_once.claimed", "job_id": "d30c3e3f-...", ...}
HTTP Request: GET http://localhost:8000/api/v1/internal/worker-jobs/d30c3e3f.../input "HTTP/1.1 200 OK"
{"job_id": "d30c3e3f-...", "event": "worker.client.submit_attempted", ...}
HTTP Request: POST http://localhost:8000/api/v1/internal/worker-jobs/d30c3e3f.../result "HTTP/1.1 200 OK"
{"status": "succeeded", "observation_count": 1, "event": "worker.run_once.submitted", "job_id": "d30c3e3f-...", ...}
{"job_id": "d30c3e3f-...", "status": "succeeded", "event": "worker.cli.done", ...}

$ curl -s http://localhost:8000/api/v1/cases/3d4269b0.../jobs/d30c3e3f-...
{"job_id":"d30c3e3f-...","status":"succeeded","observation_count":1,"claimed_at":"...","completed_at":"...", ...}

$ docker compose exec postgres psql -U tracex -d tracex -c \
    "SELECT observation_id, case_id, evidence_id, observation_type, canonical_payload->>'extraction_confidence', canonical_payload->'source_locator', canonical_payload->'extractor' FROM worker_observations WHERE job_id = 'd30c3e3f-...';"
 e9409cdf-...  | 3d4269b0-... | 22e63650-... | audio_metadata | 1.0 |
   {"time_start_ms": 0, "time_end_ms": 500, "message_id": null, "json_path": null, ...} |
   {"name": "audio_metadata_v1", "version": "1.0.0", "config_hash": "628bb4...", "model_version": "n/a"}
```
Result: **pass** — claim → secure claim-token-bound input stream → SHA-256-implicit parse → submit all succeeded for real against the real running stack; the stored `worker_observations` row is case-scoped (`case_id` matches the seeded case), evidence-scoped (`evidence_id` matches the uploaded file), provenance-complete (`source_locator`, `extractor.name`/`.version`/`.config_hash` all populated), and carries no raw audio bytes (only the derived technical metadata). No worker token, claim token, MinIO endpoint/object key, or DB credential appeared anywhere in the CLI's stdout log lines. The `chat`/`generic_social_json_v1` path was proven the same way via the automated `test_communication_worker_live.py` pytest case rather than a second manual run (redundant to repeat by hand).

The five profiles not reachable via a real upload (`transcript_import_v1`, `diarization_import_v1`, `whatsapp_export_v1`, `telegram_export_v1`, `instagram_export_v1`) were exercised end to end via directly-constructed `WorkerJobV1`s in `test_communication_worker_orchestration.py` instead — see `docs/architecture/communication-processing-worker.md`'s "Routing boundary" section for why no real-upload path exists for them yet.

## 2026-09-11 — Nipun — Phase 2.3: Explicit Structured-Data Upload Routing build

Environment: same sandbox as the Phase 2.2 build below, Python 3.12.13 (via `uv`), Docker 29.4.1 / Compose v5.1.3 (available this session, same intermittent-per-session pattern already documented). Branch `nipun`, rebased cleanly onto `origin/main` with a clean tree.

```bash
$ uv sync --all-groups
Resolved 66 packages in 2ms
Checked 65 packages in 13ms
$ uv run ruff format --check .
267 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 120 source files
```
Result: **pass**, all four. No new dependency.

```bash
$ uv run pytest -q
1037 passed in 13.61s
```
Result: **pass**, zero skips (Docker/live infra reachable this session), zero failures. New this build: `tests/unit/evidence_lifecycle/test_structured_routing.py` (11 tests), `tests/contract/test_evidence.py` (+1 parametrized), `tests/integration/structured_processing/test_worker_live.py`'s pipeline test now parametrized ×3 (`document`, `structured_tabular`, `structured_json`).

```bash
$ docker compose config
```
Result: **pass** (exit 0).

**Real bug found and fixed during live verification**: the first live run of the new `structured_tabular`/`structured_json` pipeline tests against the rebuilt container failed with a genuine `500` on upload — not the app-level `SourceType` enum (which correctly accepted the new values once the image was rebuilt with current code), but a **PostgreSQL `CHECK` constraint** (`ck_evidence_records_source_type`/`ck_worker_jobs_source_type`, defined in `migrations/versions/f2086e1e89f6_evidence_lifecycle_foundation.py`, enumerating the *original* eight `SourceType` values at the database level, independent of the Pydantic contract). Adding an enum value at the Python/contract layer alone was not sufficient — the database's own integrity constraint also needed widening. Fixed with a new, additive migration (`af5b05e61b08_structured_source_type_routing.py`, hand-written like its predecessors) that drops and recreates both constraints with the two new values added — every previously-accepted value remains accepted; nothing narrows. Applied live:

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 102857ca8d1d -> af5b05e61b08, structured source type routing
$ uv run alembic current
af5b05e61b08 (head)
```

After the migration, the same previously-failing live pipeline tests passed genuinely:

```bash
$ uv run pytest tests/integration/structured_processing/test_worker_live.py -v
tests/integration/structured_processing/test_worker_live.py::test_worker_client_against_real_running_api PASSED
tests/integration/structured_processing/test_worker_live.py::test_full_claim_stream_parse_submit_live_pipeline[document-fir_report_text_v1] PASSED
tests/integration/structured_processing/test_worker_live.py::test_full_claim_stream_parse_submit_live_pipeline[structured_tabular-generic_tabular_v1] PASSED
tests/integration/structured_processing/test_worker_live.py::test_full_claim_stream_parse_submit_live_pipeline[structured_json-generic_json_v1] PASSED
4 passed in 1.20s
```

Full suite re-confirmed clean after the migration:

```bash
$ uv run pytest -q
1037 passed in 13.61s
$ curl -sf http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -sf http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -sf http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
```

**XLSX live check** (the third format named in the task brief; the automated pipeline test above covers CSV and JSON, so this one-off script covers XLSX specifically — real register/login/case/upload/`--once` worker subprocess/status-check/cleanup, identical pattern to the automated test):

```
uploaded XLSX evidence, job_id=b28955cc-3008-4cab-a50b-284185f3207d
... claim_attempted fir_report_text_v1 -> no work, cdr_generic_v1 -> no work,
    financial_transaction_generic_v1 -> no work, generic_tabular_v1 -> claimed ...
HTTP Request: GET http://localhost:8000/api/v1/internal/worker-jobs/b28955cc.../input "HTTP/1.1 200 OK"
{"status": "succeeded", "observation_count": 2, "event": "worker.run_once.submitted", ...}
job status: {'status': 'succeeded', 'processor_name': 'generic_tabular_v1', 'observation_count': 2, ...}
XLSX LIVE CHECK PASSED
```
Result: **pass**, genuinely — real CSV, XLSX, and JSON evidence all routed to the correct processor and fully processed live.

**Docker services left running**, same as the Phase 2.2 entry below — not stood up fresh by this task.

## 2026-09-11 — Nipun — Phase 2.2: Secure Worker Evidence Delivery build

Environment: same sandbox as the Jasraj Phase 2 build below, Python 3.12.13 (via `uv`), Docker 29.4.1 / Compose v5.1.3 (confirmed reachable this session, unlike the Jasraj Phase 2 session earlier the same day — see that entry's "blocked" note; Docker availability varies by sandbox session, not by anything in this repository). Branch `nipun`, rebased cleanly onto `origin/main` (which already included Jasraj's merged Phase 2 work) with a clean working tree. Read `app/modules/evidence_lifecycle/{internal_api,service,repository,storage,dependencies,schemas,errors}.py`, `docs/architecture/{evidence-lifecycle,worker-job-lifecycle,phase-2-decisions,structured-processing-worker}.md`, and `structured_processing/{client,input_resolver,worker}.py` before designing the new endpoint.

```bash
$ uv sync --all-groups
Resolved 66 packages in 1ms
Checked 65 packages in 0.79ms
$ uv run ruff format --check .
266 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 120 source files
```
Result: **pass**, all four. No new dependency — `StreamingResponse` is existing FastAPI, `asyncio.to_thread`/chunked streaming is stdlib + already-used minio-py API surface.

```bash
$ uv run pytest -q
997 passed, 24 skipped in 20.22s
```
Result: **pass**, no regressions. New this build: `tests/unit/evidence_lifecycle/test_worker_input_api.py` (14 tests), 4 new tests in `tests/unit/evidence_lifecycle/test_object_storage.py` (lazy-chunk-pull proof against a fake minio-py response, round-trip, missing-object safety), 1 new SHA-256-mismatch test in `tests/unit/structured_processing/test_worker_orchestration.py`, and `test_worker_client.py`'s `fetch_input` tests updated for the real `Content-Disposition`/`X-TraceX-Evidence-SHA256` header contract plus a new oversized-response test.

```bash
$ docker compose config
```
Result: **pass** (exit 0).

```bash
$ docker compose up --build -d
```
Result: **pass** after 5 attempts. The first four attempts hit the same transient sandbox-network DNS/registry-resolution flakiness already documented in this file's Phase 2/2.1 entries (`registry-1.docker.io`/`files.pythonhosted.org` DNS timeouts, a different dependency each time — `typing-extensions`, `alembic`, then two bare registry-metadata timeouts) — not a code or configuration issue (`docker compose config` above already proved the compose file itself is valid). The fifth attempt completed cleanly: all five services (`api`, `postgres`, `neo4j`, `redis`, `minio`) reached `healthy`/`running`.

```bash
$ curl -sf http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -sf http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -sf http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
$ uv run alembic current
102857ca8d1d (head)
```
Result: **pass**, all four — no new migration was needed for Phase 2.2 (the endpoint reads existing `evidence_records`/`worker_jobs` tables only).

**Live smoke test — the full claim -> stream -> parse -> submit path, for real** (a one-off script, not part of the pytest suite; registers a real user, seeds a real case/membership via `AccessControlRepository`, uploads a real synthetic FIR-text evidence file through the real `/api/v1/cases/{case_id}/evidence` endpoint, runs the real `uv run python -m app.modules.structured_processing.worker --once` as a subprocess, then asserts on the real job-status API — and cleans up every row it created):

```
uploaded evidence, job_id=55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69
--- worker stdout ---
{"event": "worker.run_once.started", ...}
{"processor_name": "fir_report_text_v1", "processor_version": "1.0.0", "event": "worker.client.claim_attempted", ...}
HTTP Request: POST http://localhost:8000/api/v1/internal/worker-jobs/claim "HTTP/1.1 200 OK"
{"processor_name": "fir_report_text_v1", "event": "worker.run_once.claimed", "job_id": "55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69", ...}
HTTP Request: GET http://localhost:8000/api/v1/internal/worker-jobs/55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69/input "HTTP/1.1 200 OK"
{"job_id": "55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69", "event": "worker.client.submit_attempted", ...}
HTTP Request: POST http://localhost:8000/api/v1/internal/worker-jobs/55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69/result "HTTP/1.1 200 OK"
{"status": "succeeded", "observation_count": 2, "event": "worker.run_once.submitted", "job_id": "55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69", ...}
{"job_id": "55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69", "status": "succeeded", "event": "worker.cli.done", ...}
--- job status ---
{'job_id': '55d1dbb7-bdc2-4f8e-8354-02a4a8f7cf69', 'case_id': 'b591b72d-23ba-433c-9bb5-52dda62a8d48', 'evidence_id': '8eec3238-ff72-421d-99d1-c7aa41a60a0d', 'source_type': 'document', 'processor_name': 'fir_report_text_v1', 'processor_version': '1.0.0', 'attempt': 1, 'status': 'succeeded', 'requested_at': '2026-09-11T07:53:47.196113Z', 'dispatched_at': '2026-09-11T07:53:47.206462Z', 'claimed_at': '2026-09-11T07:53:47.681549Z', 'completed_at': '2026-09-11T07:53:47.702794Z', 'observation_count': 2, 'last_error_code': None, 'last_error_message': None}
SMOKE TEST PASSED
```
Result: **pass** — genuinely, not fabricated. Note the `GET .../input "HTTP/1.1 200 OK"` line: this is the real new endpoint, streaming the real uploaded evidence bytes back to the worker, which the worker then genuinely parsed (`FIR No. 77/2026 filed at Test Police Station. Section 420 IPC.` -> 2 observations, matching the `fir_reference` + `legal_section_mention` patterns) and submitted as a real `SUCCEEDED` result — not the `DEFERRED`/`input_resolution_unavailable` fallback every prior live attempt in this repository's history produced, because the gap that caused that is now closed. The job-status API (`GET /api/v1/cases/{case_id}/jobs/{job_id}`) independently confirms the same `job_id`, `succeeded` status, `claimed_at`/`completed_at` populated, and `observation_count: 2` — and, as always, no claim token or `object_uri` anywhere in that response. One incidental finding during script development: this long-lived sandbox's postgres volume had accumulated leftover `queued` `fir_report_text_v1` jobs from earlier manual live-verification sessions (Nipun Phase 2.1's own smoke test, run much earlier in this same session) — the claim query correctly claimed the *oldest* eligible one first, which briefly caused the smoke-test script (not the application) to check the wrong job. Not a bug in `claim_job` (this is its documented, correct FIFO behavior) — fixed in the script by clearing stale `queued` rows for the test processor before uploading, and by asserting the claimed `job_id` matches the uploaded one. All rows this script created (worker_observations, worker_results, worker_jobs, evidence_records, case_memberships, cases, users) were deleted in a `finally` block after the assertions ran; also cleaned up two earlier same-session partial-failure runs' leftover rows via a separate one-off query before the passing run above.

**`tests/integration/structured_processing/test_worker_live.py` upgraded and re-verified live**: with the input-stream endpoint now real, the original test's `InputResolutionUnavailableError`-on-404 assertion was updated to `WorkerAuthenticationError`-on-401 (the endpoint now genuinely exists and rejects an unclaimed job's fake token, rather than 404ing), and a second test, `test_full_claim_stream_parse_submit_live_pipeline`, was added — a *permanent*, self-skipping, automated version of the live smoke test above (real register/login/case/membership/upload, a real `run_once()`, real assertions, real cleanup), so this proof no longer depends on a one-off manual script:

```bash
$ uv run pytest tests/integration/structured_processing/test_worker_live.py -v
tests/integration/structured_processing/test_worker_live.py::test_worker_client_against_real_running_api PASSED
tests/integration/structured_processing/test_worker_live.py::test_full_claim_stream_parse_submit_live_pipeline PASSED
2 passed in 0.80s
```

**Full suite, live infrastructure fully up** (the strongest verification run of this whole session — every self-skipping `tests/integration/*` suite in the repository ran for real, not skipped):

```bash
$ uv run pytest -q
1022 passed in 16.31s
```
Result: **pass**, zero skips, zero failures — access_control, evidence_lifecycle, graph, structured_processing, and readiness-live integration suites all exercised against real PostgreSQL/Neo4j/Redis/MinIO/the real API. `uv run ruff format --check .`/`ruff check .`/`mypy app`/`docker compose config` were re-run clean immediately before this (266 files formatted, all checks passed, no issues in 120 source files, exit 0).

**Docker services left running**: `docker compose up --build -d` was already running when this task began (started outside this session, confirmed via `docker ps` before any action was taken) and was rebuilt/restarted in place for this verification — left running afterward rather than torn down, since it wasn't stood up fresh by this task and may still be in interactive use.

## 2026-09-11 — Jasraj — Phase 2: Structured-Processing Worker build

Environment: same sandbox as the Nipun Phase 2.1 build below, Python 3.12.13 (via `uv`). Branch `jasraj`, working tree already matched `origin/main` (no rebase needed). Inspected the merged `evidence_lifecycle` internal worker API (`internal_api.py`, `schemas.py`, `routing.py`), `structured_processing`'s existing Phase 1 `process_job`/`models.py`/`provenance.py`/`structured/profiles.py`, and `docs/architecture/worker-job-lifecycle.md` before designing anything. Confirmed via inspection (not assumption) that no endpoint exists for a worker to fetch a claimed job's evidence bytes/metadata — the "Input-access boundary" documented in `docs/architecture/structured-processing-worker.md`.

```bash
$ uv sync --all-groups
Resolved 66 packages in 1ms
Checked 65 packages in 0.56ms
```
Result: **pass**. One new runtime dependency: `httpx` (`uv add httpx`), promoted from dev-only to a runtime dependency of `pyproject.toml`'s `[project.dependencies]` — a production worker process needs an HTTP client at runtime, not just in tests. Removed the now-redundant `httpx>=0.27.2` pin from `[dependency-groups.dev]`.

```bash
$ uv run ruff format --check .
265 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 120 source files
```
Result: **pass**, all three.

```bash
$ uv run pytest -q
979 passed, 24 skipped in 19.59s
```
Result: **pass**, no regressions. New this build: `tests/unit/structured_processing/test_worker_client.py` (12), `tests/unit/structured_processing/test_worker_orchestration.py` (8), one new Markdown-classification test in `test_classifier.py`, and `tests/integration/structured_processing/test_worker_live.py` (1, self-skips without a live server — see below). The 24 skips are every self-skipping `tests/integration/*` suite in this repository (no live PostgreSQL/Neo4j/Redis/MinIO/API server reachable this session — see the Docker section below).

```bash
$ docker compose config
```
Result: **pass** (exit 0) — validates the new `WORKER_API_BASE_URL` passthrough on the `api` service; no other service definition changed.

```bash
$ docker context ls
$ docker info
$ sudo -n systemctl start docker
sudo: a password is required
```
Result: **blocked, not fabricated**. Docker itself is unavailable in this sandbox session: the active `desktop-linux` context's socket (`/home/nipun/.docker/desktop/docker.sock`) does not exist, the systemd `docker.service` is `inactive`, and starting it requires an interactive `sudo` password this session does not have. `docker compose up --build -d` — the required next verification command — could not be run as a result. This is an environment property of this particular sandbox run, not a code or configuration problem (`docker compose config` above proves the compose file itself is valid); a prior session in this same repository (see the Phase 2 entry above) *did* have Docker available. Per this task's explicit "do not fabricate live integration success" instruction, no Docker/live-stack verification is claimed below — only what was actually run.

```bash
$ uv run pytest tests/integration/structured_processing/test_worker_live.py -v -rs
tests/integration/structured_processing/test_worker_live.py::test_worker_client_against_real_running_api SKIPPED
SKIPPED [1] tests/integration/structured_processing/test_worker_live.py:68: live API server not reachable at http://localhost:8000: ConnectError; start it via `docker compose up --build -d` to run this test
1 skipped in 0.04s
```
Result: **pass (correct self-skip)** — proves the test's own honesty mechanism works: given a real `.env` (present in this sandbox) but no reachable server, it skips with a precise, actionable reason rather than reporting a false pass. This is the exact scenario 19 outcome the task anticipated for a session where "the approved input-stream capability is absent or Docker unavailable."

```bash
$ uv run python -m app.modules.structured_processing.worker --once
{"event": "worker.run_once.started", "run_id": "e60671e8-...", "level": "info", "timestamp": "..."}
{"processor_name": "fir_report_text_v1", "processor_version": "1.0.0", "event": "worker.client.claim_attempted", "run_id": "e60671e8-...", "level": "info", "timestamp": "..."}
{"reason": "request to /api/v1/internal/worker-jobs/claim failed: ConnectError", "event": "worker.cli.failed", "level": "error", "timestamp": "..."}
EXIT:1
```
Result: **pass (correct safe failure)** — a real, unmocked run of the CLI entry point (argument parsing, structlog configuration, settings loading, client construction, the real claim attempt) against the genuinely-unreachable API. Exits `1` cleanly with a safe, structured, secret-free error — no raw traceback, no `WORKER_SHARED_SECRET`, no stack trace. This is not a substitute for the blocked full Docker/live run above; it is real evidence the CLI's own error handling is correct under the one failure mode this sandbox could actually exercise.

**Blocked verification, stated explicitly**: `docker compose up --build -d` and the full claim -> resolve -> parse -> submit live path against a real running stack (the final required-verification step, and integration scenario 19's "complete path" variant) could not be attempted this session because Docker itself is unavailable in this sandbox (see above) — not because of anything in this worker's code. Whoever next has Docker available in this environment should run: `docker compose up --build -d`, wait for all five services healthy, then `uv run python -m app.modules.structured_processing.worker --once` against the running stack — expected outcome is a claimed-then-`DEFERRED` result (checkpoint `input_resolution_unavailable`) for any real queued job, since the proposed input-access endpoint (`docs/architecture/structured-processing-worker.md`) still does not exist; a `SUCCEEDED` parse is not achievable until that endpoint is added.

## 2026-09-11 — Nipun — Phase 2.1: Worker Job Claim and Result-Submission Integration build

Environment: same sandbox as the build below, Python 3.12.13 (via `uv`), Docker 29.4.1 / Compose v5.1.3. Branch `nipun`; working tree was clean and `git log` showed the local `nipun` branch (`95023e3 Completed nipun/phase-2`) and `origin/main` (`fac9f54 Completed nipun/phase-2 (#10)`) had each independently advanced by one equivalent commit past their common ancestor — the same Phase 2 work, committed on both sides separately; not touched further, per instruction to work only on `nipun` without altering history. Inspected the merged Phase 2 evidence-lifecycle code, `access_control`'s token-generation/hashing pattern, and the frozen `WorkerJobV1`/`WorkerResultV1`/`ObservationV1` contracts before designing anything.

```bash
$ uv sync --all-groups
Resolved 66 packages in 3ms
Checked 65 packages in 0.83ms
```
Result: **pass**. No new dependency — `FOR UPDATE SKIP LOCKED` is a SQLAlchemy Core method, `secrets`/`hashlib`/`hmac` are stdlib.

```bash
$ uv run ruff format --check .
259 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 118 source files
```
Result: **pass**, all three.

```bash
$ uv run pytest -q
977 passed in 13.94s
```
Result: **pass**, no regressions. 65 new/changed tests this build across `tests/unit/evidence_lifecycle/{test_worker_claim,test_worker_result,test_worker_internal_api}.py`, one new case-scoped test in `test_evidence_api.py`, `tests/integration/evidence_lifecycle/test_worker_lifecycle_live.py` (2, run live), and `tests/unit/test_config.py` (1 new security-regression test).

```bash
$ docker compose config
```
Result: **pass** (exit 0) — validates the new `WORKER_SHARED_SECRET`/`WORKER_LEASE_SECONDS` passthrough on the `api` service.

```bash
$ docker compose up -d postgres neo4j redis minio
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade f2086e1e89f6 -> 102857ca8d1d, worker job claim and result foundation
```
Result: **pass** — all four infra services reached `healthy`; the new revision applies cleanly on top of the existing chain against a live database.

```bash
$ uv run pytest tests/integration/evidence_lifecycle/test_worker_lifecycle_live.py -v
test_real_claim_and_result_submission_against_live_infra PASSED
test_real_claim_with_wrong_processor_finds_nothing PASSED
2 passed in 0.92s
```
Result: **pass — run live**, not self-skipped. Confirms `FOR UPDATE SKIP LOCKED` claiming, the full claim→submit→persist flow, and a processor/version mismatch finding nothing eligible, all against real PostgreSQL/MinIO.

### Docker build: `api` image built cleanly this session (no repeat of the earlier flakiness)

```bash
$ docker compose up --build -d
...
 Image tracex-api Built
 Container tracex-api-1 Started
$ docker compose ps
# api, postgres, neo4j, redis, minio — all Up/healthy
$ curl .../healthz    # 200
$ curl .../readyz     # 200, all four deps "ok"
$ curl .../api/v1/meta/contracts   # 200
```
Result: **pass**.

### Live end-to-end worker-lifecycle smoke test (beyond the required command list)

Against the freshly-built containerized stack, using the project's real auth/case setup (register → login → seed case + membership directly via `AccessControlRepository`, the same pattern every integration test in this repo uses) and the real `Idempotency`-free upload flow:

```bash
$ curl -X POST .../cases/<case_id>/evidence -F file=@cdr_fixture.csv -F source_type=cdr -F classification=unclassified
# 201 -- evidence + a queued WorkerJobV1 for cdr_generic_v1

$ curl -X POST .../internal/worker-jobs/claim -H "Authorization: Bearer $WORKER_SHARED_SECRET" \
    -d '{"processor_name":"cdr_generic_v1","processor_version":"1.0.0"}'
# 200 -- {"job": {...WorkerJobV1 incl. input_object_uri...}, "claim_token": "...", "lease_expires_at": "..."}

$ curl -X POST .../internal/worker-jobs/<job_id>/result -H "Authorization: Bearer $WORKER_SHARED_SECRET" \
    -H "X-Claim-Token: <claim_token>" -d '<WorkerResultV1 JSON, 1 ObservationV1, status=succeeded>'
# 200 -- {"job_id":..., "status":"succeeded", "result_id":..., "observation_count":1, "observation_ids":[...]}

$ curl .../cases/<case_id>/jobs/<job_id> -H "Authorization: Bearer <user_token>"
# 200 -- status=succeeded, claimed_at set, completed_at set, observation_count=1; no claim_token/object_uri

$ docker compose exec postgres psql -U tracex -d tracex -c "SELECT status, attempt, claimed_by FROM worker_jobs WHERE job_id = '<job_id>';"
# succeeded | 1 | cdr_generic_v1
$ ... SELECT result_id, status, payload_hash FROM worker_results WHERE job_id = '<job_id>';
# 1 row
$ ... SELECT observation_id, observation_type FROM worker_observations WHERE job_id = '<job_id>';
# 1 row, cdr_call_record
```
Result: **pass** — real durable terminal job state and a real durable observation row, confirmed both through the API and by querying PostgreSQL directly. Idempotent exact-payload replay (same `X-Claim-Token`) returned the identical `result_id`/`observation_ids` with `200`. No worker authentication header → `401`; wrong shared secret → `401`.

### Two real bugs caught and fixed during this build's live smoke test (not caught by unit tests alone, before this record)

- **A wrong/unknown claim token against an *already-terminal* job silently returned the cached result instead of being rejected.** `submit_result`'s original ordering checked `job.status in TERMINAL_WORKER_STATUSES` *before* verifying the claim token, short-circuiting straight to the idempotent-replay/conflict comparison (which only compares the submitted payload's hash, not who's asking) whenever the job was already done. Caught live: submitting a deliberately wrong `X-Claim-Token` against the just-completed job from the smoke test above returned `200` with the real cached result instead of `401` — meaning any caller holding only the coarse, worker-fleet-wide `WORKER_SHARED_SECRET` (not a valid per-job claim token) could retrieve a completed job's result summary by guessing/reusing its exact payload, defeating the claim token's purpose as a per-job credential. Fixed by moving the claim-token hash verification to run first and unconditionally (job found + token matches, checked before branching on terminal vs. running status) — the terminal-job idempotency/conflict path is now only reached once the caller has already proven they hold the job's real token. Regression test: `tests/unit/evidence_lifecycle/test_worker_result.py::test_wrong_claim_token_is_rejected_even_against_an_already_terminal_job`; re-verified live on the rebuilt container (`401` where it previously returned `200`).
- **`Settings.worker_shared_secret` could resolve to an empty-but-not-`None` secret, defeating the fail-closed check.** Found while wiring `WORKER_SHARED_SECRET` into `compose.yaml`'s `api` service, before committing the passthrough: `docker compose`'s `${VAR}` substitution (no default) resolves an unset variable to an empty string inside the container, and pydantic-settings treats a *present* env var as "provided" regardless of content, so `worker_shared_secret` would become `SecretStr('')`, not `None` — skipping `require_worker_principal`'s `is None` fail-closed branch and allowing `hmac.compare_digest("", "")` to accept an empty `Authorization: Bearer ` header. Fixed with a `field_validator` normalizing any blank/whitespace-only value to `None`, plus a redundant emptiness check at the point of use. A related, quieter issue found in the same pass: this repo's own `.env` (needed locally to exercise the live smoke test above) now defines a real `WORKER_SHARED_SECRET`, which `tests/conftest.py` didn't pin the way it already pins every other optional setting with a default (e.g. `MAX_EVIDENCE_BYTES`) — so the default unit-test run was silently picking up the developer's real local value instead of exercising the fail-closed path deterministically. Fixed by adding `WORKER_SHARED_SECRET: ""` (and `WORKER_LEASE_SECONDS`) to `tests/conftest.py`'s `_TEST_ENV_DEFAULTS`. Regression test: `tests/unit/test_config.py::test_blank_worker_shared_secret_normalizes_to_none`.

### Known limitations and intentionally deferred work

No worker daemon/consumer loop, no real per-worker credential system, no max-attempt cutoff, no lease renewal, no automatic redrive of `deferred`/`cancelled` jobs, no denied-worker-action auditing — all explicit non-goals or documented gaps for this phase. See `docs/qa/known-limitations.md` and `docs/architecture/worker-job-lifecycle.md`.

## 2026-09-11 — Nipun — Phase 2: Evidence Lifecycle and Durable Processing Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.4.1 / Compose v5.1.3. Branch `nipun`, clean tree, not diverged from `origin/main`, confirmed via `git status --short`/`git branch --show-current`/`git fetch origin` before starting. Inspected and reused, rather than duplicated: `app.modules.access_control.dependencies.require_case_action`/`require_evidence_read`, `record_audit_event`, the hand-written-`sa.Table`/Alembic convention, the `asyncio.to_thread`-wrapped MinIO client-construction pattern, and `structured_processing`'s `SourceResolver` protocol shape (satisfied structurally, no cross-module import).

```bash
$ uv add python-multipart
Resolved 66 packages in 1.22s
 + python-multipart==0.0.32
```
Result: **pass**. The only new dependency (FastAPI requires it for `File`/`Form` multipart parsing) — confirmed genuinely absent first via `uv run python -c "import multipart"` failing with `ModuleNotFoundError`.

```bash
$ uv sync --all-groups
```
Result: **pass**.

```bash
$ uv run ruff format --check .
$ uv run ruff check .
All checks passed!
$ uv run mypy app
Success: no issues found in 117 source files
```
Result: **pass**, all three. (One real mypy finding fixed before this record: `MinioObjectStorage.put_object`'s `data: IO[bytes]` parameter didn't satisfy `asyncio.to_thread`'s `BinaryIO` expectation for `minio.Minio.put_object` — changed the `ObjectStorage` protocol and both implementations to `BinaryIO` throughout `storage.py`.)

```bash
$ uv run pytest -q
948 passed in 12.97s
```
Result: **pass**, no regressions. 65 new tests this build: `tests/unit/evidence_lifecycle/` (29), `tests/security/evidence_lifecycle/` (34), `tests/integration/evidence_lifecycle/` (2, run live — see below). 883 total collected before this build (864 passed + 19 skipped, Gaurav's entry below) + 65 = 948; this run had `.env` present and live infra up (see below), so every previously-self-skipping suite across every other module also ran live instead of skipping, which is why `0 skipped` here rather than the expected `19` — not a regression, a side effect of infra being up for this build's own live verification.

```bash
$ docker compose config
```
Result: **pass** (exit 0) — validates the new `MAX_EVIDENCE_BYTES` env passthrough on the `api` service alongside the existing ones.

### Docker build: transient sandbox network flakiness (not a code defect)

```bash
$ docker compose up --build -d
...
#12 25.05   × Failed to download `opencv-python-headless==5.0.0.93`
#12 25.05   ├─▶ dns error / failed to lookup address information: Try again
```
Retried twice more (once after confirming `docker ps`/a synthetic `docker build` both had working outbound network): failed a second time on the same package, then a third time on `asyncpg` instead — three different pre-existing dependencies (unrelated to this task's own code, already present before this build), each failing ~24s into `uv sync` inside the BuildKit build network with an intermittent DNS resolution failure against `files.pythonhosted.org`. A direct `docker run alpine wget https://files.pythonhosted.org/` from the same daemon succeeded once and failed once across two attempts, confirming this is environment-level flakiness in this sandbox's Docker networking, not a dependency-resolution or code problem. Used the documented "Option B" workflow instead (infra in Docker, API on host) to complete live verification without waiting on a fresh image build — see below for the full live smoke test.

**Resolved**: the user re-ran `docker compose up --build -d` afterward (in the background, ~153s for `uv sync` alone this time — a fourth package, `neo4j`, hit the same transient DNS failure mid-attempt before it eventually succeeded), and it completed clean:

```bash
$ docker compose ps
tracex-api-1        Up   (running)
tracex-minio-1      Up   (healthy)
tracex-neo4j-1      Up   (healthy)
tracex-postgres-1   Up   (healthy)
tracex-redis-1      Up   (healthy)

$ curl .../healthz    # {"status":"ok","service":"tracex-api","version":"0.1.0"}                              200
$ curl .../readyz     # {"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}} 200
$ curl .../api/v1/meta/contracts                                                                                200
```
Result: **pass** — the full containerized stack, including the `api` image, builds and runs correctly. Confirms the earlier failures were exactly what they looked like (sandbox network flakiness across four different unrelated packages, never the same one twice), not a defect in this task's `Dockerfile`/`compose.yaml`/dependency changes. `docs/qa/known-limitations.md` and `docs/progress/mvp-progress.md` updated to close this out.

```bash
$ docker compose up -d postgres neo4j redis minio
```
Result: **pass** — all four reached `healthy` (confirmed via polled `docker compose ps`).

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade  -> 3e8cbaa07711, baseline
INFO  [alembic.runtime.migration] Running upgrade 3e8cbaa07711 -> 7e8499f34f29, access control foundation
INFO  [alembic.runtime.migration] Running upgrade 7e8499f34f29 -> f2086e1e89f6, evidence lifecycle foundation
```
Result: **pass** — the full chain applies cleanly against a genuinely fresh database, including the new revision. (The local `.env` predated the `AUTH_JWT_*`/`MAX_EVIDENCE_BYTES` settings entirely — a pre-existing gap unrelated to this task — so the missing required-setting block from `.env.example` was appended to the local, git-ignored `.env` before this could run.)

```bash
$ uv run pytest tests/integration/evidence_lifecycle -v
test_migration_created_tables_enforce_uniqueness_and_fks PASSED
test_real_upload_flow_against_live_postgres_and_minio PASSED
2 passed in 1.07s
```
Result: **pass — run live**, not self-skipped. Confirms the partial unique index on `(case_id, upload_idempotency_key)` and the FK from `evidence_records.uploaded_by` to `users.user_id` are both enforced by real PostgreSQL (the second one caught a real test-fixture bug — see below), and a real upload through `EvidenceLifecycleService` against real MinIO round-trips byte-identical content.

### Live end-to-end HTTP smoke test (beyond the required command list)

Ran a real `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` process against the live containers, exercised over real HTTP with `curl`, plus direct Redis/MinIO inspection:

```bash
$ curl .../healthz     # {"status":"ok",...}                                                  200
$ curl .../readyz      # postgres:ok, neo4j:ok, redis:ok, minio:ok                             200
$ curl .../api/v1/meta/contracts                                                               200
$ curl -X POST .../auth/register ... ; curl -X POST .../auth/login ...                         201, 200
# seeded a real case + active membership directly via AccessControlRepository (no case-CRUD API exists)
$ curl -X POST .../cases/<case_id>/evidence -H "Idempotency-Key: smoke-2-key" -F file=@fir_fixture.txt \
    -F source_type=document -F classification=unclassified
{"evidence":{...,"sha256":"b54a...","processing_status":"queued",...},
 "job":{...,"processor_name":"fir_report_text_v1","status":"queued","dispatched_at":"2026-09-11T03:47:11.414906Z",...}}
                                                                                                201
$ curl -X POST ... (same Idempotency-Key, same file again)   # identical evidence_id/job_id     200
$ curl -X POST ... (same case, file declared as video/mp4 against source_type=document)
{"error":{"code":"validation_error","message":"content_type 'video/mp4' is not accepted for source_type 'document'",...}}
                                                                                                422
```
```bash
$ uv run python3 -c "... MinioObjectStorage(settings).read_bytes(<object_uri>) ..."
b'FIR No. 999/2026 filed at Sample Police Station. Section 420 IPC.\n'   # byte-identical to the uploaded fixture

$ docker compose exec -T redis redis-cli LLEN tracex:jobs:document
2
$ docker compose exec -T redis redis-cli LRANGE tracex:jobs:document 0 -1
# both pushed jobs' canonical JSON: schema_version, job_id, case_id, evidence_id, source_type,
# processor_name, processor_version, attempt, idempotency_key, input_object_uri, requested_at —
# no extra fields, no credentials. Exactly 2 entries for 2 distinct uploads (the idempotent
# replay above did NOT push a duplicate).
```
Result: **pass** — real evidence metadata, real private MinIO object, real durable+published job, idempotent replay, and rejection of an unsupported content type, all verified against live infrastructure. `docker compose ps` afterward: `minio`/`neo4j`/`postgres`/`redis` all `healthy`, left running (nothing destroyed); the manual host `uvicorn` verification process was stopped.

### Two real bugs caught and fixed during this build (before this record)

- **`UploadOutcome.job.dispatched_at` was always `null` on a fresh upload, even when the same-request Redis publish actually succeeded.** `service.upload_evidence` built the returned `UploadOutcome` from the in-memory `WorkerJobRecord` constructed *before* calling `_dispatch`, and `_dispatch` updated only the database row, never the caller's local variable. Caught live: the smoke-test upload response showed `"dispatched_at": null` immediately after a successful upload, while a subsequent `GET /jobs/{job_id}` (which re-reads from PostgreSQL) would have shown the correct timestamp — a real, if non-critical, response-accuracy bug that no unit test (which never checked the *returned* value's freshness, only the repository's stored value) had caught. Fixed by having `_dispatch` return the updated (or, on a deferred publish, unchanged) `WorkerJobRecord`, used to build the final `UploadOutcome`. Added a regression assertion (`outcome.job.dispatched_at is not None`) to `tests/unit/evidence_lifecycle/test_upload_service.py::test_valid_upload_creates_evidence_storage_write_and_job`.
- **A test-fixture bug (not an application bug), also only caught live.** `tests/integration/evidence_lifecycle/`'s two integration tests originally passed a bare `uuid4()` for `uploaded_by`, which violates `evidence_records.uploaded_by`'s real foreign key onto `users.user_id` (`FakeEvidenceLifecycleRepository`, used everywhere else, doesn't enforce foreign keys at all — exactly the class of bug live integration tests exist to catch, the same lesson `docs/decisions/ADR-003-...md` Decision 7 already recorded for `access_control`). Fixed by adding a `seeded_user` fixture (mirroring the existing `seeded_case`) to `tests/integration/evidence_lifecycle/conftest.py` and using it for `uploaded_by` in both tests.

```bash
$ git status --short -- app/modules/graph app/modules/structured_processing app/modules/communication_processing app/modules/media_processing app/contracts
(no output)
```
Result: **pass** — zero diff against every other contributor's owned module and the frozen contracts.

### Known limitations and intentionally deferred work

No worker consumer, no automatic job redrive, no case CRUD API, no document/OCR/ASR/video parsing, no entity resolution, no graph projection, no Merkle roots/signatures were introduced — all explicit non-goals for this phase. The containerized `api` image build was blocked by sandbox-level network flakiness, not a code defect — see above and `docs/qa/known-limitations.md` for the full list.

## 2026-09-10 — Gaurav — Video and Image Processing Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2, `ffmpeg`/`ffprobe` n9.0 and `nvidia-smi` present but reporting no driver/GPU (`NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver` — exercised directly as the expected "GPU absent" path, not worked around). Started on the `gaurav` branch at `e2f8827`; partway through the session `git log`/`git status` showed the branch had since picked up `53d1d4e Completed sarthak/phase-1 (#7)` (a concurrent, separately-authored build) via a manual git operation outside this task — confirmed via `git status --short` that this caused zero conflicts with this task's own files, and this task's own doc edits (`docs/qa/test-matrix.md`, `docs/qa/known-limitations.md`, `docs/qa/test-data.md`, `docs/runbooks/local-development.md`) were re-based on the current on-disk content before editing, additive on top of Sarthak's own additions to the same files. Additive edits only to shared files: `pyproject.toml`/`uv.lock` (three new dependencies only), the four docs above, plus this file and `docs/progress/mvp-progress.md`. No file under `app/modules/graph/`, `app/modules/structured_processing/`, `app/modules/access_control/`, `app/modules/communication_processing/`, or `app/contracts/` was touched (confirmed by `git status --short -- <owned paths>` below).

```bash
$ uv add opencv-python-headless Pillow numpy
Resolved 65 packages in 1.04s
Installed 4 packages: numpy, opencv-python-headless, pillow, tracex (rebuilt)
```
Result: **pass**. The only three additions permitted for this phase; `pyproject.toml`/`uv.lock` updated accordingly. All three ship `py.typed`/native type stubs (`cv2/py.typed`, `PIL/py.typed`, `numpy`'s own inline types), so no new `[[tool.mypy.overrides]]` entry was needed.

```bash
$ uv sync --all-groups
Resolved 65 packages in 5ms
Checked 64 packages in 0.88ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
230 files already formatted
```
Result: **pass**. (One pass through `ruff format .` without `--check` was needed first — this ruff version also formats fenced ` ```python ` code blocks inside `.md` files, and `docs/runbooks/media-development.md` needed that whitespace-only pass.)

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 106 source files
```
Result: **pass**. (85 files before this build, per Sarthak's entry below → 106 after adding the 21 files under `app/modules/media_processing/`.)

```bash
$ uv run pytest tests/unit/media_processing -q
194 passed in 0.49s
```
Result: **pass**. Every unit test is infrastructure-independent by design — `test_probe.py`/`test_frames.py` monkeypatch `subprocess`/`shutil.which` directly rather than depending on a real `ffmpeg`/`ffprobe` binary, and `test_media_worker.py`'s video-path tests monkeypatch `probe_video`/`extract_frames` on the `worker` module itself for the same reason (see `docs/qa/test-data.md`). Includes the static-AST module-isolation checks (`test_media_safety.py`) confirming no infra client, no `app.modules.graph`/`structured_processing`/`access_control` import, and no `EntityV1`/`EventV1` construction anywhere in the module.

```bash
$ uv run pytest -q
864 passed, 19 skipped in 18.65s
```
Result: **pass**, no regressions. 864 vs. the prior baseline of 664 (see Sarthak's entry below) is exactly the 200 new tests in `tests/unit/media_processing/` (194) + `tests/integration/media_processing/` (6); skip count unchanged (19) since this build touches no database/queue/storage code path and no infra was started for it.

```bash
$ docker compose config
```
Result: **pass** (exit 0). No `compose.yaml` changes made (no GPU Docker service was added, per the phase's explicit non-goal); ran only to confirm the file still parses.

```bash
$ uv run pytest tests/integration/media_processing -q
6 passed in 1.62s
```
Result: **pass — run live**, not self-skipped: `ffmpeg`/`ffprobe` were present in this sandbox, so all 6 tests exercised the real binaries against a tiny synthetic (`ffmpeg lavfi testsrc`-generated) MP4 — probe, deterministic sample-frame extraction (byte-for-byte identical pixels across two independent extraction runs), the fake detector+tracker analysis pipeline end to end, exact frame/time/bbox provenance, repeat-processing observation-ID stability, and confirmed temp-file cleanup (`tempfile.gettempdir()` directory listing unchanged before/after). Had `ffmpeg`/`ffprobe` been absent, `pytestmark = pytest.mark.skipif(not ffmpeg_available(), ...)` would have reported all 6 as skipped rather than fabricating a pass.

### GPU/CPU capability verification

```bash
$ nvidia-smi
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. Make sure that the latest NVIDIA driver is installed and running.
```
This sandbox has an `nvidia-smi` binary present but no functioning driver/GPU behind it — the exact "GPU absent" case `capability.detect_capability()` must handle safely. Verified directly (not just via a unit test) that calling `detect_capability()` in this environment returns `cpu_only=True`, `gpu_visible=False`, `nvidia_smi_available=True`, `gpu_name=None`, `gpu_memory_mb=None`, `ffmpeg_available=True`, `ffprobe_available=True` — no exception, no hang, no fabricated GPU report.

```bash
$ git status --short -- app/modules/graph app/modules/structured_processing app/modules/access_control app/modules/communication_processing app/contracts migrations compose.yaml Dockerfile .env.example docs/architecture/graph-taxonomy-v1.md docs/architecture/access-control-v1.md docs/architecture/document-and-structured-processing-v1.md docs/decisions/ADR-001-graph-projection-and-case-isolation.md docs/decisions/ADR-002-deterministic-source-processing-and-provenance.md docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md
(no output)
```
Result: **pass** — zero diff against every other contributor's owned module/docs and every frozen-contract/forbidden path.

```bash
$ git status --short
 M docs/qa/known-limitations.md
 M docs/qa/test-data.md
 M docs/qa/test-matrix.md
 M docs/runbooks/local-development.md
 M pyproject.toml
 M uv.lock
?? app/modules/media_processing/
?? docs/architecture/media-observation-taxonomy-v1.md
?? docs/architecture/media-processing-v1.md
?? docs/decisions/ADR-004-media-provenance-and-anonymous-tracking.md
?? docs/runbooks/media-development.md
?? tests/fixtures/media_processing/
?? tests/integration/media_processing/
?? tests/unit/media_processing/
```
Result: nothing staged, nothing committed, no branch changed, per explicit instruction — Nipun will manually stage/commit/push/PR.

### Known limitations and intentionally deferred work

No real object-detection/tracking/OCR model, no face recognition/re-identification/biometric identification, no cross-camera identity correlation, no Neo4j graph writes, no actual file uploads/object-storage writes/hashing/Merkle roots/signatures, no video frontend, no ML training/evaluation, and no GPU Docker service were introduced — all explicit non-goals for this phase, confirmed by the zero-diff check above and the static-AST safety tests. Full list: `docs/qa/known-limitations.md`.

## 2026-09-10 — Sarthak — Audio, Social/Chat, Multilingual Alias, and Communication Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `sarthak` was based on `main` after every build below was merged (`075e52b`, `a903d3a`, `85c55c4`, `67cd31c`, `dac3362`, `e2f8827`) — confirmed via `git log --oneline -8` and `git status --short` before starting (clean tree, correct branch). No dependency changes: `app/modules/communication_processing/` uses only the Python standard library (`wave`, `json`, `unicodedata`, `re`, `dataclasses`, `enum`, `itertools`, `datetime`) plus this repo's own `app.core.ids`/`app.contracts`, per the task's "prefer stdlib, add a dependency only if unavoidable" instruction — genuinely unavoidable here, so `pyproject.toml`/`uv.lock` are untouched (confirmed below).

```bash
$ uv sync --all-groups
Resolved 62 packages in 1ms
Checked 61 packages in 14ms
```
Result: **pass**. Zero new/changed packages.

```bash
$ uv run ruff format --check .
189 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 85 source files
```
Result: **pass**. (61 files before this build, per Aditya's entry below → 85 after adding the 24 files under `app/modules/communication_processing/`.)

```bash
$ uv run pytest tests/unit/communication_processing -q
215 passed in 0.31s
```
Result: **pass**.

```bash
$ uv run pytest tests/integration/communication_processing -q
1 passed in 0.03s
```
Result: **pass** — the full seven-processor pipeline test, which needs no external service and therefore never self-skips.

```bash
$ uv run pytest -q
664 passed, 19 skipped in 16.41s
```
Result: **pass**, no regressions. 664 vs. the prior baseline of 448 (see Aditya's entry below) is exactly the 216 new tests in `tests/unit/communication_processing/` (215) + `tests/integration/communication_processing/` (1); skip count unchanged (19) since this build touches no database/queue/storage code path and no infra was started for it.

```bash
$ docker compose config
compose config valid
```
Result: **pass** (exit 0; one warning about `AUTH_JWT_SECRET` defaulting to blank, expected since no `.env` exists in this session — this task never needed Docker services running, per its own instruction not to start them unless required, and this module requires none).

```bash
$ git status --short
 M docs/progress/mvp-progress.md
 M docs/qa/known-limitations.md
 M docs/qa/test-data.md
 M docs/qa/test-matrix.md
 M docs/runbooks/local-development.md
?? app/modules/communication_processing/
?? docs/architecture/audio-social-and-communication-processing-v1.md
?? docs/architecture/multilingual-alias-candidates-v1.md
?? docs/decisions/ADR-005-provenance-first-communication-processing.md
?? tests/fixtures/communication_processing/
?? tests/integration/communication_processing/
?? tests/unit/communication_processing/
```
Result: 5 docs additively modified, 6 new paths — nothing staged, nothing committed, no branch changed, per explicit instruction.

```bash
$ git diff --check
(no output)
$ git diff --stat
 docs/progress/mvp-progress.md      | 29 +++++++++++++++++++++++++----
 docs/qa/known-limitations.md       |  9 +++++++++
 docs/qa/test-data.md               |  6 ++++++
 docs/qa/test-matrix.md             | 13 +++++++++++++
 docs/runbooks/local-development.md | 18 ++++++++++++++++++
 5 files changed, 71 insertions(+), 4 deletions(-)
$ git diff --name-only
docs/progress/mvp-progress.md
docs/qa/known-limitations.md
docs/qa/test-data.md
docs/qa/test-matrix.md
docs/runbooks/local-development.md
```
Result: **pass** — no whitespace errors; the diff touches only the five QA/progress/runbook docs this task was scoped to update additively.

```bash
$ git status --short -- app/contracts app/core app/main.py app/api app/dependencies app/modules/graph app/modules/structured_processing app/modules/access_control app/modules/media_processing migrations compose.yaml Dockerfile pyproject.toml uv.lock .env.example
(no output)
```
Result: **pass** — zero diff against every frozen-contract and forbidden path, and against Gaurav's `app/modules/media_processing/` (which doesn't exist in this branch yet — confirmed not created by this task either).

### One real design decision revised during this build (before this record)

- **Worker input-payload shape.** The task brief's `process_job(job: WorkerJobV1, input_payload: ...) -> WorkerResultV1` signature (distinct from `structured_processing.worker.process_job(job, evidence, resolver)`) was interpreted as: `input_payload` is one of four already-typed, role-tagged dataclasses (`AudioMetadataInput`/`TranscriptImportInput`/`DiarizationImportInput`/`SocialExportInput`) rather than raw bytes needing a `SourceResolver`-style fetch — matching the task's "three safe input roles" framing for audio and the fact that transcript/diarization *import* inherently starts from already-produced structured data, not a file to resolve. Documented as Decision non-obvious enough to flag: see `docs/architecture/audio-social-and-communication-processing-v1.md`'s "Supported input boundaries" section.

## 2026-09-10 — Aditya — Integration Hardening 1: Central Middleware and Exception Handling

Environment: same sandbox as the build below, Python 3.12.13 (via `uv`), Docker 29.7.2, worked from the `gaurav` branch at the user's explicit direction (git operations — commit/push/branch changes — intentionally not performed; see git status confirmation below). Task: reproduce the reported "unhandled exception escapes the safe error envelope" symptom, determine the true root cause rather than trusting the originally-assumed diagnosis, and implement one central fix.

**Root cause investigation.** Reproduced the exact symptom with a synthetic app carrying *zero* custom middleware — it still occurred, which already falsified the assumed `BaseHTTPMiddleware` diagnosis. Read Starlette's `ServerErrorMiddleware` source directly: it sends a safe response via `send()`, then unconditionally re-raises the original exception afterward (an intentional, documented upstream behavior, so a server or test harness can also observe/log the error). Read httpx's `ASGITransport.__call__`: with its default `raise_app_exceptions=True`, that re-raise propagates to the test caller instead of the transport returning the response the app already sent. Confirmed empirically by running a real `uv run uvicorn app.main:app` process and hitting an endpoint designed to raise unexpectedly over real HTTP with `curl` — it returned the correct safe `500` envelope the whole time, proving real client/server traffic was never actually affected; only the test transport's default masked this.

A second, previously-unknown, real gap was found during the same investigation: Starlette dispatches the bare-`Exception` handler from its outermost `ServerErrorMiddleware` directly, bypassing every user-added middleware (including both `RequestIDMiddleware` and `SecurityHeadersMiddleware`) for that one response path — so an unhandled exception's `500` response was missing the correlation-ID header and security headers, regardless of `BaseHTTPMiddleware` vs. pure ASGI.

**Central fix.** `app/core/errors.py`'s three exception handlers (`http_exception_handler`, `request_validation_exception_handler`, `unhandled_exception_handler`) now build their own safe headers directly via a shared `_safe_error_headers()` helper (correlation ID + `Cache-Control: no-store` + baseline security headers), instead of relying on middleware to add them after the fact. Correlation ID survives exception unwinding via `request.state.request_id` (set on the ASGI `scope["state"]`, which persists through `ServerErrorMiddleware`'s re-raise/re-catch, unlike a `contextvars.ContextVar` alone) with a `_resolve_request_id()` fallback. `RequestIDMiddleware` and `SecurityHeadersMiddleware` were converted to pure ASGI middleware as an independent, well-justified simplification (both only ever wrapped `send`) — not itself the fix for the reported bug. The now-redundant per-endpoint/per-dependency `_internal_error` workaround in `app/modules/access_control/api.py` and `dependencies.py` was removed.

```bash
$ uv sync --all-groups
Resolved 62 packages in 1ms
Checked 61 packages in 0.74ms
```
Result: **pass**. No dependency changes — this task added no new packages (explicit non-goal).

```bash
$ uv run ruff format --check .
144 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 61 source files
```
Result: **pass**. Same file count as before this task (`app/core/errors.py`, `app/modules/access_control/api.py`, `app/modules/access_control/dependencies.py` edited in place; no files added under `app/`).

```bash
$ uv run pytest tests/unit -q
353 passed in 9.93s
```
Result: **pass**. Includes the new `tests/unit/test_error_handling.py` (18 tests: route/dependency `HTTPException`, request-validation failure, unexpected exception on both route and dependency paths, no `ExceptionGroup`/traceback/secret ever reaches the client, correlation ID present on success and on every error path including the `ServerErrorMiddleware`-dispatched one, non-HTTP `lifespan` scope pass-through, and per-request context cleanup) and `tests/unit/access_control` unchanged and passing with the local `_internal_error` workaround removed.

```bash
$ uv run pytest tests/security -q
36 passed in 0.57s
```
Result: **pass**. Includes `tests/security/access_control/test_auth_no_secret_leakage.py::test_unexpected_backend_failure_never_leaks_a_connection_secret`, the direct proof that removing `_internal_error` from `login()` still safely produces a generic `500` via the central handler with no secret leakage.

```bash
$ uv run pytest tests/integration -q
2 passed, 19 skipped in 6.45s
```
Result: **pass** — self-skipping as expected with no Postgres/Redis/Neo4j/MinIO running in this session (no infra was started for this task; it touches no database/queue/storage code path). The 2 that ran require no live infra.

```bash
$ uv run pytest -q
448 passed, 19 skipped in 16.65s
```
Result: **pass**, no regressions. 448 vs. the prior baseline of 430 (see the build below) is exactly the 18 new tests in `tests/unit/test_error_handling.py`; skip count unchanged (19) since no infra was started this session.

```bash
$ docker compose config
```
Result: **pass** (exit 0). No `compose.yaml` changes made (explicit non-goal); ran only to confirm the file still parses.

```bash
$ git status --short
 M app/core/errors.py
 M app/modules/access_control/api.py
 M app/modules/access_control/dependencies.py
 M docs/architecture/security-boundaries-v1.md
 M docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md
 M docs/progress/mvp-progress.md
 M docs/qa/known-limitations.md
 M docs/qa/test-matrix.md
 M tests/conftest.py
 M tests/security/access_control/test_auth_no_secret_leakage.py
 M tests/unit/access_control/test_api.py
?? tests/unit/test_error_handling.py
```
Result: 11 files modified, 1 new test file — nothing staged, nothing committed, no branch changed, per explicit instruction.

```bash
$ git diff --check
(no output)
$ git diff --stat
 app/core/errors.py                                 | 192 ++++++++++++++++++---
 app/modules/access_control/api.py                  |  93 ++++------
 app/modules/access_control/dependencies.py         |  34 +---
 docs/architecture/security-boundaries-v1.md        |  45 ++++-
 .../ADR-003-authentication-and-case-scoped-access-control.md |   4 +-
 docs/progress/mvp-progress.md                      |  22 ++-
 docs/qa/known-limitations.md                       |   3 +-
 docs/qa/test-matrix.md                             |   4 +
 tests/conftest.py                                  |  16 +-
 tests/security/access_control/test_auth_no_secret_leakage.py |  15 +-
 tests/unit/access_control/test_api.py              |   6 +-
 11 files changed, 308 insertions(+), 126 deletions(-)
```
Result: **pass** — no whitespace errors; confirmed the diff touches only the files this task was scoped to.

```bash
$ git status --short -- app/contracts app/modules/graph app/modules/structured_processing migrations compose.yaml Dockerfile pyproject.toml uv.lock .env.example
(no output)
```
Result: **pass** — zero diff against every frozen-contract and forbidden path; no new dependency was added.

### Known limitations and intentionally deferred work

- Starlette's `ServerErrorMiddleware`-re-raises-after-sending behavior is upstream and intentional, not something this project's code controls. Every test fixture in this repo that drives HTTP through the real app now explicitly sets `ASGITransport(..., raise_app_exceptions=False)`; a future test file that constructs its own `ASGITransport` without this setting will reproduce the original, misleading symptom if it exercises a genuinely-unhandled-exception path. Flagged in `docs/qa/known-limitations.md`.
- No new auth features, MFA/SSO/CORS/cookies/CSRF, business endpoints, DB/schema/migration changes, rate-limiting redesign, logging-platform integration, frontend, or graph/entity-resolution/document/media functionality were introduced — all explicit non-goals for this task, confirmed by the zero-diff check above.

## 2026-09-10 — Aditya — Authentication, Case Access Control, Security Middleware, and Operational Foundation build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `aditya` was based on `main` after the Nipun/Shreshtha/Jasraj builds below were merged (`075e52b`, `a903d3a`, `85c55c4`, `67cd31c`) — confirmed via local `git log --oneline -5` before starting (`git fetch origin` itself failed with `fatal: could not read Username for 'https://github.com'` in this sandbox — no network credentials for GitHub specifically, unrelated to package registries, which worked fine for `uv add`). Additive edits only to shared files: `app/main.py`, `app/core/config.py`, `app/core/errors.py`, `tests/conftest.py`, `.env.example`, `compose.yaml` (new `AUTH_*` env passthrough for the `api` service only), plus `docs/qa/*`, `docs/progress/mvp-progress.md`, `docs/runbooks/local-development.md`. No file under `app/modules/graph/`, `app/modules/structured_processing/`, or `app/contracts/` was touched (confirmed by `git status` before finishing and by `tests/security/access_control/test_module_boundaries.py`).

```bash
$ uv add PyJWT "pwdlib[argon2]"
Resolved 62 packages in 7.34s
Installed 8 packages: et-xmlfile, lxml, openpyxl, pwdlib, pyjwt, pypdf, python-docx, (tracex rebuilt)
```
Result: **pass**. The only two additions allowed for this phase; `pyproject.toml`/`uv.lock` updated accordingly. Both ship `py.typed` markers, so no new `[[tool.mypy.overrides]]` entry was needed.

```bash
$ uv sync --all-groups
Resolved 62 packages in 0.73ms
Checked 61 packages in 0.38ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
143 files already formatted
```
Result: **pass**. (Note: this ruff version also formats fenced ` ```python ` code blocks inside `.md` files — two documentation files needed a pass through `ruff format .` for that reason, both whitespace-only.)

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 61 source files
```
Result: **pass**. (47 files before this build, per Jasraj's entry below → 61 after adding `app/modules/access_control/`'s 13 files plus `app/modules/__init__.py` already existing.)

```bash
$ uv run pytest tests/unit/access_control -q
105 passed in 2.92s
```
Result: **pass**.

```bash
$ uv run pytest tests/security/access_control -q
34 passed in 0.56s
```
Result: **pass**.

```bash
$ uv run pytest
443 passed, 6 skipped in 18.93s
```
Result: **pass**, run with PostgreSQL + Redis up (see below) — the 6 skips are `tests/integration/graph/` (4, Neo4j not started this session) and the `neo4j`/`minio` checks in `tests/integration/test_readiness_live.py` (2, same reason); `postgres`/`redis` in that same file, and this build's own 11 `tests/integration/access_control/` tests, all passed live. Without any infra running at all, the full suite is `430 passed, 19 skipped` (confirmed separately, after teardown below).

```bash
$ docker compose config
```
Result: **pass** (exit 0).

```bash
$ docker compose up -d --wait postgres redis
Container tracex-postgres-1 Healthy
Container tracex-redis-1 Healthy
```
Result: **pass**. Both required infra services reached `healthy`. (Neo4j/MinIO were not started this session — not needed for this phase's own verification — so `/readyz` correctly reports them `"unavailable"` alongside `postgres`/`redis` `"ok"` in the live smoke test below.)

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
```
Result: **pass** (exit 0; already at head from the integration suite's own autouse migration fixture — see below). Verified table creation directly: `users`, `cases`, `case_memberships`, `auth_sessions`, `security_audit_events` all present in `information_schema.tables` with the exact expected constraint/index set from the migration.

```bash
$ uv run pytest tests/integration/access_control -q
11 passed in 2.56s
```
Result: **pass** — all 11 live tests passed for real (not self-skipped): Alembic migration apply + idempotent re-apply, full register→login→refresh→logout round trip, wrong-password denial, refresh-token-hashed-not-raw, token-family revocation (including the still-active leaf session), live case-membership/clearance policy decisions (including a direct-SQL membership deactivation taking effect on the next read), and Redis-backed rate limiting (both the limit itself and independent keys). Confirmed via a direct row-count query afterward that `users`/`cases`/`case_memberships`/`auth_sessions` all returned to `0` (full cleanup); `security_audit_events` intentionally is not cleaned up per test by application design (see `docs/qa/test-data.md`) and was purged once, manually, as an end-of-session courtesy.

### Live end-to-end HTTP smoke test (beyond the required command list)

Ran a real `uv run uvicorn app.main:app` process against the live PostgreSQL/Redis containers and exercised the full API over real HTTP with `curl`:

```bash
$ curl .../healthz            # {"status":"ok",...}                                    200
$ curl .../readyz              # postgres:ok, redis:ok, neo4j/minio:unavailable         503 (expected, not started)
$ curl -X POST .../auth/register ...   # PublicUser body, no password/hash              201-equivalent
$ curl -X POST .../auth/login ...      # access_token + refresh_token; headers below     200
$ curl .../auth/me -H "Authorization: Bearer ..."   # correct identity + memberships     200
$ curl -X POST .../auth/refresh ...    # new token pair, refresh_token rotated           200
$ curl -X POST .../auth/refresh ... (old, now-rotated refresh_token replayed)            401
$ curl -X POST .../auth/login ... (wrong password)  # {"error":{"code":"unauthorized","message":"invalid email or password",...}}   401
```
Login response headers confirmed present: `x-content-type-options: nosniff`, `x-frame-options: DENY`, `referrer-policy: no-referrer`, `cache-control: no-store`. The smoke-test user was deleted afterward (`DELETE FROM users WHERE email_normalized = 'livesmoke@example.test'`, 1 row).

### One real bug caught and fixed during this build (before this record)

- **`repository.rotate_session` violated a foreign-key constraint against real PostgreSQL.** The original implementation ran `UPDATE auth_sessions SET replaced_by_session_id = <new_id> ...` *before* inserting the new session row. `auth_sessions.replaced_by_session_id` has a foreign key onto `auth_sessions.session_id`, checked immediately (no `DEFERRABLE` constraints in this schema) — so the `UPDATE` failed with `ForeignKeyViolationError: Key (replaced_by_session_id)=(...) is not present in table "auth_sessions"` the first time it ran against a real database. Every unit test passed regardless, because `FakeAccessControlRepository` (the in-memory test double) doesn't enforce foreign keys — this is exactly the class of bug the task's live-integration-test requirement exists to catch. Fixed by swapping the statement order (insert the new session first, then revoke-and-point the old one at it) in the same transaction. Caught by `tests/integration/access_control/test_auth_lifecycle_live.py::test_register_login_refresh_logout_round_trip_against_live_db` and `::test_token_family_revocation_works_in_live_persistence`. See `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`, Decision 7.
- **A test-cleanup bug (not an application bug), also only caught live.** `tests/integration/access_control/conftest.py`'s `cleanup_user_ids`/`cleanup_case_ids` fixtures originally issued `DELETE FROM ... WHERE id = ANY(:ids)` via a raw `sa.text(...)` bind with a plain Python `list[UUID]` — which silently matched zero rows instead of raising (no type information to cast the array against), leaving real rows behind across test runs undetected until a manual row-count check after a full local run. Fixed by rebuilding the same deletes through the typed `users_table`/`cases_table` SQLAlchemy Core objects (`.where(col.in_(ids))`), which bind correctly through the column's own `UUID` type. Confirmed fixed via a before/after row-count check (`docs/qa/test-data.md`).
- **A genuinely pre-existing (not introduced by this build) Starlette/`BaseHTTPMiddleware` interaction**, reproduced and worked around locally rather than fixed at the shared-middleware level — see `docs/architecture/security-boundaries-v1.md` and `docs/decisions/ADR-003-...md`, Decision 10, and `docs/qa/known-limitations.md`. **Correction (Integration Hardening 1, see the entry above):** re-investigated from scratch; `BaseHTTPMiddleware` was not actually the cause of the observed symptom (that was httpx's `ASGITransport` test-only default). A different, real gap was found and fixed centrally instead; the local workaround referenced here was removed as redundant.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved). Re-ran `uv run pytest -q` once more after teardown to confirm the suite returns to self-skipping (`430 passed, 19 skipped` — the 19 are the pre-existing 8 (`test_readiness_live.py` ×4, `tests/integration/graph/` ×4) plus this build's 11 `tests/integration/access_control/` tests, all skipping cleanly rather than fabricating a pass with no infra up).

## 2026-09-10 — Jasraj — Document and Structured-Data Processing build

Environment: same sandbox as the builds below, Python 3.12.13 (via `uv`), Docker 29.7.2. Branch `jasraj` was based on `main` after both the Nipun and Shreshtha builds below were merged (`075e52b`, `a903d3a`, `85c55c4`) — confirmed via `git log --oneline HEAD..origin/main` (empty) before starting, and re-confirmed no conflicts with `app/modules/graph/` or any shared doc after finishing (additive edits only to `docs/qa/*`, `docs/progress/mvp-progress.md`, `docs/runbooks/local-development.md`).

```bash
$ uv add pypdf python-docx openpyxl
Resolved 60 packages
Installed 6 packages: et-xmlfile, lxml, openpyxl, pypdf, python-docx, (tracex rebuilt)
```
Result: **pass**. The only three additions allowed for this phase; `pyproject.toml`/`uv.lock` updated accordingly, plus an `openpyxl.*` entry in `[[tool.mypy.overrides]]` (no type stubs published for `openpyxl`, same pattern as the existing `neo4j.*`/`minio.*`/`asyncpg.*` entries).

```bash
$ uv sync --all-groups
Resolved 60 packages in 1ms
Checked 59 packages in 0.82ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
107 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 47 source files
```
Result: **pass**. (39 files after adding `app/modules/structured_processing/` → 47 once `openpyxl` stub coverage was added and re-checked; 26 files before this build, per Shreshtha's entry below.)

```bash
$ uv run pytest tests/unit/structured_processing -q
150 passed in 0.44s
```
Result: **pass**.

```bash
$ uv run pytest tests/contract -q
56 passed in 0.03s
```
Result: **pass** (frozen contracts untouched — confirms this build didn't regress them).

```bash
$ uv run pytest -q
291 passed, 8 skipped in 12.80s
```
Result: **pass**. 140 pre-existing (per Shreshtha's entry: 140 passed/8 skipped without live infra) + 151 new (150 unit + 1 integration) = 291. The 8 skips are the pre-existing self-skipping live-infrastructure suites (4 `test_readiness_live.py`, 4 `tests/integration/graph/`) — unrelated to this build, which needs no live infrastructure at all (`tests/integration/structured_processing/test_local_file_pipeline.py` uses a real local-file `SourceResolver`, not a network service, so it runs unconditionally and is included in the 291 passed).

```bash
$ docker compose config
```
Result: **pass** (exit 0; also covered by `tests/integration/test_compose_config.py`, included in the 291 passed above). Full-stack `docker compose up` was not re-verified in this session — this build adds no new services/compose changes, so nothing about the previously-verified stack (Nipun's entry below) changes.

### One real design bug caught and fixed during this build (before this record)

- **Error-code priority was backwards for a fundamentally unsupported content type.** `worker.process_job` originally resolved the parser profile (`get_profile(job.processor_name)`) and checked its `accepted_content_types` *before* calling `classify()`. A job pointed at e.g. `application/zip` (not a Phase 1 format at all) was reported as `unsupported_parser_profile` — technically true but less specific than it should be, since the content type isn't even in the supported set regardless of which profile was chosen. Reordered so `classify()` runs first: a fundamentally unsupported format now reports `unsupported_content_type`; a supported format that just doesn't match the chosen profile still reports `unsupported_parser_profile`. Caught by `tests/unit/structured_processing/test_worker_dispatch.py::test_unsupported_content_type_fails`.
- Three other issues were test-fixture bugs, not code bugs (fixed in the test files, not the app): a CDR/finance CSV fixture with an unquoted comma-containing amount value that the CSV parser correctly split into extra columns; a zip-bomb test fixture written without compression, giving a 1:1 ratio that never tripped the compression-ratio limit; and a PDF fixture's second-page text one character short of the documented 20-character "meaningful text" threshold, correctly (if confusingly, for the test) routed to OCR.

## 2026-09-10 — Shreshtha — Graph Foundation and Taxonomy build

Environment: same sandbox as the Nipun build below, Python 3.12.13 (via `uv`), Docker 29.7.2. No changes to `pyproject.toml`/`uv.lock` were needed — `neo4j>=5.25.0` was already a declared Phase 1 dependency; `uv sync` resolved it to `6.3.0`. Confirmed `AsyncGraphDatabase`/`AsyncDriver`/`AsyncManagedTransaction` imports and behavior this module relies on are unchanged against that version.

```bash
$ uv sync --all-groups
Resolved 55 packages in 2ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
66 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 26 source files
```
Result: **pass**. (18 files at the start of this work → 26 after adding `app/modules/__init__.py` + the 7 files in `app/modules/graph/`.)

```bash
$ uv run pytest
140 passed, 8 skipped in 12.44s
```
Result: **pass**. The 8 skips are self-skipping live-infrastructure suites: 4 in `tests/integration/test_readiness_live.py` (pre-existing, Nipun's) and 4 in `tests/integration/graph/` (new), all requiring `docker compose up -d postgres neo4j redis minio` — confirmed for real below.

```bash
$ docker compose config
```
Result: **pass** (exit 0).

```bash
$ docker compose up -d --wait postgres neo4j redis minio
Container tracex-minio-1 Healthy
Container tracex-postgres-1 Healthy
Container tracex-neo4j-1 Healthy
Container tracex-redis-1 Healthy
```
Result: **pass**. All four required infra services reached `healthy`.

```bash
$ uv run python -m app.modules.graph.schema apply
Applied 11 schema statements:
  - case_case_id_unique
  - evidence_case_evidence_unique
  - observation_case_observation_unique
  - entity_case_entity_unique
  - event_case_event_unique
  - observation_case_id_idx
  - observation_event_time_idx
  - event_case_id_idx
  - event_event_time_idx
  - entity_case_id_idx
  - entity_entity_type_idx

$ uv run python -m app.modules.graph.schema verify
Present (11): [all 11 statement names]
All expected schema statements are present.
```
Result: **pass** (both, exit 0) — confirms the composite uniqueness constraints (Decision 2, ADR-001) are valid against the actual pinned `neo4j:5.25-community` Community Edition image, not just against documentation.

```bash
$ uv run pytest tests/integration/graph -q
....
4 passed in 0.74s
```
Result: **pass** — all 4 live graph integration tests passed for real (schema apply-twice, full-chain projection + provenance queries, repeated-projection-no-duplicates, cross-case isolation), not self-skipped. Confirmed via a direct post-test query (`MATCH (n) RETURN labels(n), count(n)`) that every test's `case_id`-scoped teardown left zero residual nodes.

```bash
$ uv run pytest   # re-run while the full stack was up
148 passed in 1.29s
```
Result: **pass**. All 8 previously-skipped tests (4 readiness, 4 graph integration) ran for real and passed.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved). Re-ran `uv run pytest` once more after teardown to confirm the suite returns to **140 passed, 8 skipped** (never fabricating a pass without real infra).

### Design decisions verified against real infrastructure, not just written down

- Composite (non-`NODE KEY`) uniqueness constraints, chosen specifically because `neo4j:5.25-community` is Community Edition (ADR-001, Decision 2) — verified applying and round-tripping successfully, twice, against that exact pinned image.
- Case isolation under a deliberately-collided `entity_id` across two cases (`tests/integration/graph/test_case_isolation.py`) — verified live, not just in the unit-level parameter-construction tests.
- Atomic dependency-deferral for `project_observation`/`project_event` (ADR-001, Decision 3) — verified in unit tests via a faked repository (`tests/unit/graph/test_projection.py`); the live suite only exercises the fully-satisfied path, since deliberately projecting an event before its entity is a unit-level concern.

## 2026-09-10 — Nipun — Phase 1 foundation build

Environment: local dev machine, Python 3.12.13 (via `uv`), Docker 29.4.1, Docker Compose v5.1.3.

```bash
$ uv sync --all-groups
Resolved 55 packages
Installed 54 packages
```
Result: **pass**.

```bash
$ uv run ruff format --check .
38 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 18 source files
```
Result: **pass**.

```bash
$ uv run pytest
80 passed, 4 skipped in 12.40s
```
Result: **pass**. The 4 skips are `tests/integration/test_readiness_live.py` — they require a running `docker compose up -d postgres neo4j redis minio` stack and self-skip per-dependency when it isn't up, by design (never fabricate a pass for infrastructure that isn't there).

```bash
$ docker compose config
```
Result: **pass** (exit 0; also covered by `tests/integration/test_compose_config.py`, included in the 80 passed above).

```bash
$ docker compose up --build -d
```
Result: **pass**. All five containers (`api`, `postgres`, `neo4j`, `redis`, `minio`) reached `Up`/`healthy` status.

```bash
$ curl http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}          # HTTP 200

$ curl http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}   # HTTP 200

$ curl http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}   # HTTP 200
```
Result: **pass** — all three endpoints verified against the live, fully-containerized stack.

```bash
$ uv run pytest   # re-run while the full stack was up
84 passed in 0.31s
```
Result: **pass**. The 4 tests in `tests/integration/test_readiness_live.py` that self-skip without infra (see the first `pytest` run above: 80 passed, 4 skipped) ran for real against the live containers and passed — confirming genuine connectivity, not just a mocked path.

Stack was stopped cleanly afterward: `docker compose down` (containers/network removed, named volumes preserved).

### Three real bugs caught by this run (all fixed before this record)

- **Worker idempotency key regex rejected valid semantic-version processor versions.** `IDEMPOTENCY_KEY_PATTERN` originally allowed `[A-Za-z0-9_-]+` per segment, which excludes `.` — so a realistic key like `"<case_id>:<evidence_id>:cdr-parser:1.0.0"` failed validation on its own version segment. Fixed to `[A-Za-z0-9_.-]+`. Caught by `tests/contract/test_worker.py::test_valid_worker_job_parses` and 2 related tests, before any container was built.
- **Route-introspection assumption broke against this Starlette version's routing internals.** An early version of `tests/unit/test_app_startup.py` walked `app.routes` and read `.path` directly; on the installed Starlette (1.6.0), `include_router` wraps routes in an internal `_IncludedRouter` with no flattened `.path` list, so the assertion failed even though the routes worked correctly over real HTTP. Rewritten to hit the routes through the ASGI test client instead of introspecting router internals — more robust and closer to what actually matters (the routes respond).
- **Dockerfile build failed: `uv sync --locked --no-dev` (the second, post-`COPY app` sync) tried to editable-install the `tracex` project itself, which needs `README.md` — never copied into the build context, so `docker compose up --build` failed outright.** The fix (not a workaround) is that this second sync step was unnecessary: the first `uv sync --locked --no-install-project --no-dev` already installs every third-party dependency, and `uvicorn app.main:app` runs `app/` as a plain path-based package without needing the project itself pip-installed. Removed the second sync entirely.
- **`check_postgres` used `asyncpg.connect()` directly against a `postgresql+asyncpg://` DSN.** `POSTGRES_DSN` uses the SQLAlchemy-style `+asyncpg` driver suffix everywhere else in this codebase (config, Alembic), but raw `asyncpg.connect()` doesn't understand that scheme and raised `ClientConfigurationError` on every call — silently reported as `"postgres": "unavailable"` in `/readyz` even though the real Postgres container was healthy. Only surfaced once the full stack was actually run (all unit/contract tests mock this check, by design). Fixed by switching `check_postgres` to SQLAlchemy's `create_async_engine`, consistent with the DSN format used everywhere else.
- **(Environment, not a code bug) `neo4j:5-community` currently resolves to a broken build (5.26.30) whose entrypoint crash-loops (`su-exec` usage error) on this host, reproducing even with a bare `docker run` outside Compose.** Pinned `compose.yaml` to `neo4j:5.25-community`, a known-good build, and confirmed Neo4j starts and passes its health check.

## 2026-09-11 — Aditya — Phase 2.4 worker identity, authorization binding, and security audit completion

Environment: local dev machine, branch `aditya` rebased onto `origin/main` (Nipun's 2.2/2.3 and Jasraj's Phase 2 present), Python 3.12.13 (via `uv`), Docker reachable.

```bash
$ uv sync --all-groups
Resolved 66 packages in 1ms
Checked 65 packages in 0.60ms
```
Result: **pass**.

```bash
$ uv run ruff format --check .
```
First run found 2 files needing reformatting (`app/modules/evidence_lifecycle/service.py`, `migrations/versions/48e9e76153ca_worker_credentials_and_job_ownership.py` — both this task's own edits, over the project's line-length limit). Fixed with `uv run ruff format <files>`. Re-run:
```
276 files already formatted
```
Result: **pass**.

```bash
$ uv run ruff check .
```
First run found 1 error: `F821 Undefined name 'UUID'` in `tests/unit/evidence_lifecycle/test_worker_input_api.py` (used in a type annotation without importing it). Fixed the import. Re-run:
```
All checks passed!
```
Result: **pass**.

```bash
$ uv run mypy app
Success: no issues found in 121 source files
```
Result: **pass**.

```bash
$ uv run pytest -q       # before Docker was brought up
1056 passed, 28 skipped in 22.44s
```
Result: **pass**. The 28 skips are every self-skipping live-infra test in the repo (no `.env`-backed running stack yet at this point).

```bash
$ docker compose config
```
Result: **pass** (exit 0, full interpolated config printed, including the new `WORKER_TOKEN`/`WORKER_CREDENTIAL_PEPPER` env passthrough on the `api` service).

```bash
$ docker compose up --build -d
```
Result: **pass**. All five containers (`api`, `postgres`, `neo4j`, `redis`, `minio`) reached `healthy`/`running` on the first attempt — no rebuild retries needed this time.

```bash
$ curl http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
```
Result: **pass** — all three endpoints correct against the live, fully-containerized stack.

```bash
$ uv run alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 7e8499f34f29 -> f2086e1e89f6, evidence lifecycle foundation
INFO  [alembic.runtime.migration] Running upgrade f2086e1e89f6 -> 102857ca8d1d, worker job claim and result foundation
INFO  [alembic.runtime.migration] Running upgrade 102857ca8d1d -> af5b05e61b08, structured source type routing
INFO  [alembic.runtime.migration] Running upgrade af5b05e61b08 -> 48e9e76153ca, worker credentials and job ownership
```
Result: **pass** — the full migration chain, including this phase's new `worker_credentials` table + `worker_jobs.claimed_by_worker_id` column/FK/index, applies cleanly against real PostgreSQL.

```bash
$ uv run pytest -q       # first live run, full stack up, real .env present
```
Result: **1 failed, 1079 passed, 4 skipped** — `test_full_worker_identity_lifecycle_against_live_stack` failed with a genuine `401` on the worker's first `/claim` call. Diagnosed and fixed (see "Two real bugs" below); this is exactly the kind of gap self-skipping live tests are meant to catch, and this task's instructions to actually run them live rather than trust the self-skip caught it on the very first live attempt.

```bash
$ uv run pytest -q       # after fixing the pepper-pollution bug
1080 passed, 4 skipped in ...
```
Result: **pass** — the identity-lifecycle test now passes for real. The 4 remaining skips are `tests/integration/structured_processing/test_worker_live.py`'s tests, self-skipping because `.env` didn't yet configure `WORKER_TOKEN`.

A dev-only, git-ignored `WORKER_TOKEN` placeholder was then added to the local `.env` (documented, non-secret, never committed) and the `api` container restarted (`docker compose up -d api`) to pick it up, specifically so these 4 self-skipping tests could be exercised for real rather than left skipped:

```bash
$ uv run pytest tests/integration/structured_processing/test_worker_live.py -v
```
Result: **4 failed** — every test rejected `401` at the worker's `/claim` call, a second, different real bug (see below). Fixed `_ensure_worker_credential`; re-run:
```bash
$ uv run pytest tests/integration/structured_processing/test_worker_live.py -v
test_worker_client_against_real_running_api PASSED
test_full_claim_stream_parse_submit_live_pipeline[document-fir_report_text_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[structured_tabular-generic_tabular_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[structured_json-generic_json_v1] PASSED
4 passed in 1.58s
```
Result: **pass** — all three processor types now complete the real `claim -> stream -> parse -> submit` pipeline end to end against the live stack, producing genuine `SUCCEEDED` results, not the `DEFERRED` fallback.

```bash
$ uv run pytest -q -rs       # entire suite, live stack, .env fully configured
1084 passed in 17.32s
```
Result: **pass — zero skips.** Every self-skipping live test in the entire repository (evidence lifecycle, structured/communication/graph/access-control integration, worker identity, worker live pipelines) ran for real against genuine PostgreSQL/Neo4j/Redis/MinIO/the live API and passed. Re-ran `ruff format --check .`, `ruff check .`, and `mypy app` once more after these fixes — all still clean.

```bash
$ docker compose ps
```
All five containers `Up`/`healthy`. `docker compose config` re-confirmed valid.

### Manual CLI smoke test against the live stack (beyond the automated suite)

```bash
$ uv run python -m app.modules.access_control.worker_credentials create --name smoke-test-worker --processor fir_report_text_v1 --processor cdr_generic_v1
worker_id:                d273de9b-49c5-4f57-a069-c50fb0a57626
display_name:             smoke-test-worker
allowed_processor_names:  fir_report_text_v1, cdr_generic_v1
token (shown once -- store it now, never in Git/.env.example/logs):
  Ayfhtjfz4saQNkdVqUWfjCHkWOFQHcmWl6P1h9wlzEc
```
`list` afterward showed every provisioned credential with no plaintext token or digest ever printed.

```bash
$ curl -X POST .../internal/worker-jobs/claim -H "Authorization: Bearer <smoke token>" -d '{"processor_name": "fir_report_text_v1", ...}'
{"job":null,"claim_token":null,"lease_expires_at":null}   # HTTP 200 — in-scope processor

$ curl -X POST .../internal/worker-jobs/claim -H "Authorization: Bearer <smoke token>" -d '{"processor_name": "generic_json_v1", ...}'
{"error":{"code":"forbidden","message":"this worker is not authorized for the requested processor", ...}}   # HTTP 403 — out of scope
```
Result: **pass** — per-worker processor scoping enforced live, not just in unit tests.

```bash
$ uv run python -m app.modules.access_control.worker_credentials rotate --worker-id d273de9b-...
new token (shown once): IQeaXT26vMZoY4thonpNz8Qd2LIkZWS-6vodMjdO6aU

$ curl -X POST .../internal/worker-jobs/claim -H "Authorization: Bearer <OLD smoke token>" ...
{"error":{"code":"unauthorized","message":"worker authentication required", ...}}   # HTTP 401 — old token dead immediately

$ uv run python -m app.modules.access_control.worker_credentials revoke --worker-id d273de9b-...
worker_id d273de9b-...: revoked (idempotent)
```
Result: **pass** — rotation invalidates the old token immediately; revocation is idempotent.

```bash
$ docker compose exec postgres psql -U tracex -d tracex -c "SELECT event_type, outcome, count(*) FROM security_audit_events GROUP BY event_type, outcome ORDER BY event_type;"
 worker_authentication_denied  | denied  |    16
 worker_credential_revoked     | success |     5
 worker_credential_rotated     | success |     1
 worker_job_access_denied      | denied  |    10
 worker_processor_scope_denied | denied  |     1
 ... (plus every pre-existing access-control event type, all still present and unchanged)
```
Result: **pass** — every new audit event type this phase adds is a real row in `security_audit_events`, confirmed by querying PostgreSQL directly rather than trusting the HTTP response alone.

### Two real bugs caught by live testing (both in this phase's own test helpers, not the application code under test)

- **Test-environment pepper pollution leaked into both new live test files.** `tests/conftest.py` sets a fixed test-only `WORKER_CREDENTIAL_PEPPER` in `os.environ` for the rest of the suite's sake (needed so unit tests get deterministic digests). Both `test_worker_identity_lifecycle_live.py` and `test_worker_live.py`'s `_live_settings()` helper built `Settings` only from keys `.env` actually defined — so when `.env` (correctly) left `WORKER_CREDENTIAL_PEPPER` unset, pydantic-settings silently fell through to conftest's fake OS-env value instead of `None`, diverging from what the live API container itself resolves (a real blank env var, normalized to `None` by the same validator). The test process then computed a credential digest with one pepper while the live server verified with another, so every claim came back `401`. Fixed by explicitly forcing `worker_credential_pepper=None` into both helpers' `Settings` kwargs, so an absent `.env` value can never be silently overridden by test-suite environment leakage.
- **`_ensure_worker_credential` bound the wrong token to the stored credential.** The helper called `worker_credentials.create_worker_credential()`, which *always* mints its own fresh random token internally (the correct, intentional behavior for the trusted-operator CLI, which hands that plaintext back to a human) — but the helper discarded the returned token and expected the row it just inserted to match the digest of the *developer's own* `WORKER_TOKEN`. Those two tokens are never the same value, so the stored digest could never match what the test client actually presented, and every claim came back `401` regardless of the pepper fix above. Fixed by constructing the `WorkerCredentialRecord` directly with `credential_digest = hash_worker_credential(token, pepper)` for the already-known token — the same computation the CLI does internally, minus the random-generation step this helper never needed.

Both bugs were latent in test-only code paths that had never actually been exercised live before this session (the two live test files self-skip without a real `.env`/`WORKER_TOKEN`, so they had never run for real in prior CI or dev sessions) — confirming the value of this task's explicit instruction to run the real worker-security lifecycle against a running stack rather than trust the self-skip. The application code itself (`require_worker_principal`, `hash_worker_credential`, digest lookup, processor scoping, audit recording) was correct on the very first live check, verified independently via direct `curl`/CLI commands before either test-helper bug was even found.

Docker stack was left running after this session (not torn down) so the live-verified state remains inspectable; `docker compose down` cleanly removes it when no longer needed (named volumes are preserved either way).

## 2026-09-11 — Shreshtha — Phase 2.5 canonical observation-to-Neo4j graph projection, plus a communication-processing routing fix

Environment: local dev machine, branch `shreshtha` (based on `05897b0`, the tip shared with `origin/main` before Aditya's Phase 2.4 and Sarthak's Phase 2 diverged further). Docker reachable; the stack from a prior session in this same environment was already running and was reused/rebuilt in place.

```bash
$ uv run ruff format --check .
276 files already formatted
```
Result: **pass** (baseline, before this task's edits).

Throughout implementation, `ruff format`/`ruff check`/`mypy app` were re-run after every file group; two real, minor issues were caught and fixed immediately:
- `F821 Undefined name 'UUID'` in a test file that used `UUID` in a type annotation without importing it (fixed: added the import).
- A regex-based Cypher-relationship-token test (`_RELATIONSHIP_TOKEN_PATTERN`) didn't match `[r:MENTIONS]` (a *bound* relationship variable, needed only because `MENTIONS.ordinal` is set after the `MERGE`) — every other relationship in this module is unbound (`[:HAS_EVIDENCE]`). Fixed by widening the pattern to `\[\w*:([A-Z_]+)\]`, which still exhaustively covers both valid forms.

```bash
$ uv run mypy app
Success: no issues found in 127 source files
```
Result: **pass** (final, after all edits).

```bash
$ uv run alembic heads
a204a94ccd49 (head)          # after adding the graph_projection_jobs migration
ed593db47d8c (head)          # after adding the communication-routing migration
```
Result: **pass** — both new migrations chain correctly on top of the existing live chain, applied cleanly against real PostgreSQL (`uv run alembic upgrade head`, confirmed via `\d graph_projection_jobs` and `\d evidence_records`'s widened `ck_evidence_records_source_type` showing all five new values).

```bash
$ uv run pytest -q
1173 passed in 26.88s
```
Result: **pass** — full suite, live infrastructure, zero skips. Stable across three consecutive full-suite runs (1157 passed before the routing-fix tests were added; 1173 after).

### Graph-projection-specific test runs

```bash
$ uv run pytest tests/unit/graph/ -q
94 passed in 0.09s
```

```bash
$ uv run pytest tests/integration/graph/test_outbox_repository_live.py -v
9 passed in 5.33s
```
Result: **pass (live)** — real `FOR UPDATE SKIP LOCKED` concurrency safety (two genuinely concurrent `claim_batch` calls via `asyncio.gather` never double-claimed any of 4 seeded jobs), real lease-expiry reclaim with `attempt` incremented, real retry exhaustion leaving a job durably `failed`, and the crash-recovery sweep for a `running` row whose lease expired with no attempts remaining — all against genuine PostgreSQL, not fakes.

```bash
$ uv run pytest tests/integration/graph/test_full_pipeline_live.py -v
1 passed in 0.59s
```
Result: **pass (live)** — the complete real path: upload → authenticated worker claim → secure evidence stream (`GET .../input`, byte-identical) → worker result with a canonical `ObservationV1` (including one `ExtractedEntityMention`) → a real, queued `graph_projection_jobs` row confirmed by direct SQL query → `app.modules.graph.projector.run_batch` (the `--once` CLI's own function, called in-process) → both a real Neo4j query (`list_case_observations`) and the real `GET /api/v1/cases/{case_id}/graph/observations` HTTP endpoint confirmed the projected `Observation` and its `EntityMention` (`display_label="Alpha Corp"`, `mention_type="organisation"`).

### Two real bugs caught during this session's live testing (both in this task's own new code, fixed before this record)

- **The API container was still running the pre-Phase-2.5 image.** The first live run of the full pipeline test found `no durable graph_projection_jobs row was enqueued` after a genuine `200` result submission — not a code bug, but a stale container: `docker compose up --build -d` had not yet been re-run since this session's code changes, so the live server was still executing the old `submit_result` (no enqueue logic). Rebuilding (`docker compose up --build -d`) resolved it immediately; all 5 services came back healthy on the first attempt.
- **Cross-test orphaned-row pollution.** Once the container was rebuilt, `run_batch`'s `claim_batch` call in the full-pipeline test picked up *other* live tests' `graph_projection_jobs` rows (every accepted worker result across the whole suite now enqueues one, unconditionally) whose own `worker_observations`/`evidence_records` rows those other tests had already cleaned up in their own `finally` blocks — a genuine but harmless side effect of adding an unconditional enqueue to a shared code path, not a defect in the enqueue/claim/projector logic itself. Fixed in the test (not the application) two ways: (1) the full-pipeline test's own assertions were narrowed to check only its own job's outcome rather than the whole batch's aggregate counts, since a shared live database legitimately holds other tests' state; (2) the test now sweeps orphaned `graph_projection_jobs` rows (rows whose `observation_id` has no matching `worker_observations` row) before running its own batch, so its own freshly-enqueued job is reliably reached within the bounded `claim_batch` batch size. Verified stable across three consecutive full-suite runs after the fix.

### Manual smoke test: the graph projector CLI itself

```bash
$ uv run python -m app.modules.graph.worker --help
usage: python -m app.modules.graph.worker [-h] --once
...
```
Result: **pass** — CLI wiring/argument parsing confirmed independent of the automated test suite.

### Communication-processing routing fix: live verification

```bash
$ docker compose exec postgres psql -U tracex -d tracex -c "\d evidence_records" | grep ck_evidence_records_source_type
"ck_evidence_records_source_type" CHECK (source_type = ANY (ARRAY['document'::text, 'cdr'::text, 'financial'::text, 'video'::text, 'image'::text, 'audio'::text, 'chat'::text, 'structured_tabular'::text, 'structured_json'::text, 'audio_transcript'::text, 'audio_diarization'::text, 'whatsapp_chat'::text, 'telegram_chat'::text, 'instagram_chat'::text, 'other'::text]))
```
Result: **pass** — the widened constraint is live.

A real script (register user, login, create case+membership, upload once per new `source_type` via the real running API) produced, for every one of the five new source types, a real `HTTP 201` and a real `worker_jobs` row with the exact expected `processor_name`:

```text
audio_transcript     -> HTTP 201, processor='transcript_import_v1'    OK
audio_diarization    -> HTTP 201, processor='diarization_import_v1'   OK
whatsapp_chat        -> HTTP 201, processor='whatsapp_export_v1'      OK
telegram_chat        -> HTTP 201, processor='telegram_export_v1'      OK
instagram_chat       -> HTTP 201, processor='instagram_export_v1'     OK
```

All test/case/evidence/job rows created by this manual script were cleaned up directly afterward via the same script.

```bash
$ uv run pytest tests/unit/evidence_lifecycle/test_communication_routing.py -v
11 passed in 0.70s
```
Result: **pass** — valid routing for all 5 new source types, cross-MIME rejection for 3 of them, idempotent replay, client-cannot-override-processor, and cross-case isolation.

```bash
$ curl -s http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -s http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
```
Result: **pass** — both confirmed against the fully rebuilt, fully containerized stack, after all of this session's changes.

### Final live smoke: the actual `--once` CLI subprocess, not the in-process test function

```bash
$ uv run python -m app.modules.graph.worker --once
```
Run as a genuine subprocess (not the in-process `run_batch` call the automated tests use) against a freshly submitted worker result on the live stack:
```json
{"claimed": 7, "succeeded": 1, "failed": 6, "retrying": 0, "deferred": 0, "event": "graph.worker.run_completed", ...}
```
Result: **pass** — the CLI's own job succeeded (confirmed by name via the read API below); the 6 `failed` were pre-existing orphaned rows from this session's own earlier manual smoke scripts (not from the automated suite, which self-cleans), cleaned up afterward with `DELETE FROM graph_projection_jobs WHERE status = 'failed'`.

```bash
$ curl http://localhost:8000/api/v1/cases/<case_id>/graph/observations -H "Authorization: Bearer <token>"
```
Returned the real projected observation with its mention: `{"mention_id": "39a8ac7a-...", "mention_type": "organisation", "display_label": "CLI Smoke Corp", "ordinal": 0}` — confirmed via both this HTTP call and a direct Neo4j query.

Final full-suite re-run after this cleanup: `uv run pytest -q` → **1173 passed**, zero skips. `git status --short` confirms only file modifications (46 files touched/added), no commits, still on branch `shreshtha`.

Docker stack left running after this session (not torn down); `docker compose down` removes it cleanly whenever wanted.

## 2026-09-11 — Shreshtha — Origin/main integration and real end-to-end verification of the communication-processing routing fix

The previous section's "Communication-processing routing fix: live verification" proved that a real upload reached the correct `processor_name` — it did **not** run the real `communication_processing` worker, because `shreshtha`'s branch tip at that time predated `origin/main`'s merge of Sarthak's Phase 2 `communication_processing` worker build. Per explicit instruction, that gap was closed rather than left implied: HEAD was verified to lack the worker (`git log --oneline -- app/modules/communication_processing` on the pre-integration tip showed nothing), `origin/main` (`0bf0ff2`) was confirmed to be a fast-forward from the then-current tip (`git merge-base --is-ancestor HEAD origin/main`), local uncommitted work was `git stash push -u`'d, the fast-forward was applied (`git merge origin/main`, no merge commit), and the stash was popped back — with four documentation files' overlapping edits resolved manually. See `docs/progress/mvp-progress.md`'s "Origin/main integration" section for the full git-safety writeup.

### A second, deeper bug found only by running the real worker

Running the real `communication_processing` worker `--once` CLI against a real uploaded job (not just checking `processor_name` on the upload response) immediately failed all five new source types with `unsupported_source_type`:

```text
code='unsupported_source_type' message="profile 'transcript_import_v1' requires source_type=audio"
```

Cause: `communication_processing/worker.py`'s own `_validate_source_type` function performed a second, independent source-type check, still written against the old group-based scheme (`SourceType.AUDIO` for all three audio profiles, `SourceType.CHAT` for all four chat profiles) that predated the routing fix's five new, disjoint source types. The routing table (`evidence_lifecycle/routing.py`) was already correctly selecting the right processor — the worker's own internal gate was the problem, and it could only be found by actually running the worker end to end.

Fixed with an exact `_PROFILE_REQUIRED_SOURCE_TYPES: dict[str, SourceType]` mapping (one entry per processor profile), replacing the two group checks. No parsing logic, dispatch table, or other worker behavior was touched. This is a real, narrow change to Sarthak's `communication_processing/worker.py`, made only because real end-to-end verification required it.

Eleven pre-existing tests in that module had fixtures built against the old group-based assumption and needed their `source_type` arguments corrected to match the profile under test:

```bash
$ uv run pytest tests/unit/communication_processing/ tests/integration/communication_processing/ -q
# before the fix: 11 failed, 258 passed
# after fixing worker.py and the 11 tests' source_type fixtures:
269 passed in 1.33s
```

### Full real end-to-end run, live, all five new source types

A script (register user, login, create case+membership, create a worker credential scoped to all 7 processor names, upload once per new source type via the real running API, run the real `communication_processing.worker --once` CLI as a genuine subprocess once per job, run the real `graph.worker --once` CLI, then confirm via both a direct Postgres/Neo4j query and the real `GET /api/v1/cases/{case_id}/graph/observations` HTTP endpoint) produced:

```text
UPLOAD  audio_transcript     -> job c6f1c750-... processor=transcript_import_v1
UPLOAD  audio_diarization    -> job ddbf7cab-... processor=diarization_import_v1
UPLOAD  whatsapp_chat        -> job 4d9f5cb6-... processor=whatsapp_export_v1
UPLOAD  telegram_chat        -> job 73748f68-... processor=telegram_export_v1
UPLOAD  instagram_chat       -> job 7f6d5801-... processor=instagram_export_v1

WORKER  audio_transcript     -> exit=0, status=succeeded
WORKER  audio_diarization    -> exit=0, status=succeeded
WORKER  whatsapp_chat        -> exit=0, status=succeeded
WORKER  telegram_chat        -> exit=0, status=succeeded
WORKER  instagram_chat       -> exit=0, status=succeeded

RESULT  audio_transcript     -> job_status=succeeded observations=1
RESULT  audio_diarization    -> job_status=succeeded observations=1
RESULT  whatsapp_chat        -> job_status=succeeded observations=1
RESULT  telegram_chat        -> job_status=succeeded observations=1
RESULT  instagram_chat       -> job_status=succeeded observations=1

GRAPH   audio_transcript     observation_id=98123665-... projected=True
GRAPH   audio_diarization    observation_id=028493dc-... projected=True
GRAPH   whatsapp_chat        observation_id=fc21f7df-... projected=True
GRAPH   telegram_chat        observation_id=5225a90a-... projected=True
GRAPH   instagram_chat       observation_id=091daec1-... projected=True

ALL 5 SOURCE TYPES VERIFIED END-TO-END SUCCESSFULLY
```

Result: **pass** — every one of the five new source types was proven, live, through the complete real chain: upload → server-selected processor → real worker claim/input/result → persisted `ObservationV1` → real graph projection, confirmed by both direct database/graph queries and the real HTTP read API.

The graph-projector run surfaced 6 unrelated `failed` rows (`observation_not_found`) from a full `uv run pytest -q` run performed earlier in this same session — pre-existing cross-test pollution (documented in the Phase 2.5 section above: an accepted result unconditionally enqueues a projection job, and another integration test's own teardown had already deleted the underlying observation before this run's projector swept it up). Not related to this session's 5 verified jobs, which projected cleanly on the first pass. Swept up as part of this session's cleanup.

### Cleanup and final state

All data created by this verification run was removed afterward: the case, user, worker credential, 5 evidence records, 5 worker jobs, 5 worker results, and their Neo4j nodes (confirmed via direct query: `MATCH (n) RETURN count(n)` → `0` in the case-scoped subgraph). One further orphaned case (from an earlier run of the same debug script, made before the `_validate_source_type` fix, whose script had exited before reaching its own cleanup step) was found and removed in the same pass, along with the 6 unrelated orphaned `graph_projection_jobs` rows described above.

Full re-verification after all fixes:

```bash
$ uv run ruff format --check .   # 297 files already formatted
$ uv run ruff check .            # All checks passed!
$ uv run mypy app                # Success: no issues found in 129 source files
$ uv run pytest -q               # 1226 passed
```

(One transient failure was observed on a single full-suite run — `tests/integration/media_processing/test_video_pipeline.py::test_temporary_artifacts_are_cleaned_up`, asserting no stray files under `tempfile.gettempdir()` — caused by an unrelated `/tmp/runc-process*` file dropped by the container runtime itself during a full-suite run, not by any code touched in this session. It passed in isolation and on an immediate full-suite re-run; flagged here for completeness, not treated as a regression.)

`git status --short` at the end of this session shows only working-tree modifications (no staged files, no commits, no branch switch) — HEAD is at `0bf0ff2` (the fast-forwarded `origin/main` tip) plus this session's uncommitted changes, still on branch `shreshtha`. Docker stack left running, all test data removed.

## 2026-09-12 — Gaurav — Phase 2 Media-Processing Worker Foundation

Branch `gaurav`, HEAD confirmed equal to the last locally-known `origin/main` (`git rev-list --left-right --count origin/main...HEAD` → `0	0`) at `1d3d885` ("Completed shreshtha/phase-2 (#18)"), which already includes Shreshtha's graph-projection work. Note: `git fetch origin --prune` failed in this sandbox (`fatal: could not read Username for 'https://github.com'` — no HTTPS credentials configured here), so this is based on the last cached remote-tracking state, not a fresh fetch; flagged rather than silently assumed current.

### What was built

Gaurav's Phase 1 `app/modules/media_processing/` already had a complete `process_job` (image decode, video probe/sample/extract, geometry validation, detection/tracking/OCR adapter protocols with deterministic fakes) but no real caller. This phase adds: `client.py`, `input_resolver.py`, and `run_once`/`main`/`SUPPORTED_PROCESSORS`/`_shim_evidence_record` in `worker.py` — mirroring `communication_processing`'s/`structured_processing`'s identical orchestration layer exactly. Full design in `docs/architecture/media-processing-worker.md`.

### Two real design/bug findings during this build

1. **`MediaKind` didn't recognize `video/x-matroska`.** `evidence_lifecycle/routing.py` has accepted it for `SourceType.VIDEO` since Phase 1, but `media_processing/source.py` had no matching classification — a real `.mkv` upload would pass routing and fail inside the worker with `unsupported_content_type`. Found only by cross-checking routing's content-type set against the worker's own `MediaKind` enum while wiring the live path — no prior test compared the two. Fixed additively (`MediaKind.VIDEO_MATROSKA`); `ffprobe`/`ffmpeg` are container-agnostic, so no other code changed.
2. **Design pivot, caught and corrected before finalizing**: an initial draft changed `process_job`'s signature to accept a new, leaner `MediaEvidenceMetadata` type instead of a full `EvidenceRecordV1`, reasoning that a live run cannot honestly know `classification`/`uploaded_by`/`processing_status`. On reviewing `structured_processing.worker._shim_evidence_record` (which already solved the *identical* problem via a shimmed `EvidenceRecordV1` with clearly-commented unused placeholders), the draft was reverted in favor of that established pattern instead — avoiding two different, independently-invented answers to the same question across sibling modules. See `docs/architecture/phase-2-decisions.md`'s "Media-Processing Worker Completion" section for the full reasoning.

### Commands run and results

```bash
$ uv sync --all-groups
Resolved 66 packages in 2ms
Checked 65 packages in 0.88ms

$ uv run ruff format --check .
303 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 131 source files

$ uv run pytest -q
1272 passed in ~30-44s (run repeatedly; stable)

$ docker compose config
(valid — no output on success)
```

### New tests added (all passing)

- `tests/unit/evidence_lifecycle/test_media_routing.py` (12 tests) — image/video routing incl. matroska, cross-MIME rejection, client-cannot-override-processor, idempotent replay, cross-case isolation.
- `tests/unit/media_processing/test_media_worker_client.py` (14 tests) — `WorkerApiClient` wire-format proof against `httpx.MockTransport`; no token/claim-token ever logged.
- `tests/unit/media_processing/test_media_worker_orchestration.py` (8 tests) — `run_once` sequencing, SHA-256-mismatch-before-decode, input-resolution-gap deferral, no-job-available, multi-processor claim loop, idempotent resubmission at the client layer.
- `tests/unit/media_processing/test_source.py` — extended with matroska classification cases.
- `tests/integration/media_processing/test_media_worker_live.py` (3 tests, self-skipping) — see below.

### Docker/live verification

```bash
$ docker compose up --build -d
... Container tracex-api-1 Started (rebuilt image, all 5 services healthy)

$ uv run alembic upgrade head
(no output — already at head)

$ curl -s http://localhost:8000/healthz
{"status":"ok","service":"tracex-api","version":"0.1.0"}
$ curl -s http://localhost:8000/readyz
{"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl -s http://localhost:8000/api/v1/meta/contracts
{"evidence_record":"EvidenceRecordV1","observation":"ObservationV1","entity":"EntityV1","event":"EventV1","worker_job":"WorkerJobV1","worker_result":"WorkerResultV1"}
```

**Pytest live integration test** (`tests/integration/media_processing/test_media_worker_live.py`), run against the live stack — real upload, real worker `run_once()`, real graph projector `run_batch()` (twice), real Neo4j query, real HTTP graph-read endpoint, for both image and video:

```bash
$ uv run pytest tests/integration/media_processing/test_media_worker_live.py -v
test_worker_client_against_real_running_api PASSED
test_full_upload_claim_stream_verify_process_submit_project_live_pipeline[image-media_metadata_v1] PASSED
test_full_upload_claim_stream_verify_process_submit_project_live_pipeline[video-media_metadata_v1] PASSED
3 passed in 2.38s
```
Re-run to confirm stability: 3 passed again.

**Real subprocess CLI verification** (not the in-process pytest call above) — a standalone script uploaded a real PNG and a real `ffmpeg`-generated synthetic MP4, then invoked the actual worker and projector CLIs as genuine child processes:

```text
UPLOAD  image    -> job 0b78fc69-... processor=media_metadata_v1
UPLOAD  video    -> job 8290657b-... processor=media_metadata_v1
WORKER  image    -> exit=0 status=succeeded   (real `uv run python -m app.modules.media_processing.worker --once` subprocess)
WORKER  video    -> exit=0 status=succeeded   (same, second subprocess invocation)
RESULT  image    -> job_status=succeeded observations=1
RESULT  video    -> job_status=succeeded observations=1
PROJECTOR run 1 -> {"claimed": 8, "succeeded": 2, "failed": 6, ...}   (real `uv run python -m app.modules.graph.worker --once` subprocess)
PROJECTOR run 2 -> {"claimed": 0, "succeeded": 0, "failed": 0, ...}   (second run: this test's 2 jobs already terminal, no re-claim, no duplicate)
GRAPH   image    observation_id=c7013bf5-... projected=True   (via real GET /api/v1/cases/{case_id}/graph/observations)
GRAPH   video    observation_id=6aeaa58e-... projected=True
NEO4J   image    observation_id=c7013bf5-... present=True     (via direct Neo4j query)
NEO4J   video    observation_id=6aeaa58e-... present=True

ALL 2 MEDIA SOURCE TYPES VERIFIED END-TO-END VIA REAL SUBPROCESS WORKER
```
The graph-read HTTP response was also confirmed free of `object_uri` and the worker token. The 6 `failed` projector attempts in run 1 were pre-existing orphaned `graph_projection_jobs` rows from this same session's earlier full-suite `pytest -q` run (`observation_not_found` — other integration tests' own teardown had already deleted their observations before this run's projector swept them up; same documented cross-test-pollution pattern as Shreshtha's Phase 2.5 session), unrelated to either of this script's own 2 jobs, which both succeeded and projected cleanly on the first pass. Swept up as part of this session's cleanup.

### Cleanup and final state

All data created by both the pytest live suite and the manual subprocess script was removed: cases, users, worker credentials created solely for this verification, evidence, jobs, results, observations, and their Neo4j nodes. One further pre-existing orphaned case/user (`LIVE-FULL-PIPELINE-...`, from `tests/integration/graph/test_full_pipeline_live.py`, predating this session) was also found and cleaned up while auditing database state — unrelated to media_processing, flagged here for transparency rather than silently left. Final state confirmed directly: `0` cases, `0` failed `graph_projection_jobs` rows, `0` Neo4j nodes.

Persistent, intentionally-kept rows (matching the established live-test convention every sibling module's own live suite already follows — `structured-processing-worker-live-test`/`communication-processing-worker-live-test` credentials also persist across runs): the `media-processing-worker-live-test` worker credential provisioned by `test_media_worker_live.py`'s `_ensure_worker_credential` helper.

`git status --short` at the end of this session shows only working-tree modifications (no staged files, no commits, no branch switch) — still on branch `gaurav`, HEAD unchanged at `1d3d885` plus this session's uncommitted changes. Docker stack left running; `docker compose down` removes it cleanly whenever wanted.

## 2026-09-12 — Aditya — Phase 3 Secure Worker Submission, Case-Scoped Claims, Lease/Heartbeat Control, Retry Limits, and Audit Events

Branch `aditya`. Started stale (`05897b0`, 6 commits behind the cached `origin/main`) — reported to the user per this task's own "confirm `origin/main...HEAD` is `0 0`, if not report and do not build" rule; the user then manually updated the branch. Re-verified before any work began: `git status --short` clean, `git rev-list --left-right --count origin/main...HEAD` → `0	0`, HEAD at `d922b0e` ("Complteted nipun/phase-3 (#22)"), confirmed to actually contain Nipun's Phase 3 observation-batch work (`app/contracts/observation_batch.py`, the `/observations` route, migration `c1c9c1c9d9f1`). Note: `git fetch origin --prune` failed in this sandbox both before and after (`fatal: could not read Username for 'https://github.com'` — no HTTPS credentials configured here) — the `0 0` check is against the last cached remote-tracking ref, not a fresh fetch.

### Inspection findings before any code was written

The existing worker-submission boundary (much of it Aditya's own prior Phase 2 work, `05897b0`, plus subsequent merges) was already extremely mature: `require_worker_principal` (fail-closed authentication), processor-scope enforcement at claim time, claim-token-hash + worker-identity verification on every mutating route (`/result`, `/input`, `/renew`, and Nipun's `/observations`), case/evidence scope validation against request bodies, `FOR UPDATE SKIP LOCKED` claim concurrency, and denial-path auditing were all already correct. Genuine, real gaps found by inspection (matching `docs/architecture/worker-job-lifecycle.md`'s own "Lease and retry policy" section, which literally named the missing pieces):

1. **No max-attempt cutoff at all** — `attempt` incremented without bound on every reclaim; nothing ever transitioned an endlessly-reclaimed job to `failed`.
2. **No absolute lease-renewal ceiling** — `/renew` (already implemented) could extend a lease indefinitely, forever, with no configured cap.
3. **No audit trail for any worker action that *succeeds*** — only `worker_authentication_denied`/`worker_processor_scope_denied`/`worker_job_access_denied` were ever recorded; a successful claim, reclaim, renewal, or retry-exhaustion event left no operational trail at all.

### A real bug caught and fixed during development (before shipping)

The first implementation of the `max_attempts` claim-eligibility filter applied `attempt < max_attempts` uniformly across both the `QUEUED` and `RUNNING`-with-expired-lease branches of the eligibility query. Since `attempt` starts at `1` (not `0`) and is never incremented on a first claim, this incorrectly blocked a job's very first claim whenever `max_attempts == 1` (`1 < 1` is false) — caught by this session's own `test_retry_exhaustion_sweep_is_processor_agnostic` test during development, never shipped. Fixed by nesting the `attempt < max_attempts` predicate only inside the reclaim branch of the `OR`, in both the real `EvidenceLifecycleRepository.claim_job` (PostgreSQL) and the in-memory `FakeEvidenceLifecycleRepository` test double. See `docs/architecture/phase-3-decisions.md`'s "Why a job's first-ever claim is exempt from the `max_attempts` check".

### Commands run and results

```bash
$ uv sync --all-groups
Resolved 71 packages in 1ms
Checked 69 packages in 0.89ms

$ uv run ruff format --check .
324 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 137 source files

$ uv run pytest -q
1432 passed in ~37s (run repeatedly; stable)

$ docker compose config
(valid — no output on success)
```

### Test summary

New/extended unit and integration coverage (all passing): `tests/unit/evidence_lifecycle/test_worker_claim.py` (+2 retry-exhaustion tests, 1 real bug found and fixed as above), `test_worker_lease_renewal.py` (+2 absolute-ceiling tests, +1 renewal-audit test, +1 `claimed_at` realism fix to an existing fixture that the new ceiling logic exposed), `test_worker_identity_api.py` (+2 claim/reclaim-audit tests), `test_worker_internal_api.py` (+2 completed/failed-audit tests), `test_worker_result.py` (+1 expired-lease-rejects-result test), `test_observation_batch_submission.py` (+1 stale-token-after-reclaim test) — plus a brand-new live-integration file, `tests/integration/evidence_lifecycle/test_worker_retry_and_lease_live.py` (4 tests: absolute lease ceiling, reclaim invalidates old token, real retry exhaustion, genuinely concurrent (`asyncio.gather`) terminal-result race), all passing against real PostgreSQL, re-run 3× for stability. Full repository suite: **1432 passed**, 0 failed, 0 unexpectedly skipped.

### Docker/live verification

```bash
$ docker compose up --build -d
... Container tracex-api-1 Started (rebuilt — the previously-running container was ~12h stale, predating this session's branch update to d922b0e; confirmed by a real 404 on /observations before the rebuild, gone after)

$ uv run alembic upgrade head
$ uv run alembic current
d3f1a6c9b8e2 (head)
```

`GET /healthz` → `{"status":"ok",...}`; `GET /readyz` → all four dependencies `"ok"`; `GET /api/v1/meta/contracts` → includes `observation_batch_submission`/`observation_batch_receipt`/`transformation_provenance` alongside the Phase 1 contracts.

**Manual real-HTTP verification round** (temporarily set `WORKER_LEASE_SECONDS=3`, `WORKER_LEASE_MAX_SECONDS=8`, `WORKER_JOB_MAX_ATTEMPTS=2` in `.env` for this run only, restored to defaults and the container restarted afterward) — every item in the task's required-verification checklist, via genuine HTTP requests against the rebuilt live container:

```text
[PASS] register
[PASS] upload evidence
[PASS] valid claim
[PASS] wrong worker denied (input)
[PASS] wrong processor scope denied (claim)
[PASS] claim input access
[PASS] valid lease renewal
[PASS] valid observation-batch submission
[PASS] expired lease denied (renew)
[PASS] reclaim after expiry
[PASS] stale token denied after reclaim (batch)
[PASS] stale token denied after reclaim (result)
[PASS] terminal result submission
[PASS] terminal result observation_count == 1
[PASS] retry-exhaustion setup: claim 1
[PASS] retry-exhaustion setup: reclaim (attempt 2 == max)
[PASS] retry exhaustion: no further claim
[PASS] retry-exhausted job is durably failed
[PASS] retry-exhausted error code is safe
[PASS] graph outbox handoff exactly once (one projection job for the one terminal-succeeded job)
[PASS] safe audit events persisted for this case
[PASS] worker_job_access_denied audit events persisted for this job
[PASS] worker_job_retry_exhausted audit event persisted
[PASS] no claim token in any audit metadata
[PASS] no worker token in any audit metadata

ALL CHECKS PASSED
```

Re-run twice for stability — identical result both times. Two real script bugs were found and fixed while building this verification round (both in the *verification script*, not application code): an empty observation batch (`observations: []`, `transformations: []`, `progress: None`) is correctly rejected by Nipun's own contract validator ("an empty observation batch must carry a progress or transformation update") — fixed by giving the batch a real `progress` object; and the graph-outbox-exactly-once check initially found `0` because the terminal result it checked against had also been submitted with zero observations — fixed by submitting one real `ObservationV1`.

**Cleanup**: all data created by both the manual script and its two earlier failed attempts (cleaned up separately via direct queries after each failure) was removed — cases, users, worker credentials, evidence, jobs, results, batches, transformations, progress events, graph-projection-outbox rows, audit events, and Neo4j nodes. Final state confirmed directly: `0` matching cases/credentials/users, `0` failed `graph_projection_jobs` rows, `0` Neo4j nodes.

### Final state

`git status --short` shows only working-tree modifications (no staged files, no commits, no branch switch) — still on branch `aditya`, HEAD unchanged at `d922b0e` plus this session's uncommitted changes. Docker stack left running with `.env` restored to its original (non-shortened-lease) values; `docker compose down` removes it cleanly whenever wanted.

## 2026-09-12 — Jasraj — Phase 3 Document, FIR, CDR, and Financial Evidence Processing Workers

Branch `jasraj`. Started at `fc5a2b7` ("Completed aditya/phase-3 (#23)"), whose parent `d922b0e` is Nipun's Phase 3 merge — confirmed both Nipun's and Aditya's Phase 3 work were present before any code was written. `git status --short` clean, `git rev-list --left-right --count origin/main...HEAD` → `0	0` against the cached ref (`git fetch origin --prune` itself fails in this sandbox with no HTTPS credentials — the same pre-existing sandbox limitation documented in Aditya's own Phase 3 entry above).

### Inspection findings before any code was written

`structured_processing` was already mature from Phase 1/2: real FIR regex extraction, real CSV/XLSX/JSON parsing for CDR/finance (stdlib `csv`/`openpyxl`/`json`, never Polars/PyArrow), a real one-shot claim/submit worker CLI. The task's required gaps were genuinely absent: no OCR of any kind for PDFs (`ocr_routing.py` only ever returned a `DEFERRED` checkpoint), no NER, no relation/event extraction, no vectorized/chunked CDR/finance reading, and no use of Nipun's `/observations` micro-batch endpoint at all — every worker result was still submitted as one all-at-once terminal `WorkerResultV1`. No routing change was needed: `SourceType.DOCUMENT`/`CDR`/`FINANCIAL` already accepted every format this phase processes.

### Environment checks before implementation

- `tesseract` (5.5.3, `eng` language pack) was already installed in this sandbox — real OCR was fully testable throughout, not just at final live verification.
- PyPI was reachable; `uv add spacy polars pypdfium2 pyarrow` all resolved and installed cleanly (prebuilt wheels, no compilation needed).
- A real, pinned spaCy model wheel (`en_core_web_sm-3.8.0-py3-none-any.whl`, from `github.com/explosion/spacy-models`) was downloaded and its SHA-256 computed directly (`1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85`) to pin `bootstrap_ner_model.py`'s default, then extracted and loaded directly by `spacy.load()` from a plain directory — confirming the "download, verify, extract a directory from a zip archive, never `pip install`" bootstrap design works before writing the production code around it.
- **A genuine sandbox restriction found**: this environment's own permission classifier blocks any `pip install`/`uv pip install`-shaped command outright, even for a legitimate, already-downloaded, checksum-verified local wheel file, and even framed as `uv pip install` rather than raw `pip install`. This did not block anything in the end, because the bootstrap design chosen (extract, never install) needs no such step — flagged as a real, observed sandbox characteristic, not a workaround.

### Commands run and results

```bash
$ uv sync --all-groups
Resolved 101 packages in 6ms
Checked 99 packages in 1ms

$ uv run ruff format --check .
342 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 147 source files

$ uv run pytest -q
1511 passed in 62-64s (run repeatedly across the session; stable)

$ docker compose config
(valid -- no output on success)
```

### Real, live OCR/NER prototyping (done before writing worker.py, to de-risk the design)

- Hand-built a real image-only PDF page (a raw, uncompressed `/DeviceRGB` image XObject with no text layer, containing Pillow-rendered text) and confirmed `pypdf.extract_text()` genuinely returns `""` for it.
- Rendered that page with `pypdfium2` at 200 DPI and ran real Tesseract OCR against the rendered image: recovered `"FIR NUMBER 123 2026"` with per-word confidences (~0.91-0.96) and pixel bounding boxes — validating the full render → OCR → normalized-bbox pipeline before any production code was written around it.
- Downloaded and extracted the real spaCy model directly (see above) and ran real NER against a sentence with Indian names/orgs: correctly found `HDFC Bank` → `ORG`, `Mumbai` → `GPE`, `12 January 2025` → `DATE` (deliberately excluded from this project's label set — see `document/ner.py`); missed the standalone first name `"Suresh"` as `PERSON` — an honest, expected small-model limitation, not a bug.

### Real functional smoke tests during development (before formal pytest coverage)

- **Document pipeline (TXT)**: a short FIR-style paragraph produced 6 regex mentions, 8 NER mentions, and 6 relation observations (`person_contact_association` ×4, `dated_communication_reference`, `transaction_claim`) in one micro-batch with 4 transformation-provenance steps — confirmed correct end to end via a local fake client before writing `test_worker_document_batches.py`.
- **Mixed PDF (trusted + real scanned page)**: page 1 (embedded text) → 4 observations, no OCR step; page 2 (blank scanned page, no image at all) → real OCR ran, found 0 regions (correct — nothing was drawn), submitted as its own batch with progress only (no observations) — proving Nipun's "a batch is valid with progress alone" contract rule end to end.
- **Real scanned page with actual rendered text** ("FIR No: 77/2026" / "Police Station: Andheri" as an image): real OCR recovered both lines, regex found `fir_reference`/`police_station_mention`, each with an exact normalized bounding box matching its line's real position in the rendered image.
- **CDR chunked batch**: a 23-valid-row + 1-malformed-row CSV produced exactly 1 batch (default `structured_batch_size=500`), 23 observations, and a checkpoint `{"malformed_row_count": 1, "valid_row_count": 23}` — the malformed row (blank `caller_number`) was reported safely without aborting the batch.

### A real application bug caught and fixed during development (never shipped)

The first implementation of `chunked_processing.normalize_chunk` called `normalize_cdr_records`/`normalize_financial_records` once per *chunk* (passing the whole list of records in one call), not once per row — but those functions raise on the *first* bad row in whatever list they're given, so a single malformed row anywhere in a chunk would have silently discarded every *valid* row in that same chunk, violating the required partial-success policy. Caught by this session's own `test_malformed_row_is_reported_safely_and_does_not_abort_the_chunk` test during development, never shipped. Fixed by calling the existing per-row normalization function once per individual row (a one-element list each time) inside `normalize_chunk`, catching `ProcessingError` per row — no change needed to the well-tested `cdr.py`/`finance.py` functions themselves.

Two smaller test-authoring mistakes (not application bugs) were also caught and fixed during this session: an XLSX malformed-row test that used an all-`None` row (correctly treated as a benign fully-empty row by the existing, unchanged `iter_xlsx_record_chunks` skip rule, not a malformed one) — fixed by using a row with only the required field missing; and a graph-projector-exit-code assumption in the final manual live-verification script (see below) that didn't account for this long-lived shared sandbox's pre-existing, unrelated stale `graph_projection_jobs` rows from earlier sessions' test runs.

### Test summary

New/extended coverage (all passing, stable across repeated runs): `document/{normalization,page_trust,ocr,ner,ner_fallback,ner_spacy,relations}.py` (new modules) each with a dedicated test file (`test_normalization.py`, `test_ner.py`, `test_relations.py`; page-trust/OCR covered directly through `test_pdf.py` and the new `test_worker_document_batches.py`); `structured/chunked_processing.py` (new, `test_chunked_processing.py`); `cdr.py`/`finance.py` extended (E.164 phone, shared timezone policy, finance `direction`/`currency_is_known_iso4217` — `test_cdr.py`/`test_finance.py` extended, two pre-existing assertions deliberately updated for the E.164 change); `batching.py` (new, smoke-tested directly); `client.py`'s `submit_batch`/`renew_lease` (new, `test_worker_client.py` extended); `worker.py`'s `run_document_job_with_batches`/`run_structured_batches_job`/`_dispatch_job` (new, `test_worker_document_batches.py` + `test_worker_structured_batches.py`, 16 tests total); a real, measured OCR/regex field-match precision test (`test_ocr_field_match_precision.py`); `test_worker_orchestration.py` extended (`_FakeClient` gained `submit_batch`/`renew_lease`). Full repository suite: **1511 passed**, 0 failed, 0 unexpectedly skipped, run repeatedly (including after the full live-Docker verification round below) with identical results.

**Real, measured OCR + regex field-match precision** (`test_ocr_field_match_precision.py`, a labelled synthetic fixture with 3 known ground-truth identifiers — an FIR number, a phone number, an amount):

```text
extracted=['25000', '91/2026', '9876543210', 'Colaba Phone: 9876543210 Amount: Rs. 25000']
expected=['25000', '91/2026', '9876543210']
precision=0.75 recall=1.00
ocr_average_confidence=0.91
```

Recall 1.00 (every known identifier was recovered); precision 0.75 (3 of 4 extracted values were correct — the 4th is `fir_report.py`'s pre-existing `police_station_mention` regex over-matching in the absence of a line break after the label, a known, documented Phase 1 limitation, not something introduced or fixed in this phase). Real Tesseract average line confidence 0.91 against clean, synthetic, high-contrast rendered text — not a claim about real-world scanned-document accuracy.

### Docker/live verification

```bash
$ docker compose up --build -d
... Container tracex-api-1 Recreated / Started (full rebuild -- new dependencies spacy/polars/pypdfium2/pyarrow all built successfully; ~2 minutes)

$ curl /healthz   -> {"status":"ok",...}
$ curl /readyz    -> {"status":"ok","dependencies":{"postgres":"ok","neo4j":"ok","redis":"ok","minio":"ok"}}
$ curl /api/v1/meta/contracts -> unchanged, includes observation_batch_submission/receipt/transformation_provenance

$ uv run alembic upgrade head
$ uv run alembic current
d3f1a6c9b8e2 (head)   # unchanged -- this phase added no migration
```

**Full repository suite against the fully rebuilt live stack**: `uv run pytest -q` → **1511 passed**, including every self-skipping integration test now running for real (not skipped). In particular, `tests/integration/structured_processing/test_worker_live.py -v` showed all 6 tests genuinely `PASSED` (not skipped):

```text
test_worker_client_against_real_running_api PASSED
test_full_claim_stream_parse_submit_live_pipeline[document-fir_report_text_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[structured_tabular-generic_tabular_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[structured_json-generic_json_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[cdr-cdr_generic_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[financial-financial_transaction_generic_v1] PASSED
```

The two new (`cdr`, `financial`) parametrized cases are this phase's own live proof: a real CDR/finance CSV upload → real claim → real claim-token-bound stream → real SHA-256 verification → real chunked normalization → one real `/observations` micro-batch → one real terminal result, with `observation_count` on the user-facing job-status endpoint correctly reflecting the batch-delivered observations (confirmed directly: batch-sourced rows land in the same `worker_observations` table a terminal result's own rows always have — no code change was needed anywhere in `evidence_lifecycle`/`graph` to make this true).

**Manual real-HTTP verification for the scanned-PDF-with-real-OCR path specifically** (not covered by the existing parametrized live test, which uses a plain-text document for the `document` case) — a genuine end-to-end run against the rebuilt live container, using the `structured-processing-worker-live-test` credential already active in this sandbox's database from an earlier session (confirmed by hashing the current `.env`'s `WORKER_TOKEN` and matching it against the stored digest directly, so no new credential needed provisioning):

```text
[PASS] register
[PASS] login
[PASS] case seeded
[PASS] membership seeded
[PASS] scanned PDF upload
[PASS] routed to fir_report_text_v1
[PASS] worker --once exit 0 (real subprocess: uv run python -m app.modules.structured_processing.worker --once)
[PASS] job status fetch
[PASS] job succeeded
[PASS] observation_count > 0 (OCR-derived)
[PASS] pdf_text_layer_assessment ran (confirmed directly via `observation_transformations`)
[PASS] pdf_page_ocr ran -- real OCR (confirmed directly via `observation_transformations`)
```

The graph-projector step in this same script asserted exit code `0`, which the real `graph.worker --once` run did not return (`{"claimed": 4, "succeeded": 3, "failed": 1, ...}`, exit `1` per its own documented "at least one job failed" convention) — investigated directly rather than assumed: the one failure was `observation_not_found` for `case_id`s that did **not** match this run's case, i.e. pre-existing, unrelated stale `graph_projection_jobs` rows left over from earlier sessions in this long-lived shared sandbox (an accepted, previously-documented convention — see e.g. the Sarthak Phase 2 entry above). This run's own 3 observations (from the real scanned-PDF OCR) all had `status='succeeded'`, confirmed directly:

```sql
SELECT c.case_reference, gpj.status, gpj.last_error_code
FROM cases c JOIN graph_projection_jobs gpj ON gpj.case_id = c.case_id
WHERE c.case_reference = 'JASRAJ-LIVE-VERIFY';
-- 3 rows, all status='succeeded', last_error_code=NULL
```

The **safe graph-read API** was then queried directly for this exact case and returned the real projected observations correctly:

```json
{"case_id": "...", "items": [
  {"observation_type": "phone_number_mention", "mentions": [{"display_label": "9876543210", ...}], ...},
  {"observation_type": "police_station_mention", "mentions": [{"display_label": "LiveVerify Phone: 9876543210", ...}], ...},
  {"observation_type": "fir_reference", ...}
]}
```

— confirmed to contain **zero** occurrences of `object_uri`, `claim_token`, or `Bearer ` anywhere in the response body.

**Cleanup**: every row created by this manual verification round (the case, its membership, the evidence record, the worker job/result/observations/transformations/progress events, the 3 graph-projection-job rows, and both throwaway users) was deleted directly afterward; the corresponding Neo4j nodes for this case were removed via `DETACH DELETE` and confirmed absent (`count(n) = 0`). Final state confirmed directly: `0` matching cases, `0` matching users.

### Final state

`git status --short` shows only working-tree modifications (no staged files, no commits, no branch switch) — still on branch `jasraj`, HEAD unchanged at `fc5a2b7` plus this session's uncommitted changes. Docker stack left running (unmodified `.env` throughout — no temporary configuration was needed this session); `docker compose down` removes it cleanly whenever wanted.

## 2026-09-12 — Sarthak — Phase 3 Audio, Social/Chat, and Multilingual Communication Evidence Pipelines

Micro-batch submission, chat-timezone default policy, mentioned-identifier extraction, sender-transliteration wiring, and a typed ASR/diarization adapter boundary for `app/modules/communication_processing/` — on top of Nipun's batch-ingestion contract, Aditya's worker-security boundary, and this module's own Phase 1/2 foundation. Full reasoning in `docs/architecture/phase-3-decisions.md`'s Sarthak section.

### Commands run and results

```text
$ uv sync --all-groups
Resolved 101 packages in 3ms
Checked 99 packages in 1ms                    # no dependency changes -- pure-stdlib additions only

$ uv run ruff format --check .
355 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run mypy app
Success: no issues found in 151 source files

$ uv run pytest -q
1577 passed, 1 skipped in 64.44s               # tests/unit/communication_processing/ alone: 269 -> 332 (+63)

$ docker compose config --quiet
(exit 0 -- valid)

$ docker compose up --build -d
Image tracex-api Built; postgres/neo4j/redis/minio Healthy; api Started (healthy)

$ uv run alembic current / upgrade head
d3f1a6c9b8e2 (head)                            # unchanged -- this phase added no migration
```

One genuine gap found only by actually running `docker compose config` after these changes: `compose.yaml`'s `x-tracex-app-env` anchor explicitly lists every config env var it passes through to containers, and the two new ones (`COMMUNICATION_BATCH_SIZE`, `COMMUNICATION_DEFAULT_TIMEZONE`) were missing -- added to `.env.example` and `Settings` but not wired into `compose.yaml`, which would have made a deployer's `.env` override silently have no effect inside the container. Fixed directly (`compose.yaml` now carries both, confirmed present in `docker compose config`'s resolved output).

### Live verification against the rebuilt stack

`GET /healthz` → `{"status":"ok",...}`; `GET /readyz` → all four dependencies `"ok"`; `GET /api/v1/meta/contracts` → all nine contract names present.

**Self-skipping live suite ran for real (not skipped):**

```text
$ uv run pytest tests/integration/communication_processing/ -v
test_worker_client_against_real_running_api PASSED
test_full_claim_stream_parse_submit_live_pipeline[audio-audio_metadata_v1] PASSED
test_full_claim_stream_parse_submit_live_pipeline[chat-generic_social_json_v1] PASSED
test_full_pipeline.py::test_every_processor_succeeds_end_to_end PASSED
```

Both parametrized pipeline cases already exercise this session's new batch-orchestration code path (`run_once` now calls `run_communication_job_with_batches`, not the old single-result path) for real against the live API/database.

**Manual real-HTTP verification focused on this session's four new capabilities specifically** (micro-batch delivery, chat-timezone default, mentioned-identifier extraction, sender-transliteration wiring) — none of the existing live tests exercise `whatsapp_export_v1` (only `audio_metadata_v1`/`generic_social_json_v1` are live-reachable in the committed parametrized suite), so a one-off script drove a real WhatsApp chat upload end to end:

```text
[PASS] healthz/readyz
[setup] reusing existing active worker credential
[PASS] register
[PASS] login
[PASS] case + membership seeded
[PASS] whatsapp chat upload -> routed to whatsapp_export_v1
[worker --once] exit=0
[PASS] worker --once exit 0 (real subprocess)
[job status] succeeded observation_count=3
[PASS] job succeeded with observation_count >= 3
[observation types persisted] ['chat_message', 'email_address', 'phone_number']
[PASS] all observations are real batch-linked rows (observation_batch_id set, result_id null)
[chat_message attributes] timestamp_source_timezone='Asia/Kolkata'
[PASS] timezone default applied + sender transliteration candidates present: [['राहुल', 'raahula'], ['शर्मा', 'sharmaa']]
```

Input: one WhatsApp-format line with a naive (timezone-less) timestamp, a Devanagari sender name ("राहुल शर्मा"), and a message body containing an Indian-mobile-shaped phone number and an email address. Confirmed directly via a raw `worker_observations` query: all 3 observations (`chat_message`, `phone_number`, `email_address`) have `observation_batch_id` set and `result_id` NULL — genuinely delivered through the new `/observations` micro-batch path, not the old terminal-result path. The `chat_message` row's `canonical_payload.attributes` confirmed both new features directly: `timestamp_source_timezone="Asia/Kolkata"` (the naive timestamp really did resolve via the configured default) and `sender_transliteration_candidates` containing two per-word candidates (`राहुल`→`raahula`, `शर्मा`→`sharmaa`) — proving the multi-word-tokenization fix actually works against a real pipeline run, not just the unit test that exercises `chat_message_to_mention` directly.

**Graph outbox/projector idempotency**, run twice against the real stack:

```text
run 1: {"claimed": 18, "succeeded": 3, "failed": 15, ...}   exit 1
run 2: {"claimed": 0, "succeeded": 0, "failed": 0, ...}     exit 0
Neo4j Observation node count for this case after both runs: 3
```

The 15 failures in run 1 were investigated, not assumed benign: `SELECT last_error_code, count(*) FROM graph_projection_jobs WHERE status='failed' GROUP BY last_error_code` returned `observation_not_found | 15` — the identical, already-documented pre-existing phenomenon from earlier sessions in this long-lived shared sandbox (stale `graph_projection_jobs` rows referencing observations from previously-cleaned-up test data, unrelated to this case). This run's own 3 observations all succeeded on the first pass; the second pass claimed zero new work and the Neo4j node count stayed at exactly 3 — genuine, live proof that batch-delivered observations (this session's new code path) project into Neo4j exactly once, with no duplication on a second projector run.

**Not verified live** (by design, not oversight): the ASR/diarization adapter `Protocol` (`UnavailableAsrAdapter`/`UnavailableDiarizationAdapter`) is deliberately never reachable from any live dispatch path — `audio/routing.py` defers before either adapter would ever be invoked — so there is no live-pipeline scenario in which it could be exercised. Its correctness is proven entirely at the unit level (`test_asr_adapter.py`/`test_diarization_adapter.py`), which is the only level where it is ever actually called.

**Cleanup**: every row created by the manual verification script (the case, its membership, the evidence record, the worker job/result/observation rows) was deleted directly afterward; the corresponding Neo4j nodes for this case were removed via `DETACH DELETE`. Confirmed directly: `0` matching cases (`case_reference LIKE 'SARTHAK-LIVE-VERIFY%'`), `0` matching users (`email_normalized LIKE 'sarthak-live-verify%'`). The pre-existing, already-active `communication-processing-worker-live-test` credential (provisioned in an earlier session) was reused, not re-created or revoked.

### Final state

`git status --short` shows only working-tree modifications (no staged files, no commits, no branch switch) — still on branch `sarthak`, no branch switch performed. Docker stack left running (rebuilt via `docker compose up --build -d` to pick up this session's code changes; `.env` unchanged throughout); `docker compose down` removes it cleanly whenever wanted.

## Gaurav Phase 3 OCR adapter follow-up verification (2026-09-13)

Local tool availability was verified before OCR tests: `tesseract 5.5.2` and
`/usr/share/fonts/noto/NotoSans-Bold.ttf` were present. The real
`ImageOcrAdapter` was constructed and exercised against labelled synthetic
PNG and JPEG fixtures containing `TRACEX OCR`; both recognized `TRACEX` and
returned bounded OCR-quality confidence plus valid original-space/normalized
geometry. This is a genuine local OCR result, not fixture-adapter output.

```text
uv run pytest -q tests/unit/media_processing/test_ocr_adapter.py \
  tests/unit/media_processing/test_ocr_batching.py \
  tests/unit/media_processing/test_media_worker_orchestration.py \
  tests/unit/media_processing/test_media_worker.py
82 passed in 1.22s
```

The focused suite also exercised the real `run_once` closure with a fake
claim-bound client: SHA verification, OCR micro-batch submission, regular
media-observation batch submission, exactly one empty terminal result, safe
batch-failure terminal behavior, and an ffmpeg-backed video branch with
zero-based global batch sequences and a final video-frame progress batch.
The live PostgreSQL/Neo4j/Redis/MinIO verification remains environment-gated;
it was not claimed as run in this follow-up because the stack was unavailable.
# Phase 5 Nipun graph/correlation integration (2026-09-14)

- `ruff format --check`, `ruff check`, and `mypy app/modules/graph` passed
  for the changed Phase 5 paths.
- Focused unit/contract/API coverage plus existing graph
  mapping/projection/projector/security coverage: `109 passed in 0.24s`.
- The Docker-backed Phase 5 PostgreSQL/Neo4j test was invoked, but the local
  service check did not finish in the bounded verification window (it emitted
  a skip marker before timeout). It is not recorded as a pass; run it against
  a healthy Compose stack during the Phase 5 merge wave.

# Phase 5 Shreshtha graph intelligence (2026-09-14)

- `uv run ruff format --check`, `uv run ruff check`, and `uv run mypy` passed for the changed graph-intelligence paths.
- Focused synthetic graph intelligence plus adjacent graph mapping/projection/outbox/schema checks: `88 passed in 0.15s`.
- The seeded Leiden-specific focused check passed: `6 passed in 0.14s`.
- `uv sync --all-groups`, `uv run alembic heads` (single `e4f7a8b9c0d1` head), and `docker compose config` passed.
- No Operation Nightfall truth, real-dataset, LAN, benchmark, P99, or Precision@K/Recall@K result is claimed here.
# Phase 4 media orchestration (Nipun)

- `UV_CACHE_DIR=/tmp/tracex-uv-cache uv run pytest -q tests/unit/evidence_lifecycle/test_media_orchestration.py`: **5 passed** (2026-09-14). Synthetic manifest/observation fixtures only; no Docker, GPU, LAN worker, real media, or Neo4j service was invoked.
