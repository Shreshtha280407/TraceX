# Phase 6 — performance, recovery, security

One measured pipeline, honest clocks. Every number below came from an actual
run on this development host (Arch Linux, kernel `7.2.4-arch1-2`, x86_64,
13th Gen Intel Core i5-13420H, 8 physical / 12 logical CPUs, 15 GiB RAM, 15
GiB swap, ~22 GiB free disk at measurement time). This is **not** the
36 GiB Mac profile the master architecture describes as a target; see
`docs/hardware.md`. No number in this document is estimated, interpolated,
or carried over from a different host.

```bash
make phase-six-test             # ruff + the crash/retry + offline/access + Phase 4 regression tests
make phase-six-throughput       # staged timings against the real generator-v2 100K fixture
make phase-six-throughput-1m    # the same pipeline at 1,000,000 synthetic rows
make phase-six-query-latency    # 200+ bounded graph queries at 1 and 4 concurrent readers
```

## What Phase 6 actually exercises

Every script here drives the real HTTP + worker pipeline (`app/api/routes.py`
+ `workers/runner.py` + `app/engine/ingestion/pipeline.py`), the same code
path a production request takes — nothing is called directly to shortcut
around the API, and no stage is mocked. `scripts/phase6_throughput.py` and
`scripts/phase6_query_latency.py` are new; the crash/retry and offline/access
evidence lives in `tests/unit/test_phase_six_crash_retry.py` and
`tests/unit/test_phase_six_offline_and_access.py`.

## 1. Throughput, staged

The pipeline does not expose separate hooks for "parse" vs "fragment commit"
— `app.engine.ingestion.pipeline.ingest_source` runs one batch loop for both
— so `source_verification_and_parse_commit_sec` is reported as one number,
honestly, rather than inventing a split the code does not have. Graph build,
and deterministic findings-plus-features (also one function,
`materialize_findings`, in this codebase), are isolated by wrapping the exact
symbols `ingest_source` calls; no production code changed to take the
measurement.

| | Real 100K fixture (generator v2, 4 detector-rich motifs) | Synthetic 100K (linear chains, throughput-only) | Synthetic 1M |
| --- | --- | --- | --- |
| Source file | `datasets/phase5a_100k/ingestion_rows.ndjson` | `/tmp/synth_100k.ndjson` | `/tmp/synth_1m.ndjson` |
| Source bytes | 126,004,521 | 34,718,361 | 347,367,161 |
| Row width (top-level JSON keys) | 19 | 6 | 6 |
| Rows accepted | 100,000 | 100,000 | **did not complete** |
| Upload (HTTP, hash-while-write) | 0.391 s | 0.164 s | n/a |
| Source verification + parse + commit | 11.36 s | 5.228 s | n/a |
| Graph build | 27.052 s | 9.317 s | n/a |
| Deterministic findings + features | 150.034 s | 39.149 s | n/a |
| **Ingest total** | **188.447 s** | **53.694 s** | **OOM-killed at ~3m40s** |
| Rows/sec (ingest stage only) | 530.7 | 1,862.4 | n/a |
| Graph nodes / edges | 589,987 / 970,260 | 203,000 / 266,544 | n/a |
| Findings written | 49,240 | 56,523 | n/a |

**The 1M-row run did not complete on this host — the kernel OOM-killer ended
it.** Confirmed from `journalctl -k`:

```
Out of memory: Killed process 275178 (python3) total-vm:20716552kB,
anon-rss:10805848kB, ... UID:1000 pgtables:34304kB oom_score_adj:0
```

~10.3 GiB resident at kill time, on a host with 15 GiB RAM (+15 GiB swap,
already ~9-10 GiB in use from the ingest working set). The first attempt (not
shown in the table) used the script's default address-pool sizing
(`rows // 1000` → 1,000 distinct addresses for 1M rows, an average of 1,000
rows per address — far denser than the 100K synthetic column's 1-per-33) and
was killed even earlier, around 12 GiB resident, confirming address-reuse
density independently drives up `materialize_findings`' working-set size (see
§6). The second attempt used a matched ratio
(`--address-pool 30000`, 1-per-33, the same as the 100K synthetic column) and
still did not fit: memory climbed in two identifiable phases — a peak
during graph construction (`app/engine/graph/builder.py` holds every node and
edge as a Python object in two dicts, then materializes a *second* full
Python list-of-dicts per table just before the PyArrow conversion, roughly
doubling that peak), a partial drop as the graph flushed to DuckDB and those
dicts were freed, then renewed, unbounded growth through the batched
parse/commit and findings loop that the OOM-killer eventually ended before
findings could run at all.

