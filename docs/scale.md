# Scaling TraceX to millions of rows

Current acceptance host: Linux x86_64, 15.2 GiB physical RAM, 12 logical CPUs
in process affinity; available RAM varies with desktop applications. Both
SQLite and PostgreSQL must be measured independently. Historical timings are
not acceptance for this integrated implementation. The reviewed October 3
release was committed as d88af37; its measurements remain historical. Current
MacBook 32 GB/1 TB procedures are in [macbook_runbook.md](macbook_runbook.md).
Fresh 3M/<1800s all-stage acceptance is NOT RUN. No LLM is in the current release.

## What changed to make multi-million-row imports work

| Bottleneck at 1M+ rows | Fix |
| --- | --- |
| The graph builder, deterministic findings and ML stage held every fact of the snapshot as Python objects (the 1M run was OOM-killed at ~10 GiB in Phase 6). | `app/resources.py` estimates the in-memory working set per import from its record count and the RAM this machine (or container) can actually spare. Imports that fit run fully in memory (fastest); the rest run in the **bounded** mode (`app/engine/bounded.py`): facts are staged in an on-disk DuckDB database, set-wise work is SQL that spills to disk, and per-row detector logic runs over one chunk / address partition at a time. Output is byte-identical to the in-memory path (graph, every feature row, every finding — checked by hash at 100K under 3 budgets). |
| ~8 address-window feature rows per transaction were inserted into the control database as JSON (834K rows = 3.9 GB of SQLite at 100K; ~25M rows / >100 GB at 3M). | Feature rows are written to one zstd-compressed Parquet file per snapshot (`app/engine/feature_store.py`, recorded with its SHA-256 in `feature_stores`). Same rows, a fraction of the space, no row-by-row inserts: the 100K end-to-end import dropped from 163 s to 111 s. Export streams JSON, pages it, or serves the Parquet file. |
| Duplicate-txid detection kept hex strings for every txid and variant. | Binary txid keys and 16-byte variant digests (about half the memory). |
| Entity clustering, Geo-IP enrichment, network correlation and embeddings. | Built in DuckDB plus compact numpy arrays (union-find over input pairs, sparse SVD), identical in both modes. |

The cost reductions in the table above are historical observations, not a
matched speedup claim for this worktree. Typed projections retain canonical
JSON and evidence references; SQL graph construction preserves IDs, attributes,
first-insert ordering and real-prevout-only spend resolution. SQL address-window
aggregates and bounded Arrow numeric reads avoid repeated Python decoding.
Each child has its own spill directory and one native SQL/BLAS thread. Export
row groups are 8,192 rows after the baseline allocation failure.

Graph-only SQL and serial peeling preprocessing now receive the already
planned half-budget, with at most four threads. Neither phase has a concurrent
detector pool. Their prior quarter-budget failed the real 3M outpoint/grouping
joins at 1.1 GiB. Each `finally` block restores the exact detector memory
configuration and thread count, including on failure. This is not a change to
graph or scoring semantics. Fresh 100K canonical and complete published
ML/confidence parity pass after both changes.

The real PostgreSQL 1M repeat also exposed a confidence-pinning SQL bottleneck:
`NOT IN` materialized all saved pins and repeatedly scanned that set above the
planner's hash threshold. The equivalent correlated `NOT EXISTS` (pin primary
key is non-null) gets a hash anti-join and preserves write-once provenance.
Image06 includes the fix; fresh full 100K graph/features/findings/ML/pin parity
passes and a missing-pin/idempotency regression compiles the PostgreSQL SQL.
The interrupted image05 1M attempt remains FAIL, not a successful timing control.

Image07 additionally materializes the exact rapid-spend prevout/time relation
once before the parallel address-window pool, with bucket ordering, instead of
repeating the same global joins per partition. Original raw records/sequences,
inclusive0..3600-second bounds and scoring thresholds are unchanged. Serial
construction restores the exact detector thread/memory settings in `finally`.
The independent1M old-query/cache comparison covers all180884 changed rows
and48 partitions, with zero wrong-bucket rows and identical full-row hashes.
The complete100K graph/features/deterministic/ML/pin comparisons pass too.

## Measured results

