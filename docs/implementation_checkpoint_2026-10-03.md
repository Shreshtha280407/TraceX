# Implementation checkpoint: steps 1–5

Historical checkpoint only. The user subsequently freed disk space and
authorized completion of steps 6–12. See
[the final acceptance report](implementation_acceptance_2026-10-03.md) for
current measurements, cleanup and remaining acceptance limitations. The
reference vaults mentioned below were subsequently retired after verification;
their hash archives and reports remain. Do not treat the pause below as current.

This is the requested manual-review checkpoint for `TraceX_Tomorrow_Implementation_Plan (1).pdf`, not a claim that the entire plan or final release acceptance is complete. Work stops here before steps 6–12 pending the user's disk cleanup and instruction to continue. No commit or push was made.

## GitHub synchronization

After a fresh `git fetch --all --prune`, local `main`, `origin/main`, and `origin/claude/adoring-sagan-wfw7f5` all resolve to `e469a82a24a177288a331a79eef8fe5d6793e29c`. Ahead/behind is `0 / 0`. Claude's `e4132ed` (8,237 insertions across 70 files), followed by `e469a82` (302 insertions across 10 files), is present locally. The current implementation changes are uncommitted on top of that base.

Being synchronized does not mean those commits were free of runtime, resource or benchmark-acceptance defects.

## Status of the agreed sequence

| Step | Work delivered | Qualification |
| --- | --- | --- |
| 1. Finish checks and investigate failures | Strict failed-run recording; original DuckDB Parquet allocation failure fixed; native core-stack diagnosis; controlled reproduction of the unsafe shared-spill configuration; independent spill directories, child thread caps and retained tracebacks | The controlled shared-spill defect is demonstrated and corrected, but a full 300K rerun is still needed to certify the original native exit `-11` is resolved. |
| 2. Safe dataset cleanup | Removed five generated 300K NDJSON files, reclaiming 755,932,367 bytes (~721 MiB); retained the manifest, generator identity, seed, evaluation truth, logs and results | Existing benchmark vaults, raw evidence, receipts and prior verification artifacts were not deleted. |
| 3. Retry/recompute without duplicates | Case-authorized retry; ingestion resumes from committed receipts; additive durable analysis/request records; immutable versioned analytics refresh; auto-refresh after Geo-IP installation; explicit `recompute_analytics=true`; requests survive failure/reclaim | Existing network findings, analyst reviews and pinned confidence are preserved. Only new natural keys are appended; retained findings are historical, not silently rewritten to new scores. Old risk runs are not exposed as current after refresh. |
| 4. Profile/performance/resource safeguards | Typed fact projections, bulk SQL graph construction, SQL address-window distinct counts/value sums/prior history, bounded Arrow numeric reads, batched confidence pinning, joint worker/parse/global-array admission, read-side DuckDB caps, scratch disk reserve, child CPU/thread diagnostics | Address feature/detector logic and skewed address partitions still use bounded Python objects; global ML/embedding/risk arrays are explicitly admitted, not constant-memory. Admission estimates are conservative screens, not guarantees. |
| 5. Correctness verification | Full 100K canonical graph/features/deterministic parity; exact production ML output parity; recovery/replay/auth/null/empty/skewed/pre-epoch/refresh tests; backend and frontend checks | This is evidence against regression on the tested fixtures, not a universal guarantee of better real-world precision or a passed multi-million-row release gate. |

## Verification

The frozen in-memory reference and the isolated bounded two-worker replay agree on every compared semantic row, not just totals:

| Artifact | Rows | Canonical SHA-256 (both runs) |
| --- | ---: | --- |
| Graph nodes | 589,987 | `2a5c2dd5783d93ed8c04f043fdab98fce899794bf795b7c02097feb22d4ca1c1` |
| Graph edges | 970,260 | `76fa95cf4106d703c52ff9cc321b8f4a324c503669a215f375a59294d0f5c064` |
| Address-window features | 833,912 | `9d00b6e283ede09570cab4565a484ce363a35d3d994a9fabfb9b3838f2843254` |
| Deterministic findings | 21,876 | `673457a8fc1bab20d4da1daae165dea6ab6c003199ead3d4f41aa5320bacffc5` |
| Production ML findings | 1,223 | `f3342ba0264a211800d92aaf16343eae63ea3f4a2095313b4f45a23c2bc72d46` |

