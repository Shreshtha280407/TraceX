
# TraceX — Bitcoin Intelligence and Investigation Platform

**Case-scoped, evidence-first analysis of Bitcoin transaction data: UTXO graph construction, deterministic pattern findings, unsupervised anomaly ranking, and reviewable evidence export.**

| Item | Value |
| --- | --- |
| Software version | `0.1.0` (`pyproject.toml`, `app/main.py`) |
| Repository | `TODO: insert the verified repository URL` (the source archive used to prepare this document contained no VCS remote) |
| Problem-statement reference | Project documents cite **SIH 26146**. Its official wording and portal status are recorded as unconfirmed in [`docs/requirements.md`](docs/requirements.md). `TODO: confirm ID and official wording before submission` |
| Team, institution, department, organization | `TODO: not present in the repository` |
| License | MIT, copyright holder as stated in [`LICENSE`](LICENSE) |

> **Interpretation notice.** Every finding, score and rank produced by TraceX is an investigative lead that requires human review. None of them is proof of criminal conduct, of common ownership, or of the identity of any person. TraceX contains no address-attribution dataset.

---

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Problem framing and motivation](#2-problem-framing-and-motivation)
3. [Objectives and scope](#3-objectives-and-scope)
4. [Proposed solution](#4-proposed-solution)
5. [Implemented functional capabilities](#5-implemented-functional-capabilities)
6. [System architecture](#6-system-architecture)
7. [End-to-end data and investigation flow](#7-end-to-end-data-and-investigation-flow)
8. [Technical design and algorithms](#8-technical-design-and-algorithms)
9. [Technology stack](#9-technology-stack)
10. [Repository structure](#10-repository-structure)
11. [Installation and deployment](#11-installation-and-deployment)
12. [API reference](#12-api-reference)
13. [Data contracts and examples](#13-data-contracts-and-examples)
14. [Testing and validation](#14-testing-and-validation)
15. [Experimental evaluation and performance](#15-experimental-evaluation-and-performance)
16. [Security, privacy and evidence integrity](#16-security-privacy-and-evidence-integrity)
17. [Limitations and assumptions](#17-limitations-and-assumptions)
18. [Demonstration workflow](#18-demonstration-workflow)
19. [Screenshots and diagrams](#19-screenshots-and-diagrams)
20. [Contributing, license, acknowledgments, contact](#20-contributing-license-acknowledgments-contact)
21. [Documentation references](#21-documentation-references)
22. [Documentation verification record](#22-documentation-verification-record)

---

## 1. Executive summary

Investigators who receive bulk Bitcoin transaction exports must establish which outputs were spent by which transactions, find structurally unusual activity in large connected datasets, and retain enough provenance for another reviewer to reproduce each conclusion. TraceX addresses this as a single offline-capable workflow.

An authorised user uploads a CSV, NDJSON, JSON-array or XML file into an isolated **case**. TraceX stores the original bytes immutably under a SHA-256 path, parses them in a background worker, normalizes records to integer satoshis with explicit unknowns, quarantines invalid rows, and commits Parquet fragments with database receipts. From the committed fragments it builds a DuckDB UTXO graph, generates deterministic findings (address-window rules, bounded peeling-chain candidates, CoinJoin-like structures), and, when the optional `ml` dependencies are installed, an unsupervised anomaly ranking. Each finding carries source-record locators, coverage limitations and benign alternatives. Reviewers record dispositions with optimistic concurrency, and a case-scoped JSON export bundles findings, evidence references, review history and audit entries.

The intended users are analysts, reviewers and case leads working on datasets they are authorised to hold. All quantitative results in this repository were obtained on **synthetic** data (Section 15); no claim of real-world detection effectiveness is made.

## 2. Problem framing and motivation

This section is the project's own problem framing. The official problem-statement text has not been supplied to the repository (`docs/requirements.md`), so exact alignment with an official statement is not claimed.

- **Spend lineage is incomplete in exported data.** When previous-output references (`prev_txid`, `prev_vout`) are absent or point outside the supplied coverage, spend links cannot be established. Inventing them would corrupt downstream analysis.
- **Value flow is ambiguous.** A Bitcoin transaction spends prior outputs and creates new ones; assigning a particular input to a particular output requires an additional, evidenced method.
- **Structural patterns have benign look-alikes.** Payroll batches, exchange consolidation and treasury sweeps can satisfy the same shape predicates as mixing-like or peeling-like activity.
- **Provenance is easily lost.** Scores that cannot be traced to exact source rows cannot be independently checked.
- **Reproducibility.** Investigations need deterministic replay: same source bytes, same rule and model versions, same output.

## 3. Objectives and scope

### 3.1 Engineering objectives (as implemented)

| ID | Objective | Where addressed |
| --- | --- | --- |
| O1 | Preserve source bytes and hash them before parsing | `app/storage/raw.py` |
| O2 | Normalize to integer satoshis; unknown is `null`, never inferred | `app/engine/canonical/normalize.py` |
| O3 | Build a UTXO graph that creates a spend edge only when both outpoint fields resolve to a committed output | `app/engine/graph/builder.py` |
| O4 | Produce reviewable findings with source locators, coverage and benign alternatives | `app/engine/findings/`, `app/engine/motifs/`, `app/ml/findings.py` |
| O5 | Isolate cases and record reviewer decisions and audit entries | `app/auth/`, `app/api/routes.py`, `app/models.py` |
| O6 | Survive worker interruption without duplicate or phantom data | `app/jobs/service.py`, `app/engine/catalogue/fragments.py` |
| O7 | Rank transactions by anomaly using strictly causal features | `app/ml/` |

### 3.2 Out of current scope

Real-world address attribution; live blockchain node integration; multi-node or horizontally scaled operation; streaming graph construction beyond available memory (Section 17); ingestion of the official SIH-linked dataset (its bytes have not been inspected).

## 4. Proposed solution

| Stage | Description |
| --- | --- |
| Input | One source file per import (`.csv`, `.json` top-level array, `.ndjson`, `.xml`), uploaded to a case with an `Idempotency-Key`. |
| Processing | Hash-preserving storage, streaming parse, batch normalization with quarantine, Parquet fragment commit with receipts, immutable DuckDB graph, deterministic findings and address-window features, optional anomaly ranking. |
| Output | Snapshot-scoped graph, feature rows, findings with evidence, case events, export bundles. |
| Analyst interaction | Browse cases and findings, query bounded graph neighbourhoods, reopen the exact raw source record for any locator, record a disposition with reasons and counter-evidence references. |
| Evidence trail | Source SHA-256, record locators (CSV record index, JSON Pointer, XML element path), fragment receipts, rule/release identifiers, review history, audit records. |

## 5. Implemented functional capabilities

Status values: **Implemented** (present in source and covered by a committed test or benchmark script), **Partial**, **Research-only**, **Not implemented**. "Covered by test" refers to existence of a committed test; the pytest suite was **not executed** while preparing this document (Section 22).

| Capability | Status | Basis |
| --- | --- | --- |
| Ingestion of CSV, NDJSON, JSON array, XML with safe XML parsing | Implemented | `app/engine/adapters/source.py`; `tests/unit/test_phase_two.py` |
| Validation, normalization, quarantine, duplicate and conflicting-variant detection per import | Implemented | `normalize.py`, `pipeline.py` |
| Case-scoped workflows with roles `case_lead`, `analyst`, `reviewer` | Implemented | `app/auth/dependencies.py`, `routes.py` |
| Bitcoin UTXO graph with six edge types | Implemented | `graph/builder.py`; `tests/unit/test_phase_three.py` |
| Bounded neighbourhood query (depth 1–5) | Implemented | `graph/query.py` |
| Graph continuation cursor | **Partial** — a cursor is returned when results are capped, but no route accepts it | `graph/query.py`, `routes.py` |
| Deterministic address-window rules (3) | Implemented | `findings/deterministic.py` |
| Peeling-chain candidate, CoinJoin-like structure, synthetic-seed proximity | Implemented | `motifs/deterministic.py`; `test_phase_four_one.py` |
| Anomaly scoring and ranking (layers A and D deployed) | Implemented, optional (`ml` extra); evaluated on synthetic data only | `app/ml/` |
| Layers B, C, E and supervised layer S | Layers B, C, E: implemented in the offline harness, not part of the deployed release. Layer S: **Research-only** | `app/ml/layers.py` |
| Evidence references and record replay by locator | Implemented | `GET /v1/evidence/{source_id}/records` |
| Offline Geo-IP / ASN enrichment from open downloadable databases (DB-IP country lite CC BY 4.0, IPtoASN PDDL) | Implemented | `app/engine/geoip.py`, `tracex-geoip`; `tests/unit/test_entities_network_risk.py` |
| Entity clustering (common-input-ownership, CoinJoin-shaped inputs excluded) | Implemented | `app/engine/analytics.py`; `GET /v1/cases/{id}/entities` |
| Graph embeddings (spectral, wallet x transaction) and similar-wallet search | Implemented | `app/engine/analytics.py`; `GET /v1/cases/{id}/entities/{wallet}/similar` |
| Network <-> blockchain correlation (relay concentration tests, Geo-IP mismatch, network context on every ML finding) | Implemented | `app/engine/analytics.py`, `app/ml/findings.py`; `GET /v1/cases/{id}/network` |
| Risk propagation from analyst-seeded illicit wallets (UTXO haircut taint, downstream + upstream) | Implemented | `app/engine/analytics.py`; `/v1/cases/{id}/risk*` |
| Calibrated confidence per finding (isotonic on labelled synthetic truth; statistical for network tests; anomaly p-value) | Implemented | `app/engine/confidence.py`, `scripts/calibrate_confidence.py` |
| Memory-adaptive execution (in-memory or bounded on-disk) for multi-million-row imports | Implemented | `app/resources.py`, `app/engine/bounded.py` |
| Analyst review with optimistic concurrency | Implemented | `POST /v1/findings/{id}/reviews` |
| Case-scoped evidence export with review and audit history | Implemented | `GET /v1/cases/{id}/findings/export` |
| Feature export | Implemented (streamed JSON, paged JSON, or the snapshot's Parquet feature store) | `GET /v1/cases/{id}/features/export[.parquet]`, `app/engine/feature_store.py` |
| Durable job leasing, crash and retry recovery | Implemented (tested against SQLite, Section 14) | `tests/unit/test_phase_six_crash_retry.py` |
| Server-sent case events with replay | Implemented | `GET /v1/cases/{id}/events` |
| Single-finding chat assistant using a locally hosted Ollama model | Implemented, optional, **no committed test located**; requires a reachable Ollama endpoint | `app/engine/chat/assistant.py` |
| Web frontend (React, 9 routes) | Implemented; Playwright smoke script provided, not executed here | `frontend/` |
| Agentic or autonomous AI workflow | **Not implemented.** The chat feature is single-turn question answering over one finding's stored evidence, with no tools or actions | — |
| Incremental or provisional graph per ingestion batch | **Not implemented** (documented gap in `docs/phase6.md`) | — |
| Offline Linux appliance (container image + compose + air-gapped installer) | Implemented; verified by building the image, an air-gapped install from the bundle, an end-to-end import, and a no-egress check on the worker | `Dockerfile`, `deploy/appliance/`, `scripts/build_offline_bundle.sh` |
| Ingestion of PS-shaped datasets | Implemented. The PS publishes no dataset ("Dataset Link: Nil"; participants use synthetic data); header aliases, list encodings and epoch timestamps are mapped, and `tracex-dataset inspect` verifies any supplied file | `app/engine/canonical/profiles.py`, `app/engine/dataset_cli.py`, `data_manifest.json` |

## 6. System architecture

TraceX is a **two-process, shared-nothing-in-memory** system: a stateless FastAPI process serves requests, and a separate worker process performs all heavy computation. The two never call each other. They coordinate only through the PostgreSQL control plane (job rows, leases, events) and the filesystem evidence vault. This keeps uploads responsive, makes worker crashes recoverable, and means every derived artifact can be traced back to immutable source bytes.

### 6.1 Design principles

| Principle | How the architecture enforces it |
| --- | --- |
| **Evidence first** | Original bytes are hashed while streaming to disk and published by atomic rename to `<case>/<sha256>/original`. Nothing downstream may modify them; the worker re-verifies hash and size before parsing. |
| **Unknown stays unknown** | Normalization emits `null` for missing values; the graph builder creates a `SPENT_BY` edge only when both outpoint fields resolve to a committed output. Gaps are reported as coverage counters, not filled in. |
| **Commit or nothing** | Each ingestion batch commits Parquet fragments (staging file, then atomic rename) together with receipts, a checkpoint and an event in one database transaction. Files without receipts are ignored on resume. |
| **Case isolation** | Every route, query and file path is scoped by `case_id`; non-members receive `404`, not `403`. |
| **Deterministic replay** | Same source bytes plus the same rule and release identifiers produce the same output; findings carry locators that reopen the exact original record. |
| **Optional intelligence, mandatory review** | The anomaly stack and chat assistant are optional and can fail or be disabled without failing an import. Every result is a lead that a human disposes through a versioned review. |

### 6.2 Logical architecture

Read the diagram **left to right**. The browser talks only to the **API**. The **Worker** does all heavy computation. The API and the Worker never call each other: they meet only in the **storage** column (PostgreSQL and the evidence vault).

**Colour key:** blue = user interface, teal = API process, amber = worker process, green = storage, purple dashed = optional external service.

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontSize":"22px","fontFamily":"Arial, Helvetica, sans-serif","lineColor":"#1E293B","textColor":"#0F172A","primaryTextColor":"#0F172A","edgeLabelBackground":"#FFFFFF"},"flowchart":{"nodeSpacing":45,"rankSpacing":70,"curve":"basis","padding":18}}}%%
flowchart LR
    subgraph L1["① PRESENTATION"]
        UI["<b>React web app</b><br/>Dashboard<br/>Evidence Intake<br/>Graph Explorer<br/>Findings Feed<br/>Export"]
    end

    subgraph L2["② API PROCESS<br/>FastAPI, stateless"]
        AUTH["<b>Auth</b><br/>token + case membership"]
        UPLOAD["<b>Upload</b><br/>stream, hash, store"]
        QUERY["<b>Graph query</b><br/>bounded, read-only"]
        REVIEW["<b>Review and export</b><br/>plus chat proxy"]
    end

    subgraph L3["③ WORKER PROCESS<br/>runs the pipeline"]
        direction TB
        J["<b>1. Claim job</b><br/>lease + heartbeat"]
        I["<b>2. Ingest</b><br/>parse, normalize, quarantine"]
        G["<b>3. Build graph</b><br/>DuckDB snapshot"]
        D["<b>4. Find patterns</b><br/>rules, peeling, CoinJoin-like"]
        M["<b>5. Rank anomalies</b><br/>optional ML"]
        J --> I --> G --> D --> M
    end

    subgraph L4["④ STORAGE"]
        PG[("<b>PostgreSQL</b><br/>jobs, receipts, findings<br/>reviews, audit, events")]
        VAULT[("<b>Evidence vault</b><br/>original files<br/>Parquet fragments<br/>graph files")]
    end

    subgraph L5["⑤ OPTIONAL"]
        LLM["<b>Ollama</b><br/>local chat model"]
    end

    UI ==>|"HTTPS + token"| L2
    L2 <==>|"metadata"| PG
    L2 ==>|"save and read files"| VAULT
    L3 <==>|"jobs, findings"| PG
    L3 <==>|"fragments, graphs"| VAULT
    L2 -.->|"one finding question"| LLM

    class UI ui
    class AUTH,UPLOAD,QUERY,REVIEW api
    class J,I,G,D,M worker
    class PG,VAULT store
    class LLM ext
    style L1 fill:#EFF6FF,stroke:#1D4ED8,stroke-width:2px,color:#1E3A8A
    style L2 fill:#ECFEFF,stroke:#0E7490,stroke-width:2px,color:#164E63
    style L3 fill:#FFFBEB,stroke:#B45309,stroke-width:2px,color:#78350F
    style L4 fill:#F0FDF4,stroke:#15803D,stroke-width:2px,color:#14532D
    style L5 fill:#FAF5FF,stroke:#7E22CE,stroke-width:2px,color:#581C87

    classDef ui fill:#DBEAFE,stroke:#1D4ED8,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef api fill:#CFFAFE,stroke:#0E7490,stroke-width:3px,color:#0F172A
    classDef worker fill:#FEF3C7,stroke:#B45309,stroke-width:3px,color:#0F172A
    classDef store fill:#DCFCE7,stroke:#15803D,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef ext fill:#F3E8FF,stroke:#7E22CE,stroke-width:3px,stroke-dasharray:6 4,color:#0F172A
    classDef risk fill:#FEE2E2,stroke:#B91C1C,stroke-width:3px,color:#0F172A
```

| Layer | What it does | Main code |
| --- | --- | --- |
| ① Presentation | Investigator screens | `frontend/src` |
| ② API process | Login, authorization, upload, graph queries, review, export | `app/main.py`, `app/api/routes.py`, `app/auth`, `app/storage/raw.py` |
| ③ Worker process | Runs the five pipeline steps in order for each import | `workers/runner.py`, `app/jobs/service.py`, `app/engine`, `app/ml` |
| ④ Storage | PostgreSQL decides *what is valid*; the vault holds *the bytes* | `app/models.py`, `TRACEX_EVIDENCE_ROOT` |
| ⑤ Optional | Local LLM for single-finding questions; failure never affects imports | `app/engine/chat/assistant.py` |

### 6.3 Runtime topology and trust boundaries

Red = untrusted input. Green = trusted single host. Everything the user uploads is treated as hostile until it has been hashed and parsed by a safe reader.

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontSize":"22px","fontFamily":"Arial, Helvetica, sans-serif","lineColor":"#1E293B","textColor":"#0F172A","primaryTextColor":"#0F172A","edgeLabelBackground":"#FFFFFF"},"flowchart":{"nodeSpacing":45,"rankSpacing":70,"curve":"basis","padding":18}}}%%
flowchart LR
    subgraph U["UNTRUSTED"]
        B["<b>Browser</b><br/>localhost:5173"]
        F["<b>Uploaded file</b><br/>CSV, NDJSON,<br/>JSON, XML"]
    end

    subgraph H["TRUSTED HOST: one machine"]
        API["<b>API</b><br/>uvicorn, port 8000"]
        WK["<b>Worker</b><br/>tracex-worker<br/>one per host"]
        FS[("<b>Evidence vault</b><br/>filesystem volume")]
        DBC[("<b>PostgreSQL 16</b><br/>port 5433")]
    end

    OL["<b>Ollama</b><br/>port 11434<br/>optional"]

    B ==>|"REST + SSE"| API
    F ==>|"size limit + hash on write"| API
    API <--> DBC
    API --> FS
    WK <--> DBC
    WK <--> FS
    API -.-> OL

    class B,F risk
    class API api
    class WK worker
    class FS,DBC store
    class OL ext
    style U fill:#FEF2F2,stroke:#B91C1C,stroke-width:2px,color:#7F1D1D
    style H fill:#F0FDF4,stroke:#15803D,stroke-width:2px,color:#14532D

    classDef ui fill:#DBEAFE,stroke:#1D4ED8,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef api fill:#CFFAFE,stroke:#0E7490,stroke-width:3px,color:#0F172A
    classDef worker fill:#FEF3C7,stroke:#B45309,stroke-width:3px,color:#0F172A
    classDef store fill:#DCFCE7,stroke:#15803D,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef ext fill:#F3E8FF,stroke:#7E22CE,stroke-width:3px,stroke-dasharray:6 4,color:#0F172A
    classDef risk fill:#FEE2E2,stroke:#B91C1C,stroke-width:3px,color:#0F172A
```

| Boundary | Control |
| --- | --- |
| Browser → API | Bearer token (HMAC-SHA256, TTL `TRACEX_TOKEN_TTL_SECONDS`), CORS restricted to a single origin (hard-coded to `http://localhost:5173` in `app/main.py`, Section 11.5), case-membership check on every case-scoped route |
| Upload → vault | Extension allow-list (`.csv`, `.json`, `.ndjson`, `.xml`), byte limit (`TRACEX_MAX_UPLOAD_BYTES`), sanitized filename, hash-while-write, immutable target with digest check |
| Source bytes → parser | Streaming parsers; `defusedxml` for XML; invalid rows quarantined with a locator, never silently dropped |
| Control plane ↔ vault | Receipts in PostgreSQL define which files are valid; vault paths are resolved against the evidence root and rejected if they escape it |
| API → LLM | Optional, single-turn, grounded in one finding's stored evidence; failure returns `503` and affects nothing else |

### 6.4 Evidence vault layout

All derived data is addressable from the source SHA-256, which is what makes replay and audit possible.

```text
<TRACEX_EVIDENCE_ROOT>/
├── .staging/                              in-flight uploads (*.part), removed on success or failure
└── <case_id>/
    ├── <source_sha256>/original           immutable uploaded bytes
    ├── derived/<source_sha256>/
    │   └── batch-00000001/<record_type>.parquet   fragments, valid only when a receipt exists
    └── graphs/snapshot-<snapshot_id>.duckdb       immutable UTXO graph for one import
```

### 6.5 Job lifecycle and recovery

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontSize":"22px","fontFamily":"Arial, Helvetica, sans-serif","lineColor":"#1E293B","primaryColor":"#FEF3C7","primaryBorderColor":"#B45309","primaryTextColor":"#0F172A","tertiaryColor":"#FFFFFF"}}}%%
stateDiagram-v2
    direction LR
    [*] --> queued: upload accepted
    queued --> running: worker claims job
    running --> checkpointed: batch committed
    checkpointed --> running: next batch
    checkpointed --> running: lease expired, reclaimed
    running --> completed: graph, findings, ranking done
    running --> failed: unrecoverable error
    completed --> [*]
    failed --> [*]

    classDef good fill:#DCFCE7,stroke:#15803D,stroke-width:3px,color:#0F172A
    classDef bad fill:#FEE2E2,stroke:#B91C1C,stroke-width:3px,color:#0F172A
    classDef wait fill:#DBEAFE,stroke:#1D4ED8,stroke-width:3px,color:#0F172A
    class completed good
    class failed bad
    class queued,checkpointed wait
```

A job is reclaimable when its state is `running` or `checkpointed` and its lease has expired. Resumption continues after the last checkpointed record. The anomaly stage is best-effort: if it is disabled, unavailable or fails, the import still completes and `ml_status` records why.

### 6.6 Component table

| Component | Responsibility | Inputs | Outputs | Implementation |
| --- | --- | --- | --- | --- |
| Frontend | Investigator interface: case dashboard, evidence intake, graph explorer, findings feed, evidence package, export | User actions; REST and SSE | Rendered views; JSON downloads | React 19, React Router 7, TypeScript, Vite; hand-written CSS |
| API | Authentication, case authorization, upload, queries, review, export | HTTP requests | JSON, SSE streams | FastAPI, Pydantic, SQLAlchemy |
| Auth | Password hashing, signed session tokens, membership checks | Credentials; `Authorization: Bearer` | Authenticated user or `401`/`404` | Python standard library (PBKDF2-HMAC-SHA256, HMAC-SHA256) |
| Evidence vault | Immutable original bytes, Parquet fragments, graph files | Upload streams; worker output | Files under `TRACEX_EVIDENCE_ROOT` | Local filesystem, SHA-256, atomic rename |
| Control plane | Metadata, jobs, leases, receipts, findings, reviews, audit, events | ORM writes | Relational records | PostgreSQL 16 via `psycopg` (docker-compose); SQLite is used by all committed tests and scripts |
| Worker | Claim jobs, verify source hash, run ingestion, graph, findings, ranking | Queued jobs | Snapshot, graph snapshot, findings | `workers/runner.py` |
| Ingestion pipeline | Stream, normalize, quarantine, commit fragments and checkpoints | Original source | Parquet fragments, receipts, events | `ijson`, `defusedxml`, `pyarrow` |
| Graph builder | Build immutable node and edge tables with coverage counters | Receipt-approved fragments | DuckDB file plus `GraphSnapshot` record | DuckDB, PyArrow |
| Deterministic findings | Address-window features, three window rules, peeling and CoinJoin-like detectors | Fragments, graph coverage | `FeatureRecord`, `FindingRecord` | Pure Python |
| Anomaly stack | Transaction-structure and burst scoring, fusion, budgeted flagging | Fragments | `FindingRecord` rows with `rule_version=anomaly-stack-v1` | NumPy, SciPy, scikit-learn (optional extra) |
| Chat assistant | Answer a question using only one finding's stored evidence | Finding evidence, question | Text answer | HTTP call to Ollama (`qwen3:8b` default) |

### 6.7 Architectural constraints

- **Single node.** One API process, one active worker per host, local filesystem vault. The job lease (default 30 s) is renewed only at batch commits, so a second worker could reclaim a job that is still in its graph or findings stage (Section 16.3, Section 17).
- **In-memory graph build.** The graph builder and findings stage hold snapshot-wide structures in memory (Section 17).
- **No incremental graph.** Findings are computed once per snapshot after ingestion completes (Section 8.6).

## 7. End-to-end data and investigation flow

The whole journey has **four phases**. Phases 1 to 3 are automatic; phase 4 is where the human analyst works.

**Colour key:** blue = analyst, teal = API, amber = worker, green = analyst review.

```mermaid
%%{init: {"theme":"base","themeVariables":{"fontSize":"22px","fontFamily":"Arial, Helvetica, sans-serif","lineColor":"#1E293B","textColor":"#0F172A","primaryTextColor":"#0F172A","edgeLabelBackground":"#FFFFFF"},"flowchart":{"nodeSpacing":45,"rankSpacing":70,"curve":"basis","padding":18}}}%%
flowchart TB
    P1["<b>PHASE 1: UPLOAD</b><br/>Analyst sends one file to a case<br/>API hashes it with SHA-256 and saves it<br/>A job is queued"]
    P2["<b>PHASE 2: INGEST</b><br/>Worker claims the job and re-checks the hash<br/>Parses, normalizes, quarantines bad rows<br/>Commits Parquet fragments in batches"]
    P3["<b>PHASE 3: ANALYSE</b><br/>Builds the UTXO graph<br/>Runs the pattern rules<br/>Ranks anomalies if ML is enabled"]
    P4["<b>PHASE 4: INVESTIGATE</b><br/>Analyst opens findings, graph and source records<br/>Records a review decision<br/>Exports the evidence bundle"]

    P1 ==>|"job row in PostgreSQL"| P2
    P2 -->|"repeat for every batch"| P2
    P2 ==>|"all batches committed"| P3
    P3 ==>|"import.completed event"| P4

    class P1 api
    class P2,P3 worker
    class P4 ui

    classDef ui fill:#DBEAFE,stroke:#1D4ED8,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef api fill:#CFFAFE,stroke:#0E7490,stroke-width:3px,color:#0F172A
    classDef worker fill:#FEF3C7,stroke:#B45309,stroke-width:3px,color:#0F172A
    classDef store fill:#DCFCE7,stroke:#15803D,stroke-width:3px,color:#0F172A,font-weight:bold
    classDef ext fill:#F3E8FF,stroke:#7E22CE,stroke-width:3px,stroke-dasharray:6 4,color:#0F172A
    classDef risk fill:#FEE2E2,stroke:#B91C1C,stroke-width:3px,color:#0F172A
```

### 7.1 Step-by-step

| # | Phase | Who | What happens | Result is stored in |
| :-: | --- | --- | --- | --- |
| 1 | Upload | Analyst | Sends the file to `POST /v1/cases/{id}/imports` with an `Idempotency-Key` | request |
| 2 | Upload | API | Streams the file to staging, computes SHA-256, syncs to disk, then moves it atomically into place | Evidence vault: original file |
| 3 | Upload | API | Creates the source record and a `queued` job, writes audit and event rows, replies `202` with `job_id` | PostgreSQL |
| 4 | Ingest | Worker | Records a heartbeat, claims the job with a lease, re-verifies source hash and size | PostgreSQL: lease |
| 5 | Ingest | Worker | For each batch: parse, normalize, quarantine invalid rows, detect duplicates | in memory |
| 6 | Ingest | Worker | Writes the batch as a Parquet file (staging, then atomic rename) | Evidence vault: fragments |
| 7 | Ingest | Worker | Commits receipt, checkpoint and `batch.committed` event in **one** transaction | PostgreSQL |
| 8 | Analyse | Worker | Builds one DuckDB graph file from receipt-approved fragments | Evidence vault: graph, PostgreSQL: `GraphSnapshot` |
| 9 | Analyse | Worker | Computes address-window features and deterministic findings | PostgreSQL |
| 10 | Analyse | Worker | Anomaly ranking, only if enabled and available; otherwise records why it was skipped | PostgreSQL |
| 11 | Analyse | Worker | Marks the snapshot complete and emits `import.completed` with counts and `ml_status` | PostgreSQL |
| 12 | Investigate | Analyst | Reads findings, graph neighbourhoods and evidence; reopens raw records by locator | read-only |
| 13 | Investigate | Analyst | Posts a review with `expected_finding_version`; a stale version returns `409` | PostgreSQL: review and audit |
| 14 | Investigate | Analyst | Downloads the case export (findings, evidence references, reviews, audit) | JSON file |

### 7.2 Rules that hold at every step

| Rule | Behaviour |
| --- | --- |
| **Duplicate upload** | The same content inside one case reuses the existing source record (unique on `case_id`, `sha256`). A repeated `Idempotency-Key` returns the existing job. |
| **Crash or restart** | A `running` or `checkpointed` job whose lease has expired is reclaimed (`FOR UPDATE SKIP LOCKED`). Work resumes after the last checkpoint; files without receipts are ignored. |
| **Batch size** | `TRACEX_INGESTION_BATCH_RECORDS`, default 32768 records per committed batch. |
| **ML is optional** | If the anomaly stage is disabled, unavailable, misconfigured or failing, the skip is recorded and the import still completes. |
| **Snapshot state** | A snapshot is provisional throughout ingestion and becomes complete only at step 11. |
| **One graph per import** | Each import creates its own snapshot and graph. The graph endpoint always uses the most recent completed graph of the case. |

Ordering was verified against `app/engine/ingestion/pipeline.py`.

## 8. Technical design and algorithms

### 8.1 Bitcoin data model

- A transaction is identified by `(case_id, network, txid)`; an output by `txid:vout`. Amounts are integer satoshis; values with excess precision or above the maximum supply constant are quarantined.
- Inputs reference a prior output through `prev_txid` and `prev_vout`. Both are nullable. When either is null, the input contributes to coverage counters (`missing_outpoint_inputs`) but produces no `SPENT_BY` edge.
- Timestamps are normalized to UTC. A generic `timestamp` is stored as a source timestamp with meaning `unknown`; only an explicit `block_time` sets the meaning to `block_time`.
- Network observations (`src_ip`, `dst_ip`, ports, `geo_country`, `asn`) are stored as separate records and never as ownership or origin assertions.

### 8.2 Graph representation and traversal

Nodes: `transaction` (`tx:<txid>`), `output` (`out:<txid>:<vout>`), `address_or_script` (`address:<value>`), `network_observation` (`obs:<id>`), `network_endpoint` (`endpoint:<ip>:<port>`).

| Edge | Meaning |
| --- | --- |
| `CREATES_OUTPUT` | transaction created this output |
| `SPENT_BY` | output was spent by this transaction; created only when the outpoint resolves to a committed output and has a single spender |
| `LOCKED_TO` | output is locked to an address or script identifier (not an identity) |
| `OBSERVED_TX`, `SEEN_FROM`, `SEEN_TO` | separate network-observation relations |

Coverage counters recorded per graph: input count, resolved spend inputs, missing outpoints, unknown prior outputs, double-spend conflicts, value checks with complete previous outputs, value violations, and `spend_lineage_complete`.

Traversal (`graph/query.py`) is a breadth-first expansion that follows edges in **both directions** from a seed (a node identifier, or a 64-hex txid), with parameterized DuckDB queries, default depth 1 / 200 nodes / 500 edges and hard caps of depth 5 / 1000 nodes / 3000 edges. Results report `truncated_nodes` and `truncated_edges` rather than silently omitting data.

### 8.3 Deterministic pattern detection

All rules live in `app/engine/findings/deterministic.py` and `app/engine/motifs/deterministic.py` (rule version `deterministic-v1`). Address-window features are computed at 15-minute, 1-hour and 24-hour windows. Every feature record marks `window_boundary_incomplete: true` because activity outside the supplied data is unknown.

| Rule identifier | Condition (defaults) | Score formula |
| --- | --- | --- |
| `concentrated_collection` | at least 3 distinct source transactions send outputs to one address in a window | `min(100, 25 + 15·n)` |
| `emerging_hub` | at least 4 distinct source transactions in a window | `min(100, 20 + 10·n)` |
| `rapid_redistribution` | at least 3 inbound transactions and at least one committed-prevout spend within 3600 s | `min(100, 45 + 10·events)` |
| `peeling_chain_candidate` | at least 3 sequential verified spends, at most 8 hops; the continuation is the largest output strictly smaller than the previous output that is itself uniquely spent | mean of hop extent, output-reduction regularity, timestamp continuity, evidence coverage (0–1) |
| `coinjoin_like_structure` | at least 3 inputs, 3 outputs and 3 outputs with equal value (tolerance 0 sat by default) | mean of input, output, equal-output and evidence factors (0–1) |
| `synthetic_seed_proximity` | downstream distance at most 2 verified UTXO hops from an explicit synthetic review seed; decay 0.60 per hop; permitted only in cases created with `synthetic: true` | `0.60^distance` |

The `coinjoin_like_structure` result is an observable shape only and does not assert CoinJoin or mixing activity. Peeling continuation selection is a structural convention, not a change-output inference.

**Score scale.** The three address-window rules score on 0–100 while the three motif detectors score on 0–1. Findings from both families are sorted together by raw score when ranks are assigned (`candidates.sort`), so ranks compare unlike scales; this is consistent with the deterministic ranks reported in `docs/phase7.md` (2013 and 2024 of 2124 for two CoinJoin-like findings). Treat `rank` across rule families with caution.

### 8.4 Anomaly stack

Release `anomaly-stack-v1` (`app/ml/findings.py`) runs **layer A (global)** and **layer D (burst)** and fuses them.

| Element | Implementation |
| --- | --- |
| Transaction features | 32 columns (`app.ml.grains.TRANSACTION_COLUMNS`): input and output counts, equal-output group sizes at several tolerances, value entropy and Gini, output shares, fee terms, peel ratio, round-value counts, input value concentration, input ages, rapid-input share |
| Layer A | Isolation Forest (100 trees, `max_samples` 256, seed 42) and an in-tree ECOD implementation, rank-normalized against reference rows and averaged |
| Layer D | Per motif family (`coinjoin_like`, `peel_step`), a Poisson EWMA control statistic over 900-second count buckets (half-life 96 buckets). Bayesian online change-point posteriors are computed and stored in layer detail; the per-transaction score uses the EWMA burst statistic. Only transactions belonging to a family carry that family's burst score |
| Family membership | Derived from structural predicates (at least 3 inputs, 3 outputs and 3 equal outputs within 100 sat; or a one-large-one-small output shape). Layer D therefore inherits part of the deterministic rule's predicate |
| Fusion | Per-layer empirical-CDF p-values fitted on reference rows, weighted Stouffer combination (equal weights), threshold at the reference quantile matching the review budget (default 1%) |
| Temporal assumption | Reference set is the earliest 70% of the snapshot's transactions by time. Every feature uses only facts at or before its transaction's timestamp; equality of features under truncation is checked by property tests (`truncate_facts` in `tests/unit/test_anomaly_stack.py`) |
| Fitting | Dynamic refit per snapshot; **no static model artifact exists**. `release_identity()` and `release_manifest_sha256()` identify the frozen procedure; `model_run_id` is deterministic in release, snapshot and budget |
| Minimum data | Snapshots with fewer than 50 transactions are not scored |
| Flagging | All rows scoring at or above the reference threshold are written, including reference rows; the budget is therefore a calibration target, not a hard cap on the queue |

Offline-only components: layers B (Kaplan–Meier spend latency), C (empirical-Bayes history), E (bounded graph features), HBOS, and supervised layer S (`HistGradientBoostingClassifier`, opt-in, trained on fixture generator labels). Layer S is not run inside the application; `review_decision_labels` returns `None` until at least 50 review decisions with at least 10 positives exist and is not wired into the ingestion path.

### 8.5 Explainability and evidence traceability

- Deterministic findings expose `reason_codes`, an `explanation`, `graph_path` (node and edge identifiers), `evidence_refs`, coverage, uncertainty, benign alternatives, and an opposing-evidence coverage statement.
- Anomaly findings store per-layer scores, the fused score, the threshold, the four strongest ECOD feature contributions from layer A, the structural feature values, and source locators for the transaction's records.
- Any locator can be reopened against the immutable original file through the evidence-record endpoint.

### 8.6 Design trade-offs

- Findings are computed once per snapshot after ingestion rather than incrementally, which simplifies consistency but delays the first result (Section 15.2).
- The graph builder holds all nodes and edges in memory (Section 17).
- ML scoring is refit per snapshot, which avoids stale artifacts but makes calibration depend on the snapshot's own early transactions.

## 9. Technology stack

Versions below are version constraints from `pyproject.toml` and `frontend/package.json`; resolved versions are pinned in `uv.lock` and `frontend/package-lock.json`.

| Layer | Technology |
| --- | --- |
| Language and tooling | Python `>=3.11`; `uv`; `ruff` and `pytest` (dev extra) |
| API | FastAPI `>=0.116,<1`; Uvicorn (standard); python-multipart; SQLAlchemy `>=2.0.43,<3` |
| Control-plane database | PostgreSQL 16 (`postgres:16-alpine` in `docker-compose.yml`); `psycopg[binary]>=3.2,<4` |
| Ingestion and storage | `ijson>=3.3,<4`, `defusedxml>=0.7,<1`, `pyarrow>=18,<24` (Parquet, zstd) |
| Graph | `duckdb>=1.2,<2` |
| Machine learning (extra `ml`) | `scikit-learn>=1.5,<2`, `numpy>=1.26,<3`, `scipy>=1.11,<2`; ECOD and HBOS implemented in-tree |
| Frontend | React `^19.2.8`, React Router `^7.18.4`, TypeScript `~6.0.2`, Vite `^8.3.0`, oxlint, Playwright (smoke test) |
| Optional LLM | Ollama HTTP API, default model `qwen3:8b` |

## 10. Repository structure

```text
.
├── app/                     Backend application
│   ├── main.py              FastAPI app and CORS configuration
│   ├── config.py            Environment-driven settings
│   ├── db.py, models.py     SQLAlchemy engine, sessions, ORM models
│   ├── events.py            Durable case events and outbox records
│   ├── api/routes.py        All HTTP routes
│   ├── auth/                Password hashing, tokens, membership checks
│   ├── jobs/service.py      Idempotent jobs, leases, heartbeats
│   ├── storage/raw.py       Streaming hash-while-write source storage
│   ├── engine/
│   │   ├── adapters/        CSV, NDJSON, JSON-array, XML readers
│   │   ├── canonical/       Normalization to v1 facts
│   │   ├── catalogue/       Immutable Parquet fragment publication
│   │   ├── ingestion/       Batch ingestion, checkpoints, completion
│   │   ├── graph/           Graph builder and neighbourhood queries
│   │   ├── motifs/          Peeling, CoinJoin-like, seed-proximity detectors
│   │   ├── findings/        Address-window features and finding records
│   │   └── chat/            Optional grounded chat (Ollama)
│   └── ml/                  Anomaly stack (layers, fusion, findings, evaluation)
├── workers/runner.py        Worker entry point (tracex-worker)
├── frontend/                React + TypeScript investigator UI
├── schemas/v1/              JSON Schema contract for canonical records
├── examples/                Example v1 records
├── fixtures/                Synthetic fixture generators and manifests
├── scripts/                 Benchmark, walkthrough, evaluation and release scripts
├── tests/                   Phase 0 stdlib tests and pytest suite (tests/unit)
├── experiments/             Protocols, model decision, release manifest
├── docs/                    Requirements, phase reports, final report, hardware notes
├── docker-compose.yml       PostgreSQL service only
├── Makefile                 Reproducible verification targets
├── pyproject.toml, uv.lock  Python dependency declaration and lock
├── data_manifest.json       Source and fixture inventory
├── feature_schema.json      Feature schema descriptor
└── LICENSE
```

## 11. Installation and deployment

### 11.0 Fastest path: the offline Linux appliance

For a complete offline installation (PostgreSQL, API + web UI, worker, ML stack
and the open Geo-IP database in one image), build the bundle once on a connected
machine and install it on any Linux host with Docker — no network needed there:

```bash
scripts/build_offline_bundle.sh                 # -> dist/tracex-offline-<version>.tar.gz
# on the target host
tar xzf tracex-offline-<version>.tar.gz && ./tracex-offline/install.sh
# open http://127.0.0.1:8000/
```

Details: `deploy/appliance/README.md`. For development, follow 11.1 onwards. The
Geo-IP database for a source checkout is installed with `make geoip` (or, offline,
`uv run tracex-geoip import <DB-IP / IPtoASN files>`).

### 11.1 Prerequisites

Python 3.11 or newer, [`uv`](https://docs.astral.sh/uv/), Docker with Compose (for PostgreSQL), and Node.js with npm (frontend). Dependency installation requires network access unless wheels and packages are pre-provisioned; TraceX does not require network access at runtime except for the optional chat feature.

### 11.2 Download the source from a command shell

The repository URL is not recorded in the source archive (see the table at the top). Replace `<owner>` with the account that hosts the repository, or substitute your full URL; `TODO: insert the verified repository URL`. The default branch is assumed to be `main`, as suggested by the archive folder name `TraceX-main`.

#### Option 1: `git clone` (recommended)

Works in Bash, Zsh, PowerShell and Windows Command Prompt once Git is installed.

```bash
# confirm Git is available
git --version

# HTTPS
git clone https://github.com/<owner>/TraceX.git

# or SSH, if you have a key registered with the host
git clone git@github.com:<owner>/TraceX.git

cd TraceX
```

Useful variants:

```bash
git clone --depth 1 https://github.com/<owner>/TraceX.git      # latest snapshot only, smaller and faster
git clone --branch main https://github.com/<owner>/TraceX.git  # name the branch explicitly
git clone https://github.com/<owner>/TraceX.git tracex-src     # clone into a different folder name
```

To update an existing clone later: `git pull --ff-only`.

#### Option 2: download the ZIP archive without Git

**Linux and macOS (Bash or Zsh)**

```bash
curl -L -o TraceX-main.zip https://github.com/<owner>/TraceX/archive/refs/heads/main.zip
# or: wget -O TraceX-main.zip https://github.com/<owner>/TraceX/archive/refs/heads/main.zip

unzip TraceX-main.zip
cd TraceX-main
```

**Windows PowerShell**

```powershell
Invoke-WebRequest -Uri "https://github.com/<owner>/TraceX/archive/refs/heads/main.zip" -OutFile "TraceX-main.zip"
Expand-Archive -Path "TraceX-main.zip" -DestinationPath .
cd TraceX-main
```

**Windows Command Prompt (Windows 10 build 17063 and later)**

```bat
curl -L -o TraceX-main.zip https://github.com/<owner>/TraceX/archive/refs/heads/main.zip
tar -xf TraceX-main.zip
cd TraceX-main
```

#### Verify the download

```bash
ls                       # expect app/, frontend/, workers/, docker-compose.yml, Makefile, pyproject.toml, uv.lock
cat pyproject.toml | grep '^version'     # expect version = "0.1.0"
```

On PowerShell use `dir` and `Select-String '^version' pyproject.toml`. If the clone or download fails with a `404`, `403` or authentication prompt, the repository is probably private or the URL is wrong; sign in with `git credential` support, a personal access token, or an SSH key, and re-check the URL.

Continue with Section 11.3 (environment variables) and Section 11.4 (development setup).

### 11.3 Environment variables

Defined in `app/config.py` unless noted.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRACEX_ENV` | `development` | Any other value makes startup fail unless `TRACEX_SECRET_KEY` is set to a non-default value |
| `TRACEX_SECRET_KEY` | public development constant | HMAC key for session tokens. **Must be set to a unique random value outside development** (for example the output of `openssl rand -hex 32`) |
| `TRACEX_DATABASE_URL` | `postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5432/tracex` | SQLAlchemy URL |
| `TRACEX_EVIDENCE_ROOT` | `var/evidence` | Evidence vault root; use a dedicated volume |
| `TRACEX_MAX_UPLOAD_BYTES` | `536870912` | Upload size limit |
| `TRACEX_LEASE_SECONDS` | `30` | Job lease duration |
| `TRACEX_EVENT_HEARTBEAT_SECONDS` | `15` | SSE heartbeat interval |
| `TRACEX_WORKER_HEALTH_SECONDS` | `60` | Maximum worker heartbeat age for `/readyz` |
| `TRACEX_INGESTION_BATCH_RECORDS` | `32768` | Records per committed batch |
| `TRACEX_TOKEN_TTL_SECONDS` | `86400` | Session token lifetime |
| `TRACEX_ALLOW_DEV_ACTOR_HEADER` | `0` | Development-only `X-TraceX-Actor` identity bypass. **Never enable outside local development** |
| `TRACEX_ML_FINDINGS` | `1` | Set to `0` to disable the anomaly ranking |
| `TRACEX_ML_REVIEW_BUDGET` | `0.01` | Review budget fraction; values outside `(0, 0.5]` are recorded as `invalid_budget` |
| `TRACEX_ML_THREADS` | `min(4, CPU count)` | BLAS/OpenMP thread bound for the ML stack (`app/ml/stack.py`) |
| `TRACEX_OLLAMA_BASE_URL`, `TRACEX_OLLAMA_MODEL`, `TRACEX_OLLAMA_TIMEOUT_SECONDS` | `http://localhost:11434`, `qwen3:8b`, `60` | Optional chat backend |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Frontend build-time API base URL (`frontend/.env.example`) |

### 11.4 Development setup

`docker-compose.yml` publishes PostgreSQL on **host port 5433**, whereas the default `TRACEX_DATABASE_URL` uses port 5432. Set the URL explicitly:

```bash
docker compose up -d postgres
uv sync --extra dev --extra ml
export TRACEX_DATABASE_URL='postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5433/tracex'
uv run tracex-init-db                                          # creates tables (SQLAlchemy create_all)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000       # API
uv run tracex-worker                                           # worker, in a second terminal
```

Frontend, in a third terminal:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

The compose file contains a development-only database password. `/v1/readyz` reports ready only while the worker heartbeat is fresh.

### 11.5 Production deployment

The repository does **not** contain a production deployment definition. Specifically:

- No `Dockerfile` exists, and `docker-compose.yml` defines only PostgreSQL. The compose fragment shown in [`docs/final_report.md`](docs/final_report.md) Section 9.3 uses `build: .` and therefore cannot be built from the repository as committed.
- Schema creation uses `Base.metadata.create_all`; no migration tooling is present.
- CORS is hard-coded to `http://localhost:5173` in `app/main.py`; any hosted frontend origin requires a code change.
- TLS termination, secret management, encrypted storage, backup and network policy are not provided by the repository.

Minimum requirements for any non-development deployment: `TRACEX_ENV=production`, a unique `TRACEX_SECRET_KEY`, `TRACEX_ALLOW_DEV_ACTOR_HEADER` unset or `0`, a non-default database password, a TLS-terminating reverse proxy, an encrypted evidence volume, and a single active worker per host (see Section 17).

## 12. API reference

All routes are prefixed `/v1`. FastAPI generates interactive OpenAPI documentation at `/docs` and `/openapi.json` by default (the application does not disable it); this was not verified by execution. Authenticated routes require `Authorization: Bearer <token>`. For case-scoped routes, a caller who is not a member of the case receives `404`, not `403`.

| Method and path | Auth / role | Behavior |
| --- | --- | --- |
| `POST /auth/signup` | none | Body `display_name`, `password` (minimum 8 characters). `201` with `token`, `actor`; `409` if the name exists |
| `POST /auth/login` | none | `200` with token; `401` on invalid credentials |
| `GET /healthz` | none | Checks database and evidence-vault writability; `503` if the vault is unavailable |
| `GET /readyz` | none | Adds worker heartbeat freshness; `503` if absent or stale |
| `GET /cases` | member | Cases of the caller with role |
| `POST /cases` | authenticated | Body `name`, `synthetic` (bool). Caller becomes `case_lead`; writes an audit record |
| `GET /cases/{case_id}` | member | Case detail and members |
| `POST /cases/{case_id}/members` | `case_lead` | Body `actor`, `role` in `case_lead`, `analyst`, `reviewer`. `403` for other roles |
| `GET /cases/{case_id}/sources` | member | Source metadata including SHA-256 and size |
| `POST /cases/{case_id}/imports` | member | Multipart `file`; required `Idempotency-Key` header. Accepts `.csv`, `.json`, `.ndjson`, `.xml`. `202` with `job_id`, `source_id`; `422` on rejected upload; `409` if the key was used with a different source |
| `GET /jobs/{job_id}` | member | State, stage, attempt, lease, exact row counters, snapshot id, error fields |
| `GET /cases/{case_id}/events` | member | SSE replay. Use `Last-Event-ID` or `after` (not both, else `400`); `follow=true` adds heartbeats |
| `GET /cases/{case_id}/graph` | member | Query `seed` (required), `depth` 1–5, `node_limit` 1–1000, `edge_limit` 1–3000. `409` if no completed graph; `422` on invalid parameters |
| `GET /evidence/{source_id}/records` | member of the source's case | Query `locator`; returns the raw record replayed from the original file; `404` if not found; `409` if the stored source cannot be parsed |
| `GET /cases/{case_id}/findings` | member | Query `limit` (1–200, default 50), `offset`; response includes `method`, `methods`, `ml_enabled` derived from the rows returned |
| `GET /findings/{finding_id}/evidence` | member | Finding, feature vector, source references, coverage, opposing evidence, review and audit history, `replay_contract` |
| `POST /findings/{finding_id}/reviews` | `case_lead` or `reviewer` | Body `expected_finding_version`, `disposition` in `open`, `triaged`, `dismissed`, `escalated`, `needs_data_review`; `reason`; optional `counterevidence_refs` (each with `evidence_id`, `locator`, validated against sources of the same case). `409` on stale version; `403` for other roles |
| `POST /findings/{finding_id}/chat` | member | Body `question`, optional `history`; `503` if the Ollama endpoint is unreachable |
| `GET /cases/{case_id}/findings/export` | member | Case-scoped JSON bundle with limitations statement |
| `GET /cases/{case_id}/features/export` | member | Address-window feature rows; optional `snapshot_id` |
| `POST /cases/{case_id}/synthetic-review-seeds` | member; case must have `synthetic: true` | Adds explicit synthetic seed context to a completed snapshot; `422` otherwise |

## 13. Data contracts and examples

### 13.1 Accepted input

Each record must be an object (CSV: one row). Array-valued fields in CSV are JSON arrays encoded in the cell. Only the fields below are read by the normalizer (`normalize.py`).

| Field(s) | Rule |
| --- | --- |
| `txid` | Required; 64 hexadecimal characters (lower-cased) |
| `network` | `bitcoin-mainnet`, `bitcoin-testnet`, `bitcoin-regtest` or `unknown` (default) |
| `timestamp` or `observed_at` | ISO-8601 with an explicit timezone; stored as source timestamp with meaning `unknown` |
| `block_time`, `block_height`, `block_hash` | Optional; `block_time` sets the timestamp meaning to `block_time` |
| `fee` (BTC) or `fee_sats` | Optional; non-negative; BTC values converted to integer satoshis |
| `output_addresses[]` with `output_amounts[]`, or `outputs[]` | Outputs required; array lengths must match; amounts in BTC (or `amount_sats` in `outputs[]`) |
| `input_addresses[]` with `input_amounts[]`, or `inputs[]` | Optional; `inputs[]` items may carry `prev_txid`, `prev_vout`, `address`, `amount`/`amount_sats` |
| `script_type` | Optional |
| `src_ip`, `dst_ip`, `src_port`, `dst_port`, `geo_country`, `asn`, `observer_id` | Optional; stored as a separate network observation; ports must be within 0–65535 |

Rows failing validation are quarantined with a reason and a source locator rather than dropped. A second row with an already-seen `txid` within the same import is quarantined as a duplicate or, if its content differs, as a conflicting variant. Locators: `record:<n>` for CSV, JSON Pointer for JSON and NDJSON, `/<tag>[<n>]` for XML.

The canonical record contract is defined by JSON Schemas in [`schemas/v1/`](schemas/v1/README.md) with valid examples in [`examples/`](examples/). The committed test checks example structure against the schemas' required and permitted properties; no runtime JSON Schema validation library is used by the application.

### 13.2 Repository fixtures (all synthetic)

| Fixture | Path | Notes |
| --- | --- | --- |
| Phase 0 demonstration fixture (100,000 rows) | [`fixtures/demo_100k/`](fixtures/demo_100k/README.md) | Regenerated deterministically; `python3 fixtures/demo_100k/generate.py --verify` |
| Motif fixture | [`fixtures/phase4_1/motifs.ndjson`](fixtures/phase4_1/README.md) | Peeling path and equal-output structure |
| 10K smoke fixture | [`fixtures/ml_smoke_10k/`](fixtures/ml_smoke_10k/README.md) | `python3 fixtures/ml_smoke_10k/generate.py --verify` |
| Generator v2, 100,000 rows, four formats | [`fixtures/phase5a_100k/`](fixtures/phase5a_100k/README.md) | `make dataset` writes `datasets/phase5a_100k/` (gitignored, about 350 MB) |

None of these is official SIH data or real blockchain data. `evaluation_truth.json` is for evaluation only and must never be uploaded.

## 14. Testing and validation

### 14.1 Available tests

| Scope | Files | Make target |
| --- | --- | --- |
| Phase 0 contract (stdlib only): schemas, examples, manifest, fixture regeneration | `tests/test_phase_zero.py` | `make phase-zero` |
| Foundation: case isolation, upload, jobs, idempotency, lease reclaim | `test_phase_one.py`, `test_auth_and_case_reads.py` | `make phase-one` |
| Ingestion, quarantine, XML safety | `test_phase_two.py` | `make phase-two` |
| UTXO graph correctness | `test_phase_three.py` | `make phase-three` |
| Deterministic findings and review | `test_phase_four.py`, `test_phase_four_one.py` | `make phase-four` |
| 10K smoke and feature contract | `test_phase_five_a_smoke.py` and `phase5a`/`phase5b` tests | `make phase-five-a-smoke` |
| Anomaly stack, causality, ML integration | `test_anomaly_stack.py`, `test_ml_pipeline_integration.py`, `test_phase5a_*` | `make anomaly-stack-test` |
| Crash and retry, offline and cross-case access | `test_phase_six_crash_retry.py`, `test_phase_six_offline_and_access.py` | `make phase-six-test` |
| Offline process boot, dependency pinning, export honesty | `test_phase_seven_offline_launch.py` | `make phase-seven-test` |
| Frontend end-to-end smoke (Playwright, requires Chrome and running servers) | `frontend/e2e/smoke.mjs` | `npm run e2e` |

Full suite command documented by the project: `uv run --extra ml pytest -q`. A static count finds 117 `test_` functions under `tests/`; the project documents report 130 (`docs/final_report.md`) and 128 (`docs/phase7.md`) passing, a difference not reconciled here (parametrization may account for it).

Important qualification: every committed pytest module, benchmark script and walkthrough script inspected constructs its control-plane database with **SQLite** (`make_engine("sqlite:///...")`). PostgreSQL-specific behavior (`FOR UPDATE`, `SKIP LOCKED` row locking, concurrent workers) is therefore not exercised by any committed test or benchmark.

### 14.2 Results obtained while preparing this document

| Command | Outcome |
| --- | --- |
| `python3 fixtures/demo_100k/generate.py --verify` | printed `fixture verification passed` |
| `python3 -m unittest discover -s tests -v` | 4 tests run, all `ok`, about 27 s |
| `python3 -m compileall -q app workers scripts fixtures tests` | no errors |

### 14.3 Not executed here

The pytest suite, `ruff`, all `uv`-based Make targets, the anomaly evaluation, the benchmarks, the frontend build and lint, the Playwright smoke test, and Docker/PostgreSQL startup were not executed in the documentation environment (FastAPI, PyArrow, DuckDB and pytest were not installed and network access was disabled). Results quoted in Section 15 are those recorded by the project, not reproduced here.

## 15. Experimental evaluation and performance

### 15.1 Anomaly ranking

**Dataset.** Synthetic, generator v2 (`fixtures/phase5a_100k/generate.py` 2.0.0, seed `tracex-phase5a-prep-100k-v2`, network label `bitcoin-regtest`), 100,000 canonical transactions. Generator and config SHA-256 values are in [`experiments/model_decision.md`](experiments/model_decision.md).

**Population and splits.** Three time-ordered splits (`train_reference`, `validation`, `final_holdout`) assigned from transaction timestamps. Final-holdout labelled counts recorded in [`experiments/releases/anomaly-stack-v1.json`](experiments/releases/anomaly-stack-v1.json): 2,779 positives, 3,125 labelled near-miss negatives, 1,375 transactions inside labelled surge episodes. Candidate selection used validation; the holdout was evaluated once afterwards, per the project's protocol.

**Baseline.** The deterministic Phase 4.1 predicate reimplemented as a score (`rule_baseline` in `app/ml/layers.py`).

**Tasks and metrics.** Average precision (tie-aware) at a 1% review budget for three tasks: `motif` (CoinJoin-like and peel-step positives against all transactions), `surge` (transactions in labelled burst episodes), `discrimination` (true motifs against near-miss negatives only).

**Final-holdout results, deployable unsupervised configuration** (as recorded in `docs/anomaly_stack.md`):

| Task | Best candidate | Average precision | Rule baseline AP | Ratio |
| --- | --- | ---: | ---: | ---: |
| surge | `A_global+D_burst` | 0.5927 | 0.2388 | 2.48 |
| motif | `A_global+D_burst` | 0.6282 | 0.3799 | 1.65 |
| discrimination | rule baseline | 0.9569 | 0.9569 | 1.00 |

No unsupervised combination exceeded the rule on `discrimination`. A supervised comparator (layer S, trained on generator labels) reached AP 0.9961 on that task and is reported by the project as an upper bound, not a deployable result.

**Hardware.** The holdout was run on a MacBook Pro whose memory is recorded as 32 GB in `experiments/model_decision.md` and as 36 GB in `docs/final_report.md` (about 50 s, peak RSS about 871 MB). Timings on a 12-thread Intel i5 host with 15 GiB RAM are in `docs/anomaly_stack.md` (about 19 s, peak RSS 565 MB for the unsupervised stack).

**Threats to validity.**

1. Labels are the generator's own scenario families, and the `motif` positives are close to the deterministic predicate; the project itself characterizes this task as near-circular. Only `discrimination` separates a threshold from a model, and there the rule wins.
2. Layer D's family membership is derived from structural predicates, so the ranking is not independent of the rule.
3. The fixture is synthetic; the numbers say nothing about real Bitcoin data.
4. The raw holdout report (`experiments/runs/anomaly_stack_holdout.json`) is gitignored. The committed release manifest records its SHA-256 (`84e9c111…ab17e`), but the file itself is not in the repository.
5. Single fixture, single seed; no confidence intervals are reported.

**Single-instance demonstration** ([`docs/phase7.md`](docs/phase7.md), Section 3): on the first 5,000 rows of the fixture, a labelled benign near-miss transaction ranked 7 and a labelled CoinJoin-like transaction ranked 56 under the unsupervised model, and both fired the deterministic rule. This is one illustrative pair, not a measured rate. It demonstrates why analyst review is required.

### 15.2 Ingestion throughput and query latency

Recorded in [`docs/phase6.md`](docs/phase6.md) on an Arch Linux host (Intel Core i5-13420H, 8 physical and 12 logical CPUs, 15 GiB RAM, 15 GiB swap, no usable GPU driver), using the real HTTP API and worker path with a **SQLite** control database, ML findings disabled, on the generator-v2 100K fixture (126,004,521 bytes, 100,000 rows accepted, 0 quarantined).

| Stage | Seconds |
| --- | ---: |
| Upload | 0.391 |
| Source verification, parse and commit | 11.36 |
| Graph build (589,987 nodes, 970,260 edges) | 27.05 |
| Deterministic findings and features (49,240 findings) | 150.03 |
| **Ingest total** | **188.45** |

Bounded graph queries (210 requests, 200 timed, default caps): p95 215.1 ms with one reader and 481.2 ms with four readers; no failures. The 1,000,000-row synthetic run was terminated by the operating-system out-of-memory killer at about 10.3 GiB resident memory and did not complete. The project's stated architecture targets were not all met (first ranked lead 188.4 s at 100K against a 20 s target); the target profile hardware was never measured. No throughput or latency claim is made for other hardware, larger data, or PostgreSQL.

### 15.3 Crash and retry

Two interruption points (before the atomic fragment rename; after rename but before receipt commit) are exercised by `tests/unit/test_phase_six_crash_retry.py`, which raises `KeyboardInterrupt` at the exact write step. The project reports no duplicated or lost fragments after lease reclaim. This is simulated interruption on SQLite, not process-kill testing on PostgreSQL.

## 16. Security, privacy and evidence integrity

### 16.1 Implemented controls (verified in source)

| Area | Control |
| --- | --- |
| Authentication | PBKDF2-HMAC-SHA256, 260,000 iterations, random salt; HMAC-SHA256 signed tokens with expiry (default 24 h); constant-time comparisons |
| Startup guard | With `TRACEX_ENV` other than `development`, startup fails if the secret key is the public default |
| Development bypass | `X-TraceX-Actor` header is honored only when `TRACEX_ALLOW_DEV_ACTOR_HEADER` is enabled; disabled by default |
| Authorization | Case membership checked server-side on every case, job, evidence, finding, event and export route; non-members receive `404`; role checks for member management and review |
| Input handling | Pydantic field constraints; extension allowlist; sanitized filenames; upload size limit; XML DTD and entity rejection with `defusedxml`; parameterized SQL and DuckDB queries; caps on graph and page sizes; client-supplied filesystem paths are never accepted, and stored paths are resolved and confined to the evidence root |
| Source integrity | Streaming SHA-256 during upload; atomic move; existing target digest verified rather than overwritten; hash and size re-verified by the worker before ingestion; fragments hashed and receipt-approved |
| Provenance | Every canonical fact and finding carries source locators tied to a source hash; original records replayable |
| Audit | `AuditRecord` entries for case creation, member changes, source preservation, import queueing, synthetic seeds and finding reviews; review history retains prior review references |
| Review integrity | Optimistic concurrency on finding version; counter-evidence references must belong to the same case |
| Offline behavior | Tests install a socket guard that rejects non-`AF_UNIX` sockets and then boot the application and run an import, graph query and findings path; frontend fonts are bundled locally |

### 16.2 Distinctions that must be preserved

- **Case isolation is application-level.** It is enforced by membership checks in one shared database and one shared filesystem root. There is no per-case physical, operating-system or cryptographic isolation.
- **Hashes and audit rows are not a proven chain of custody.** SHA-256 values and database audit records support integrity checking, but audit rows are ordinary mutable rows (not hash-chained or signed), exports are not signed, and no filesystem-level immutability (for example WORM storage) is enforced by the code.
- **"Offline" means the tested application workflow made no outbound socket connections under a test guard.** It is not an independently verified air-gapped deployment. Installation requires pre-provisioned dependencies, and the optional chat feature contacts an Ollama endpoint that may be on another machine.

### 16.3 Gaps and hardening requirements

| Observation | Implication |
| --- | --- |
| No rate limiting or lockout on `/auth/login` and `/auth/signup`; no token revocation or MFA found in source | Add controls before exposure beyond a trusted network |
| Session token is stored in browser `localStorage` (`frontend/src/lib/auth.tsx`) | Susceptible to theft through script injection; consider alternative session handling for production |
| Evidence vault is unencrypted by the application | Use an encrypted volume; documentation advises this but the code does not enforce it |
| Development database password is committed in `docker-compose.yml` and as a default URL | Development use only |
| No TLS in the application; no container images | Terminate TLS in a proxy; build a deployment definition |
| Uploads are validated by extension, not content sniffing | Content is parsed by format-specific readers and invalid content is rejected or quarantined |
| `GET /evidence/{source_id}/records` scans the stored source linearly per request | Authenticated request cost grows with file size |
| Upload declares every source `synthetic=false` regardless of case flag (`routes.py`) | The synthetic flag is only meaningful at case level |
| A lead may add a member who has not signed up; that creates a user record without a password, and `/auth/signup` then returns `409` for the name | Code-reading observation, not executed. Members should register first |
| Chat assistant: grounding is best-effort through a system prompt | Answers are not verified statements; no committed test located |
| Job lease (default 30 s) is renewed only at batch commits, while graph, findings and ranking run afterwards without renewal (about 177 s on the 100K fixture) | A second worker could reclaim an active job; the project recommends one worker per host |
| Weak-data and partial coverage are surfaced as coverage fields, not blocked | Reviewers must read coverage before relying on a finding |

## 17. Limitations and assumptions

- **Data.** All evaluation is on synthetic data with generator-defined labels. The official SIH-linked dataset has not been inspected; its schema, licence, labels and outpoint availability are unverified.
- **Spend lineage.** Where `prev_txid`/`prev_vout` are absent or refer outside supplied coverage, lineage is partial; the walkthrough fixture has 900 of 6,932 inputs without outpoints.
- **Time.** Generic timestamps have unknown meaning; window features treat them as source-observed times. Activity outside the supplied data is unknown, and no dormancy claim is made.
- **Heuristic false positives.** Payroll, exchange batching, consolidation and treasury sweeps can satisfy the same predicates. The synthetic fixture includes such cases by construction and shows the rule and the model both rank them highly.
- **Model limits.** The unsupervised ranking does not separate a benign rule-satisfying transaction from a true motif (Section 15.1). Calibration depends on the snapshot's own first 70% of transactions. Scores are not probabilities.
- **Attribution.** TraceX does not attribute addresses to persons or entities. Address, script, IP and ASN values are observations. Graph proximity is not common ownership.
- **Scale.** The graph builder and findings stage hold snapshot-wide structures in memory. One million rows did not complete on a 15 GiB host; a streaming rewrite is required.
- **Scoring scales.** Deterministic findings mix 0–100 and 0–1 scores in a single rank ordering (Section 8.3).
- **Graph cursor.** A continuation cursor is emitted but cannot be consumed through the API.
- **Storage engine.** Behavior on PostgreSQL under concurrency is untested by the committed suite.
- **Production readiness.** Not claimed. See Sections 11.5 and 16.3.
- **Interpretation.** Suspicious patterns and anomaly scores are investigative leads requiring human review. They are not proof of criminal conduct or identity.

## 18. Demonstration workflow

All data used below is synthetic. Expected outputs are those described in the project documents; they were not reproduced while preparing this README.

**Option A — scripted walkthrough (requires the environment in Section 11.4 with `--extra ml`):**

```bash
make dataset                    # generate the synthetic 100K fixture (about 35 s, about 350 MB)
make phase-seven-walkthrough    # drives the real API and worker on the first 5,000 rows
```

The script (`scripts/phase7_case_walkthrough.py`) uploads a slice of `datasets/phase5a_100k/ingestion_rows.ndjson`, reads progress events, queries the graph, opens a deterministic finding and a model-ranked finding, replays source records by locator, records two review decisions, and requests the case export. It writes `experiments/runs/phase7_case_walkthrough.json` (gitignored). Documented expectations are in [`docs/phase7.md`](docs/phase7.md) Sections 2 and 3.

**Option B — manual, through the UI or API:**

1. Start PostgreSQL, API, worker and frontend (Section 11.4), sign up, and create a case with `synthetic: true`.
2. Generate the fixture with `make dataset` and upload `datasets/phase5a_100k/ingestion_rows.ndjson` (or a smaller file such as the 10K fixture) with an `Idempotency-Key`.
3. Poll `GET /v1/jobs/{job_id}` or the case event stream until `state` is `completed`; check `rows_seen == rows_accepted + rows_quarantined`.
4. Open `GET /v1/cases/{case_id}/findings`, then `GET /v1/findings/{finding_id}/evidence`, then `GET /v1/evidence/{source_id}/records?locator=<locator from source_refs>`.
5. With a `reviewer` or `case_lead` account, `POST /v1/findings/{finding_id}/reviews` using the current `finding_version`; then fetch `GET /v1/cases/{case_id}/findings/export`.

Full ingestion of the 100K fixture took about 188 s on the benchmark host recorded in `docs/phase6.md`; a 10K fixture (`make phase-five-a-smoke`) is smaller.

## 19. Screenshots and diagrams

The repository contains no screenshots, presentation deck or demonstration video. The only image asset is the logo at `frontend/src/assets/tracex-logo.png`.

- `TODO: add committed screenshots of the Case Dashboard, Graph Explorer, Findings Feed and Evidence Package pages (paths to be added only after files exist)`
- `TODO: add link to the presentation deck and demonstration video, which docs/final_report.md states are separate artifacts outside this repository`

Architecture and flow diagrams are in Sections 6 and 7 (Mermaid, colour-coded, 22 px base font).

## 20. Contributing, license, acknowledgments, contact

- **License.** MIT License, see [`LICENSE`](LICENSE) (copyright line as written in that file).
- **Contributing.** No contribution guide is present. `TODO: add if applicable`
- **Acknowledgments.** `TODO: to be supplied by the team`
- **Contact.** `TODO: to be supplied by the team`

## 21. Documentation references

| Document | Content |
| --- | --- |
| [`docs/requirements.md`](docs/requirements.md) | Requirement register, authority and data-meaning rules |
| [`docs/hardware.md`](docs/hardware.md), [`docs/environment.manifest.json`](docs/environment.manifest.json) | Measured host and offline runtime constraints |
| [`docs/phase1.md`](docs/phase1.md) to [`docs/phase4.md`](docs/phase4.md) | Foundation, ingestion, graph, deterministic findings |
| [`docs/phase5a_handoff.md`](docs/phase5a_handoff.md) | Frozen feature contract |
| [`docs/anomaly_stack.md`](docs/anomaly_stack.md) | Anomaly stack design and measured results |
| [`docs/phase6.md`](docs/phase6.md) | Performance, recovery, security measurements |
| [`docs/phase7.md`](docs/phase7.md) | Submission proof package |
| [`docs/final_report.md`](docs/final_report.md) | Phase-by-phase project report and deployment notes |
| [`experiments/protocol.md`](experiments/protocol.md), [`experiments/phase5b_protocol.md`](experiments/phase5b_protocol.md) | Evaluation protocols |
| [`experiments/model_decision.md`](experiments/model_decision.md), [`experiments/releases/anomaly-stack-v1.json`](experiments/releases/anomaly-stack-v1.json) | Frozen release decision and manifest |
| [`schemas/v1/README.md`](schemas/v1/README.md) | Canonical schema notes |
| [`frontend/README.md`](frontend/README.md) | Frontend notes |

Known inconsistencies among these documents, to be resolved by the authors:

- `docs/final_report.md` states that frontend implementation was out of scope, although `frontend/` exists; the frontend README refers to 10 routes, while 9 are defined in `frontend/src/App.tsx`.
- `docs/final_report.md` and `docs/phase6.md` refer to SQLite errors, consistent with the benchmark harness using SQLite, whereas the documented control plane is PostgreSQL.
- The git commit in the text of `experiments/model_decision.md` differs from the one in `experiments/releases/anomaly-stack-v1.json`.
- MacBook memory is stated as 32 GB in `experiments/model_decision.md` and 36 GB in `docs/final_report.md`.
- Passing-test totals differ between `docs/final_report.md` (130) and `docs/phase7.md` (128).

## 22. Documentation verification record

Prepared by static audit of the complete source archive on 2026-09-30. Verified by reading: application source, routes, models, configuration, auth, storage, ingestion, graph, detectors, ML modules, tests, scripts, frontend manifests and pages, Makefile, compose file, fixtures, schemas and all project documents. Commands executed and their outcomes are listed in Section 14.2; everything in Section 14.3 remains to be run by the team. The external reference repository named in the documentation brief was not accessible and was not inspected. No source file other than this README was modified. The download commands in Section 11.2 and the diagrams in Sections 6 and 7 were added or redesigned in later revisions and were not executed or rendered in the documentation environment; the diagrams were checked against `app/storage/raw.py`, `app/jobs/service.py`, `app/engine/catalogue/fragments.py`, `app/engine/graph/builder.py` and `docker-compose.yml`.