All times below are real HTTP upload plus separate API/worker unless marked
replay/profile. RSS is sampled at one-second intervals, can miss peaks and
sums shared pages more than once. A 5,120 MiB planning budget is not an OS hard
RSS cap. External desktop load is not a controlled laboratory workload.

| Run / configuration | Strict outcome | Wall seconds | Worker-tree peak MiB |
| --- | --- | ---: | ---: |
| Untouched 100K control | Failed native Parquet allocation | about 84.2 | 2,756 |
| Serial SQL budgets 100K, 2 workers / 2,600 MiB | PASS all stages, including Geo-IP | 101.233 | 2,720 |
| Final confidence anti-join 100K, same config | PASS all stages and exact full parity | 126.062 | 2,624 |
| Final rapid-spend cache 100K, same config | PASS all stages and exact full parity | 103.203 | 2,381 |
| 303,850 TX, 1 worker / 5,120 MiB | PASS | 395.054 | 3,664 |
| Same 303,850 TX, 2 workers | PASS | 337.532 | 3,779 |
| Same 303,850 TX, 4 workers | PASS | 291.829 | 5,053 |
| Same 303,850 TX, 6 workers | FAIL native export allocation | 193.483 to failure | — |
| Same 303,850 TX, 8 workers | FAIL native export allocation | 205.566 to failure | — |
| 1,014,581 TX, 4 workers / 5,120 MiB, SQLite | PASS all stages, no OOM | 1,444.038 | 5,415 |
| 3,043,062 TX, first SQLite attempt | FAIL graph native outpoint join | 1,303.161 to failure | 3,894 |
| Same 3M, receipt-based SQLite recovery | Graph PASS; FAIL peeling native grouping | 611.503 recovery-only | 3,623 |
| Copied-image05 PostgreSQL 1M | FAIL; stalled confidence query canceled, not a successful control | 1975.723 to interruption | 5,808 |
| Copied-image06 PostgreSQL 1,014,581 TX | PASS every mandatory stage, Geo-IP and embeddings | 1388.134 | 5,779 |
| Copied-image07 PostgreSQL, identical 1,014,581 TX | PASS all stages/counts, full published ML/pins equal | 1289.993 | 5,681 |

The 100K diagnostic overlapped brief focused tests and is correctness evidence,
not a clean timing comparison. The matched serial 300K source/configuration
and graph/features/findings fingerprints agree at workers 1/2/4. Four is the
smallest worker count within 5% of the fastest passing result; six/eight failed
their 203/152 MiB native child export budgets and are now rejected earlier
under the same joint budget. This does not prove a speedup over an equivalent
successful untouched 300K control: that control was unavailable after the
untouched 100K failure. The earlier 515.312 s / 3,475 MiB 300K profile shared CPU
with a build and remains diagnostic only.

Evidence: `experiments/runs/matched-workers-300k-20261003-01.json`,
`var/scale-run/ladder-1m-w4-20261003-final-01/result.json`,
`var/scale-run/ladder-3m-w4-20261003-final-01/result.json`, and
`experiments/runs/graph-budget-{canonical,full-ml}-parity-100k-20261003.json`.
The recovery publishes 17,885,618 graph nodes and 29,372,118 edges, with
4,388,594 resolved inputs and 45,917 genuinely missing outpoints. The graph
stage takes 587.318 s; peeling fails 15.829 s later. Both failures are retained.
The next phase-budget fix preserves 100K fingerprints in
`experiments/runs/serial-sql-budget-{canonical,full-ml}-parity-100k-20261003.json`.
Copied-image PostgreSQL outcomes are recorded separately
in [the final acceptance report](implementation_acceptance_2026-10-03.md).
Do not treat a resumed run's elapsed time as a new end-to-end upload measurement.

The successful image06 PostgreSQL stage seconds are .858 verification,
274.291 ingest,181.620 graph,640.350 deterministic/features,126.274 ML and
155.561 analytics. It writes10133 ML findings,74978 entities,919789 wallets,
420222 embeddings and183 network findings. The separate actual-worker3M
preflight rejects7884 MiB estimated ML against5120 MiB planned RAM; available
memory at observation also does not admit it. This screen is NOT a fresh
PostgreSQL3M pipeline result. Following the user's updated priority, the1M
cache optimization passes in1289.993 seconds:2.359 verification,273.931 ingest,
202.187 graph,537.955 deterministic/features,121.740 ML and140.976 analytics.
Observed full elapsed reduction is7.07%, findings/features15.99%. Only
`app/engine/bounded.py` differs in deployed app/worker source hashes; configs,
versions, source, locks, manifests and calibration match. This is one sequential
pair on a noisy shared workstation, not replicated/isolated timing or a proven
minimum. No production score/model was changed to obtain the reduction.

