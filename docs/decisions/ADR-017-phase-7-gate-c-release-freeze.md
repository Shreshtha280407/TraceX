# ADR-017: Phase 7 Gate C release-evidence and configuration freeze

Status: **accepted for this release** (2026-09-20)

## Decision

`configs/benchmarks/release-freeze.v1.json` is the authoritative Gate C
record for the `gate-b-v1` evaluation cycle. The loader in
`app/modules/evaluation/release_freeze.py` validates the file against the
exact SHA-256 hashes of Dataset Manifest V1, Model Candidate Catalog V1, and
Benchmark Metrics V1, then re-validates every dataset ID, candidate ID,
allowed pair, version, task, split, result schema, required metric, status,
and hash shape. Every manifest dataset and catalogue candidate is classified
exactly once. Unknown entries, catalogue drift, unsafe extra artifact/path
configuration, and attempts to enable unavailable, deferred, or evidence-only
candidates fail closed.

The current Gate B evidence is frozen for this release. New datasets, model
artifacts, or results may be added only through a new versioned manifest and
evaluation cycle; they must not mutate `gate-b-v1`.

## Evidence admitted to the approved hash chain

Only results with a complete, repository-verifiable dataset/configuration/
artifact/safe-result hash chain and a `complete` dataset evaluation are
`approved_evidence`:

| Dataset | Candidate | Split | Result |
| --- | --- | --- | --- |
| `ibm_amlsim` | `existing-deterministic-parsers` | `development` | succeeded |
| `fir_icdar_2023` | `paddleocr-ppocrv5-mobile` | `development` | succeeded |
| `fir_icdar_2023` | `paddleocr-ppocrv5-server` | `development` | succeeded |

The GoMask attempt is frozen as `unavailable`, including its safe-result hash,
but carries no dataset/model artifact or metrics. The documented real VIRAT
YOLO11n/YOLO11s/ByteTrack and AMI Silero VAD measurements are also retained in
the freeze with their documented aggregate metrics, recomputed configuration
hashes, dataset/model hashes, host limits, and limitations. They remain
`deferred` from release selection because the underlying manifests are still
`in_progress` and the external safe-result files' hashes are not preserved in
the repository. The AMI deterministic-diarization and pyannote attempts remain
explicitly unavailable. No missing result hash or metric was invented.

All other planned, blocked, or inaccessible datasets/candidates remain
explicitly `deferred` or `unavailable`. Raw datasets, weights, caches, local
paths, and full result artifacts remain outside Git.

## Release configuration and rollback

The enabled configuration `tracex-release-v1-baseline` contains exactly one
execution component: the existing internal, deterministic
`phase5-rules-baseline` at `phase5_preliminary_rules_v1`, bound to its canonical
configuration hash. It loads no artifact and is still a review-only baseline.
The correlation pipeline resolves that exact frozen component before its first
case-data SQL read. An unknown configuration ID, hash drift, non-baseline
candidate, or `RELEASE_CONFIGURATION_DISABLED=true` stops execution first.

`tracex-release-v1-disabled` is the explicit empty rollback target. Operators
can set `RELEASE_CONFIGURATION_DISABLED=true`; the resulting structured event
contains only the configuration ID and request ID, never case content or
configuration payloads. Re-enabling requires an explicit configuration change.

## Model-selection boundary

Gate C does **not** choose an OCR, detection, tracking, VAD, diarization, ASR,
language-ID, social-extraction, or final relationship-scoring ML winner. It
retains the transparent deterministic correlation rules baseline because no
Gate C evidence justifies replacing it. Logistic Regression, XGBoost, and
LightGBM remain deferred. Shreshtha Part 6 owns their later experiment,
calibration/comparison, and final relationship-scoring freeze.

## Security and operations consequences

- Every existing case graph/correlation/candidate/hypothesis route authorizes
  through the default-deny case dependency before route-level retrieval.
- Graph/correlation/candidate/review/hypothesis collection reads now accept a
  validated `1..200` limit and apply it in SQL before rows are loaded.
- The optional pgvector path remains internal, same-case, and bounded; no new
  vector/model endpoint was added.
- `/readyz` reports safe derived worker-control-plane, graph-projection, and
  correlation-outbox capability states. It explicitly reports external worker
  process liveness as `not_observed` rather than fabricating a heartbeat.
- Generic retries log only safe operation names, counts, delay, request ID, and
  exception type. Jitter is clamped and exhaustion is explicit.
- The correlation outbox loop now applies bounded exponential backoff and exits
  non-zero after repeated retryable batches/driver failures; durable events stay
  queued for an operator-controlled restart.

No Docker/LAN deployment, model download, new dataset evaluation, frontend,
training, or final ML selection is part of this decision.