Receipt counts also match: 100,000 transactions, 146,484 inputs, 262,338 outputs, 100,000 network observations, and 25 quarantined records. Graph coverage, finding scores/ranks, feature values, raw-source checksums and evidence locators are compared. Random graph/run identifiers are normalized in the canonical graph comparison; the same-snapshot ML comparison excludes only generated finding row IDs/database timestamps and compares all other stored finding fields, including feature hashes and model/feature-contract provenance. The latest replay reused the unchanged graph; the earlier fresh HTTP import separately exercised bulk graph construction.

Production ML remains `anomaly-stack-v2`, with 100,000 transactions scored. No research model was adopted, no weaker quality gate substituted, and no calibration map refitted over a final holdout to improve a displayed number. Operational reliability and confidence honesty improved; measured production output was preserved exactly on this fixture.

Final backend regression result: **192 passed**, one dependency deprecation warning, in 118.97 seconds. The untouched baseline passed 175 tests; the additional tests exercise the new correctness and recovery contracts.

Repository-wide Ruff passed. Frontend `npm run build` (TypeScript and Vite) passed; `npm run lint` exited successfully with existing warnings. Newly introduced polling/reset/dependency warnings were addressed. `mypy` is not installed in the current environment, so a successful mypy run is not claimed. Browser E2E remains deferred.

## Measured runs and failures

| Run | Outcome | Time | Sampled worker-tree peak RSS |
| --- | --- | ---: | ---: |
| `baseline-100k-20261003-review-01` | Untouched code failed DuckDB Parquet allocation | ~84.2 s | ~2,756 MiB |
| `final-100k-w2-20261003-review-03` | Strict HTTP/worker 100K diagnostic passed | 126.108 s | 3,075 MiB |
| `postgres-100k-w2-20261003-review-01` | Dedicated PostgreSQL 100K diagnostic passed | 149.89 s | 2,992 MiB |
| `fresh-300k-w2-20261003-review-01` | Failed: native child exit `-11` | 195.928 s | 3,095 MiB |
| `fresh-300k-resume-w1-20261003-review-03` | Failed: SQLite `database or disk is full` after producing 65,279 deterministic findings | 257.566 s | 1,955 MiB |
| `checkpoint-100k-derived-w2-20261003-review-03` | Isolated derived-stage correctness replay passed | 87.613 s | 2,256 MiB |

The latest replay hard-links immutable source/fragments/graph and skips another upload/parse. Its 87.613 s is **not** an end-to-end throughput result and must not be compared directly against a complete HTTP import. It retained ~686 MiB of sampled free disk at its minimum. RSS is sampled and summed child RSS can double-count shared pages; timing/profile runs are not all matched for host load. Earlier Geo-IP-exempt diagnostic passes are not full offline acceptance.

The 300K retry hit ~200 MiB sampled free space. SQLite rollback later freed scratch space; that does not invalidate the recorded disk-full failure. The control database's read-only `quick_check` returned `ok`. New scratch admission should reject such insufficient-space attempts earlier, without deleting evidence or silently reducing computation.

Saved native core metadata for child PIDs 1327239 and 1327240 shows both failing inside DuckDB `RowMatcher::Match` → `JoinHashTable::Finalize` → `HashJoinLocalSourceState::ExternalBuild`. This localizes the failure to an external/spilling hash join, not a Python scoring exception or a missing GitHub update. The original child configuration shared a spill directory across independent DuckDB buffer managers; independent directories are now used. Core files were inspected read-only and not deleted.

The controlled diagnostic `spill-isolation-probe-20261003-review-02` ran two concurrent joins over 300,000 synthetic rows with 64 MiB per child. The shared-directory control failed reading `duckdb_temp_storage_S128K-0.tmp` (not enough bytes); both independent-directory children returned the exact expected sum **921,600,000**. It finished in 2.703 s with core dumps disabled. This demonstrates a real shared-spill collision and supports the fix; it does not by itself prove every cause of the original 300K SIGSEGV is eliminated. The preceding syntax-error probe is also retained rather than misrepresented as a valid control.

After inspection, both throwaway probe input databases were removed under the dataset-cleanup permission, reclaiming another 220,225,536 bytes (~210 MiB). Their result JSON and reproduction code remain. Removed input hashes were `cf2a37670857d7abff1199d9492ba4d3e86b70c0c0dd6a832ee905f2cfee04bb` (109,850,624 bytes) and `669dfcb9bf367e3867331a42dabde7671e3fa28df389bd287a9ad7ae82e9295d` (110,374,912 bytes). They are reproducible synthetic scratch, not case evidence.

