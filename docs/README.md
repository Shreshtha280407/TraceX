# TraceX documentation map

Start with the [master README](../README.md) for the product story, real UI recordings, quickstart, architecture and scoped results. This index connects the existing engineering guides and implementation reports.

## 01 · Run an investigation

| Guide | Use it for |
|---|---|
| [Quickstart](../README.md#quickstart) | Start the local API, worker and investigator console |
| [Offline Linux appliance](../deploy/appliance/README.md) | Prepare, transfer, install and operate the offline bundle |
| [MacBook runbook](macbook_runbook.md) | Native architecture, Docker VM admission and UI routing |
| [Recorded walkthrough](readme-media.md) | Follow or regenerate the real synthetic UI demonstration |
| [Canonical schemas](../schemas/v1/README.md) | Understand transaction, output, observation and evidence contracts |
| [Dataset inspection](../app/engine/dataset_cli.py) | Inspect supplied bytes, field coverage and normalization |

## 02 · Understand the engine

| Guide / implementation | Use it for |
|---|---|
| [Requirements and source status](requirements.md) | Trace project requirements and distinguish confirmed data from contextual material |
| [Phase 1: cases and access](phase1.md) | Understand the early identity, case and intake foundation |
| [Phase 2: canonical ingestion](phase2.md) | Follow streaming parsing, normalization, fragments and receipts |
| [Phase 3: UTXO graph](phase3.md) | Review outpoint semantics, graph snapshots and bounded queries |
| [Phase 4: deterministic findings](phase4.md) | Inspect address-window observations and source-backed rules |
| [Current grouped release and quality](grouped_review_2026-10-04.md) | Read the selected v2 procedure, grouping contract and measured ML decision |
| [Integrated implementation](integrated_implementation_2026-10-04.md) | Review implementation boundaries, stage outcomes and eligibility |
| [Recipient history and deployment](recipient_history_and_deployment_2026-10-04.md) | Understand causal candidate contracts and fail-closed activation |
| [Analytics implementation](../app/engine/analytics.py) | Inspect entity association, similarity, network context and seeded exposure |
| [Structured evidence implementation](../app/engine/evidence.py) | Trace supporting/opposing records, procedure identities and coverage |

The [anomaly-stack design history](anomaly_stack.md) also describes research layers and older experiments. Read its dated scope alongside the current release report; the existence of a layer in the research harness does not make it active in default v2.

## 03 · Operate and recover

| Guide | Use it for |
|---|---|
| [Worker retry correction, 5 Oct](worker_retry_fix_2026-10-05.md) | Bounded DB retry classification, lease ownership and preserved evidence |
| [Group queue recovery, 5 Oct](group_queue_recovery_2026-10-05.md) | Generate missing review groups without reuploading or rescoring evidence |
| [Review counts correction, 5 Oct](review_counts_fix_2026-10-05.md) | Distinguish queued, unresolved and backlog groups across API/UI |
| [Grouping executor, 5 Oct](grouping_speed_fix_2026-10-05.md) | Disk-indexed execution, atomic publication, progress and PostgreSQL renewal |
| [Laptop 1M admission](laptop_1m_runbook.md) | Screen RAM, source copies, disk/scratch and complete-stage resource demand |
| [Appliance configuration](../deploy/appliance/.env.example) | Review deployment settings without committing secrets |
| [Runtime configuration](../app/config.py) | Find authoritative setting names and defaults |

## 04 · Verify and extend

| Guide / tool | Use it for |
|---|---|
| [Historical scale report](scale.md) | Examine earlier million-transaction performance under its exact configuration |
| [October 3 acceptance](implementation_acceptance_2026-10-03.md) | Review earlier copied-appliance, browser and isolation measurements |
| [Quality matrix correction](quality_matrix_reporting_2026-10-04.md) | Understand missing/inapplicable metric handling |
| [Compact grouped final results](../experiments/grouped_review_20261004/final/README.md) | Inspect registered synthetic finals and the retained-v2 decision |
| [Backend tests](../tests/) | Locate regression coverage and fixture-specific prerequisites |
| [Live browser acceptance](../frontend/e2e/acceptance.mjs) | Exercise authenticated evidence, graph, review, export and case isolation |
| [README capture runner](../scripts/capture_readme.py) | Produce real recordings on its own disposable synthetic stack |
| [API routes](../app/api/) | Find authoritative endpoint behavior; the running `/docs` exposes schemas |
| [Frontend guide](../frontend/README.md) | Work on the React console |

## Reading measurements correctly

Reports are dated records. Their commands, counts and hardware belong to the code/configuration they identify. Some contain owner handoff notes that describe the work at the time of writing.

- **Implemented:** the source contains the capability.
- **Small verified:** a named fixture/check exercised it.
- **Historical measured:** a prior release/configuration produced the recorded result.
- **Blocked / not run:** admission prevented the workload; no pass or timing result exists.
- **Not evaluable:** required labels, population applicability or queue coverage are missing.

The newest grouped release has small PostgreSQL and real-browser evidence. Its fresh million-TX acceptance remains blocked/not run; the earlier 1M timing is separate. Synthetic AP, queue capacity and group precision have distinct meanings.