All10133 published ML findings and confidence pins match exactly after only
known run-ID normalization; original feature hashes/model links are verified
first. Evidence: `experiments/runs/paired-appliance-1m-performance-20261003.json`,
`experiments/runs/appliance-full-ml-parity-1m-20261003.json` and
`experiments/runs/rapid-spend-parity-1m-20261003.json`. Complete1M unpublished
features/deterministic/graph canonical parity is not claimed from equal counts.
Image07 worker-only RSS4752 MiB/tree5681; API421/PGtree1360, logical scratch
8237382075 bytes/spill704118784. Tree/spill are lower, parent/API/PG RSS and
scratch slightly higher; not every resource metric improved. Both1M case vaults
remain for review. Neither3M success nor any-dataset AP improvement is inferred.

## Explicit supported limits

Bounded parsing, SQL spilling and partitioned detectors are not constant-memory
ML, embeddings or risk propagation. Global estimates include identifier
dictionaries, matrices and scoring scratch, not merely compact integer facts.
`FactStore.ml_facts` admits `1600*TX + 384*outputs + 80*inputs` bytes against
both the planned budget and 80% of currently available memory. The exact 1M
fixture estimate is 2,755,040,368 bytes; the 3M fixture requires 8,267,692,976
bytes, above the tested 5,120 MiB budget. Do not lower the estimate or skip ML
to turn a degraded import into a release pass. Analytics and risk have their
own global-array admission checks. More RAM is necessary to test larger global
workloads under this procedure; an alternate scalable procedure needs its own
parity or versioned scoring decision.

Derived scratch is screened at 12,000 bytes per accepted transaction plus
512 MiB reserve; this is an admission estimate, not protection against other
applications consuming disk. Resource NDJSON records filesystem free space.
The 3M recovery additionally has a metadata-only `.work`/spill observer;
PostgreSQL appliance sampling includes scratch observations. Logical file
sizes are not exclusive physical allocation and sampling can miss peaks.

## Reproduction

Use unique names. Helpers never reset a pre-existing database or overwrite a
benchmark result. Dataset manifests and independently computed acceptance
counts must agree; injected scenarios and duplicate rows make the canonical
counts greater than the nominal generator size. Evaluation truth is not an
import input. The generator-v3 seeds/hashes are in retained manifests.

```bash
python generator.py --help
python generator.py --rows 3000000 --output datasets/review_NEW_3m \
  --seed tracex-review-scale-three-million-20261003-v1 --workers 4 --verify

uv run --extra ml python scripts/scale_benchmark.py \
  datasets/review_scale_3m_20261003/ingestion_rows.ndjson \
  --name NEW-3m-w4 --workers 4 --memory-budget-mb 5120 \
  --execution-mode bounded --expected-counts experiments/acceptance_3m_counts_20261003.json \
  --min-transactions 3043062 --max-seconds 1800 --timeout 7200 --keep

# Existing copied appliance: bearer-authenticated upload; no host-code worker.
uv run python -m scripts.appliance_acceptance \
  --base http://PRIVATE_API_ADDRESS:8000 \
  --compose var/FINAL_COPIED_INSTALL/docker-compose.yml \
  --project tracex-review-final --context default \
  --source datasets/review_scale_3m_20261003/ingestion_rows.ndjson \
  --expected-counts experiments/acceptance_3m_counts_20261003.json \
  --name NEW-offline-postgres-3m --min-transactions 3043062 \
  --max-seconds 1800 --timeout 7200
```

The private address can change after container recreation; inspect the exact
review API, never an unrelated stack. These commands intentionally keep all
mandatory stages enabled. A completed job with missing/failed intelligence
still exits nonzero. Full-source hashes, stage timings, software versions,
configuration, runtime-source hashes, memory and failures are preserved in
each result. The <=1,800-second target remains a separate project gate, not an
official problem-statement requirement or a linear extrapolation from 300K.