Intermediate failed reruns were preserved too: a missing pandas path was removed from integer Arrow conversion; one correctness replay exposed a child spill-parent directory lifecycle bug, fixed with an explicit directory creation and regression test; a missing read-only SQLite URI option in the replay helper was corrected. They are not omitted from the local run history.

Parent profiles identify ordered-process waiting/IPC, feature-part copying and confidence pinning as substantial costs. Confidence pinning now projects only required columns, hoists source/calibration context once and inserts 256-row Core batches in the surrounding transaction. Its cumulative parent profile time was 2.262 s in the replay versus 7.223 s in the earlier full import; this is indicative profiling, not an isolated matched speedup measurement. Child Python work is not included in parent cProfile; new stage diagnostics separately record reaped direct-child CPU when supported.

## Local artifacts and reproduction

Detailed results are local and intentionally ignored by Git:

- `experiments/runs/parity-100k-checkpoint-20261003-review-02.json`
- `experiments/runs/ml-output-parity-checkpoint-20261003-review-01.json`
- `var/scale-run/checkpoint-100k-derived-w2-20261003-review-03/{result.json,worker.log,worker.prof,resources.ndjson}`
- `var/scale-run/spill-isolation-probe-20261003-review-02/result.json`
- The success/failure run directories named above under `var/scale-run/`.
- `datasets/review_fresh_300k_20261003/dataset_manifest.json` and `evaluation_truth.json`.

Deleted generated input is reproducible from generator v3.0.0, three parts, seed `tracex-review-development-20261003`. Its ingestion SHA-256 is `977cf3f4852e38a2567fc454c212ccea6f8110d03aa05ee12af2f85ecfe7b280`; truth SHA-256 is `40e7a9c614429f0177611a6ad6d3e217d5459321d15ff5b8ab40287ff672782f`. This was a development dataset; its already-inspected split labelled "final_holdout" is not an untouched new release holdout.

Use new output names; verification helpers refuse to overwrite existing evidence:

```bash
uv run pytest -q
uv run ruff check .
cd frontend
npm run build
npm run lint
```

For a correctness replay after obtaining sufficient scratch space, from the repository root:

```bash
uv run --extra ml python -m scripts.replay_correctness \
  var/scale-run/parity-100k-memory-w1-20261003-review-01 \
  var/scale-run/NEW-correctness-run
uv run --extra ml python scripts/canonical_parity.py \
  var/scale-run/parity-100k-memory-w1-20261003-review-01 \
  var/scale-run/NEW-correctness-run --output experiments/runs/NEW-parity.json
uv run --extra ml python -m scripts.ml_output_parity \
  var/scale-run/parity-100k-memory-w1-20261003-review-01 \
  var/scale-run/NEW-correctness-run --output experiments/runs/NEW-ml-parity.json
```

## Pause boundary and manual review

About 1.6 GiB remains free on the workspace filesystem at this checkpoint. Free substantially more space before resuming; rerun `scripts/scale_resource_screen.py` on the actual available disk rather than assuming 1.6 GiB can support 300K/1M/3M. Do not delete raw benchmark vaults or truth needed for review unless deliberately retiring that evidence.

Next, once authorized: reproduce and close the 300K native failure; run the matched 100K→300K→1M→3M scale/worker ladder; finish pre-registered model/causal/stress/scenario evaluation and two untouched independent final holdouts; adopt a candidate only if every gate beats the retained baseline; browser/E2E; offline bundle install/no-egress/Ollama checks; then the final acceptance matrix and release report. No 3M/30-minute, final-model superiority or whole-appliance offline-acceptance claim is made here. The available presentation PDF is not treated as a verified official problem-statement source.

Prior research and appliance implementation edits are also still present in the worktree from the earlier authorized work, but their remaining release-level verification is paused. The cached appliance image predates the latest edits and is not the final verified image. Root `.env` was left untouched; the essential local appliance `.env` is permission-restricted and ignored, with only its non-secret `.env.example` eligible for review/commit. Never include the real `.env`, local databases, generated datasets or images in the commit.

All code and this report remain for the user's manual review. No staging, commit or push is required or performed by this checkpoint.