**A second, independent finding while chasing this: `/tmp` on this host is
`tmpfs` (RAM-backed, 7.7 GiB, confirmed via `mount`), not disk.** The query
latency benchmark's first attempt (§3) failed with a `sqlite3.OperationalError:
disk I/O error` writing feature rows — not a code bug, but `tmpfs` running out
of space because leftover scratch files from earlier runs (including the
347 MB synthetic 1M fixture) had already consumed most of it, on top of
competing with the ingestion process's own heap for the same physical RAM.
Both `scripts/phase6_throughput.py` and `scripts/phase6_query_latency.py` now
write their working directories under `var/phase6-tmp/` (ext4, gitignored)
instead of the OS temp default. This does not change the OOM finding above —
that was Python heap RSS, unrelated to where temp files live — but it was a
real, separate failure mode worth fixing and stating plainly rather than
silently retrying past it.

**This is the honest ceiling for this implementation on this host, not a
target the guide expected to fail.** The master architecture's resource
budget assumes a 36 GiB Mac; nothing in `app/engine/graph/builder.py` or
`app/engine/findings/deterministic.py` currently bounds memory to less than
"the whole snapshot's nodes, edges, and window buckets, in Python objects, at
once." Both the 100K real fixture (`589,987` nodes / `970,260` edges) and the
100K synthetic column completed comfortably; scaling either by 10x is where
this host runs out. A streaming or chunked graph/feature build — writing
Arrow batches incrementally instead of collecting one `dict`/`list` for the
whole snapshot — would be the fix, and is out of scope for this benchmark to
implement.

ML findings were disabled for every throughput run (`--with-ml` omitted) to
isolate the deterministic pipeline's own cost; the anomaly stack's own
throughput is already measured separately in `docs/anomaly_stack.md`
(`make anomaly-stack`, ~38 s over the same fixture, all 63 layer
combinations).

**Why the real fixture and the synthetic one differ so much per row.** The
real fixture is generator v2's labelled scenario data: bursts, motifs,
near-misses, deliberate address reuse patterns — every one of those is a
detector hit, and `materialize_findings` does real work per hit (peeling
chains, CoinJoin structure, rapid-redistribution windows). The synthetic
generator (`scripts/phase6_throughput.py::generate_synthetic_ndjson`) is
deliberately plain — many independent 1–5-hop chains over a seeded-PRNG
address pool — so its 100K-row column is a fairer same-generator comparison
point for the 1M-row extrapolation than the real fixture would be.

**Starting targets from the master architecture were explicitly unmeasured.**
First 10K-record facts within 5 s, first 100K-fixture rank within 20 s, 1M
clean transactions within 180 s, warm capped graph p95 within 500 ms. Measured
against the *real* 100K fixture on this host: first useful output (see below)
is 38.4 s and first ranked lead is 188.4 s — both above the stated targets.
The targets were written for a 36 GiB Mac; this host has 15 GiB. Rather than
re-quote the target as if it were met, the honest statement is: **on this
hardware, the deterministic-findings stage is the dominant cost, and it does
not hit the stated target at 100K rows.** See "Where the time goes" below.

## 2. First useful output / first ranked lead

The guide's "first" is honest here rather than aspirational: this pipeline
builds the graph and runs findings **once**, after every batch of the source
file is ingested — not incrementally per batch. So:

- **First useful output** = upload-complete → graph committed =
  `source_verification_and_parse_commit_sec + graph_build_sec`.
- **First ranked lead** = upload-complete → first deterministic finding
  committed = first useful output + `deterministic_findings_and_features_sec`
  (findings run immediately after the graph, before anything else).

On the real 100K fixture: first useful output **38.4 s**, first ranked lead
**188.4 s**. A provisional label is not separately modelled in this build —
`Snapshot.provisional` flips to `False` only at the very end of
`ingest_source`, atomically with completion, not per-batch — so there is no
intermediate "provisional graph" state to time separately. That is a real
gap against the guide's "show provisional label until reconciliation"
language, noted here rather than glossed over.

## 3. Bounded graph query latency

`scripts/phase6_query_latency.py` ingests the real 100K fixture once (same
pipeline as §1: 589,987 nodes, 970,260 edges), finds the graph's actual
highest-degree node by querying the built DuckDB file directly (not
guessed), then issues 210 `GET /v1/cases/{id}/graph` calls through the real
route — 10 discarded as warmup, 200 timed — split between 60 queries against
the highest-degree node (depths 1/2/3, 20 each) and 150 against random
node seeds (address, transaction, and output nodes) at depth 1. Every query
uses the production default caps (`node_limit=200`, `edge_limit=500`).

The highest-degree node in this fixture is not an address or transaction —
it's a shared network-relay endpoint, `endpoint:198.51.100.1:8333`, at
**19,319 edges** (the next four: 10,330 / 7,301 / 5,547 / 4,560 — a
realistic power-law tail from many transactions relaying through a handful
of observed endpoints).

| | 1 reader | 4 readers |
| --- | --- | --- |
| Queries | 210 | 210 |
| Wall clock | 25.816 s | 10.041 s |
| Throughput | 8.1 q/s | 20.9 q/s |
| p50 | 104.7 ms | 120.6 ms |
| p95 | **215.1 ms** | **481.2 ms** |
| p99 | 237.8 ms | 601.5 ms |
| min / max | 64.9 ms / 318.4 ms | 55.9 ms / 708.3 ms |
| Failures | 0 | 0 |
| Queries truncated (capped) | 60 / 210 | 60 / 210 |

**Against the master architecture's stated target (warm capped graph p95
within 500 ms): met at 1 reader (215 ms), effectively met at 4 concurrent
readers (481 ms, under the line but with p99 at 601 ms).** All 60 truncated
queries are the high-degree-node queries at depth ≥ 2 — expected, correct
behavior (`query_neighbourhood` reports `truncated_nodes`/`truncated_edges`
rather than silently returning a partial graph), not a defect. Four
concurrent readers gave a real 2.6x throughput improvement (8.1 → 20.9 q/s)
on this 12-logical-CPU host — DuckDB's `read_only=True` connections against
one file do genuinely parallelize, not serialize behind a lock — but per-query
tail latency roughly doubled under contention (p95 215 ms → 481 ms), the
expected cost of shared CPU/IO rather than a bug.

## 4. Crash and retry

Both failure points the guide names are exercised in
`tests/unit/test_phase_six_crash_retry.py`, by raising `KeyboardInterrupt`
(not `Exception` — so it is not swallowed by `workers/runner.py`'s
`except Exception: fail_job(...)` handler, exactly like a real `SIGKILL`
would not be) at the exact point in the real write path:

- **Mid-fragment, before the atomic rename.** `os.replace` is intercepted
  after the Parquet bytes are written and `fsync`'d to the `.staging/*.part`
  temp file, but before the rename to the final immutable path. Verified
  after the "crash": no `FragmentReceipt` row exists, no final fragment file
  exists, the job stays `running` (never silently marked `checkpointed`), and
  the `.staging` temp file is cleaned up by `_write_immutable`'s `finally`
  block even though an exception was raised. After reclaiming the lease
  (`lease_expires_at` moved into the past, the same condition
  `claim_next_job` checks in production) and retrying: job reaches
  `completed`, `rows_seen`/`rows_accepted` match the source exactly, exactly
  one `FragmentReceipt` exists for the batch, and the recovered fragment has
  the correct row count — no duplication, no loss.
- **After rename, before the receipt commits.** The fragment is durably
  renamed to its final path (confirmed on disk), then `append_event` —
  called in `_commit_batch` right before `session.commit()`, after the
  `FragmentReceipt` rows are `session.add()`-ed — is intercepted to crash.
  The receipt insert was never durably committed. Verified: fragment bytes
  exist on disk, no `FragmentReceipt` row is visible. On retry, the pipeline
  re-derives the same batch, `_write_immutable` finds the path already
  exists, recomputes its digest, finds it byte-identical to what it's about
  to write, and reuses the existing file instead of rewriting or
  conflicting (`app/engine/catalogue/fragments.py`'s
  `if path.exists(): if _digest(path) != digest: raise ...`). Verified: the
  fragment bytes are byte-identical before and after retry, exactly one
  receipt is created, row count is correct.

Both tests upload through the real `/v1/cases/{id}/imports` endpoint and
retry through the real `workers.runner.process_one`, not a hand-rolled
substitute.

## 5. Offline and access

`tests/unit/test_phase_six_offline_and_access.py`:

- **No outbound network call.** `socket.socket.__init__` is patched to raise
  `AssertionError` for any address family other than `AF_UNIX` (which covers
  `TestClient`'s in-process ASGI transport, itself confirmed to not touch a
  real socket by this same test — if it did, the guard would have caught its
  own transport and the test would fail immediately, not just fail to catch
  a hypothetical app-level call). Under that guard, a full upload → parse →
  commit → graph → findings run, plus a graph query, completes successfully.
- **Case isolation on every endpoint the guide names.** A second,
  authenticated, non-member user gets `404 {"detail": "Case not found"}` —
  not `403`, matching the existing "don't reveal the case exists" policy in
  `app/auth/dependencies.py::require_case_member` — on `GET .../graph`,
  `GET .../events`, `GET .../findings/export`, `GET .../features/export`, and
  `GET .../sources`. A positive control (the real owner gets `200` with real
  content on the same five calls) rules out the trivial false pass where
  every request 404s regardless of membership. A sixth check covers
  `GET /v1/evidence/{source_id}/records`, which is keyed by `source_id` in
  the URL rather than `case_id` — confirming membership is still enforced
  via the source's own `case_id`, not accidentally open because the case
  isn't in the path.

This complements, not duplicates, `tests/unit/test_auth_and_case_reads.py`
(which already covers case detail/sources 404-not-403); this file covers
specifically the endpoints Phase 6 names: "snapshots, event stream, or
exports."

## 6. A real bug found while building this, fixed under Phase 4's ownership

While building the 1M-row synthetic throughput fixture, ingestion crashed
with a SQLite `UNIQUE constraint failed` on the `findings` table's
`(snapshot_id, entity_ref, window_start, window_end, rule_id)` key. Root
cause, confirmed by direct reproduction against
`app.engine.findings.deterministic.materialize_findings`
(`app/engine/motifs/deterministic.py::detect_peeling_chains` tries every
uniquely-resolved spent outpoint as a chain start, not just chain roots): two
*independent* peeling chains that happen to originate at the same
(reused) address, with their first spend landing in the same 15-minute
window, each produce a `peeling_chain_candidate` candidate with an identical
`(entity_ref, window, rule_id)` key — and the insert loop
(`app/engine/findings/deterministic.py`, previously around line 513) had no
deduplication before the bulk insert. This is address-reuse-within-a-window,
not a contrived adversarial input; it is plausible on real data (e.g. an
exchange hot wallet address that is the origin of two unrelated peeling
chains within fifteen minutes).

**Fix** (`app/engine/findings/deterministic.py`): deduplicate `candidates` by
that exact key before insert, keeping the highest-scoring candidate per key
(deterministic tie-break: first-constructed wins, since candidate
construction order is itself deterministic). Five lines, no schema change,
no behavior change on the non-colliding path.

**Verification:** a regression test,
`test_two_independent_peeling_chains_sharing_an_origin_address_and_window_do_not_crash_ingestion`
in `tests/unit/test_phase_four.py`, builds two independent 3-hop chains
sharing an origin address and window; confirmed to fail (job state `failed`,
the same `IntegrityError`) against the pre-fix code and pass against the fix.
All 118 pre-existing unit tests still pass; `make anomaly-stack-test` and
`make phase-five-b-compare` were not rerun (out of scope for this change —
neither touches `deterministic.py`'s candidate construction) but the fix is
strictly additive (fewer rows only when a real collision exists) so it
cannot change their inputs.

## 7. Known gaps, stated rather than hidden

- **1M-row throughput on this host: did not complete.** The kernel
  OOM-killer ended the ingestion worker at ~10.3 GiB resident (§1). This is a
  real, confirmed hardware/implementation ceiling on this 15 GiB host, not a
  simulated or assumed one — see §1 for the two attempts, the exact
  `journalctl -k` evidence, and why (unbounded in-memory graph/feature
  construction, not the address-reuse fix in §6, which only pushed the
  ceiling a bit higher before it was hit by graph size alone).
- **No incremental "first useful output."** Noted in §2 — the graph and
  findings are built once per snapshot, not streamed per batch, so there is
  no earlier partial signal to time.
- **Query latency, single case, single graph file.** Concurrent-reader
  numbers are DuckDB's own `read_only=True` multi-connection behavior against
  one file; this does not test concurrent *writers* (there is only ever one
  ingestion worker per case's snapshot by construction) or multiple
  simultaneous cases' graphs on the same host, which the resource-budget
  section below addresses qualitatively, not by measurement.
- **10M/30-minute run:** explicitly a stretch goal per the guide, not
  attempted — 1M already characterizes this host's ceiling (see §1).

## Resource budget on this host (not the 36 GiB Mac profile)

15 GiB total RAM, 15 GiB swap, single ingestion worker process observed to
use double-digit-GB RSS at 1M-row scale (see §1) — there is no 8 GiB
DuckDB-buffer / 3 GiB graph-feature / 3 GiB services partition to propose
here the way the master architecture does for a 36 GiB machine, because this
host cannot support that partition *and* the peak observed by one worker.
The honest operating recommendation for this hardware: one ingestion worker
active at a time per host, not the four-worker profile the guide suggests
starting from.
