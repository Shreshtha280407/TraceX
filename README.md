<div align="center">

<img src="docs/assets/readme/hero.gif" alt="TraceX animated identity: an illustrative transaction flow moves through preserve, reconstruct, rank and review" width="100%">

# TraceX

### Follow the funds. Keep the evidence.

**An offline-capable Bitcoin intelligence and investigation platform.**

Turn supplied transaction and network metadata into a UTXO graph, explainable patterns, ranked anomalies, and evidence-backed investigation groups. Inspect the original records, record a reasoned decision, and export the case trail from one local console.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=white)](frontend/package.json)
[![Scorer](https://img.shields.io/badge/scorer-anomaly--stack--v2-E3A62F?style=flat-square&labelColor=1C1B17)](docs/grouped_review_2026-10-04.md)
[![Local analysis](https://img.shields.io/badge/analysis-local_CPU-7FAE63?style=flat-square&labelColor=1C1B17)](deploy/appliance/README.md)
[![License](https://img.shields.io/badge/license-MIT-A8A08C?style=flat-square&labelColor=1C1B17)](LICENSE)

[**Watch the investigation**](#watch-a-real-investigation) · [**Run TraceX**](#quickstart) · [**See the architecture**](#how-it-works) · [**Check the results**](#measured-results) · [**Explore the docs**](#documentation)

<sub>Package version 0.1.0 · Documentation updated 5 October 2026</sub>

</div>

---

## In one minute

- **Start with a case.** Access, imports, findings, review history, and exports are scoped to case membership.
- **Preserve before processing.** The original upload gets a SHA-256 hash; canonical facts retain locators that reopen the source record.
- **Follow actual spend links.** A UTXO spend edge requires a resolved previous output. Missing lineage stays visible.
- **Combine structure and statistics.** Transparent pattern detectors work alongside global Isolation Forest scoring and population burst analysis.
- **Review bounded episodes.** Investigation groups preserve their member findings, while the queue and backlog expose the actual workload.
- **Work locally.** Analysis, graph queries, scoring, and prepared GeoIP resources run on the host. The current runtime has no LLM or chat dependency.

> A TraceX finding is an investigative lead. Scores, addresses, clusters, and relay observations require interpretation by a person; they do not establish criminality or identity.

## Watch a real investigation

These are **Playwright recordings of the running application**, using a real FastAPI API and worker on a fresh, disposable SQLite database. The source contains **1,000 synthetic transactions**. Product responses are not mocked. The absent GeoIP cache remains explicitly degraded; this recording is a product walkthrough, not PostgreSQL, offline-isolation, or scale acceptance.

### 01 · From source file to case intelligence

Create a synthetic case, select the deployed unsupervised procedure, upload the source, and watch the worker produce persistent findings and investigation groups.

![Real browser recording: TraceX signup, synthetic case creation, evidence upload, worker stage progression and the resulting case dashboard](docs/assets/readme/intake.gif)

<sub>Real browser capture · Synthetic evidence · Persisted API and worker results</sub>

### 02 · Follow the verified UTXO path

Open a stored peeling-chain candidate, inspect its observed spend sequence, change zoom, and trace a source into its direct inputs and outputs.

![Real Graph Explorer recording: select a stored peeling-chain candidate, inspect its transaction and output path, adjust zoom and trace a source-centred fund-flow view](docs/assets/readme/graph.gif)

<sub>The displayed risk label describes a detector signal.</sub>

### 03 · Reopen the evidence. Record the decision.

Adjust the review capacity, open an investigation proposition, replay an original source record, save a group decision, and download a real JSON evidence export.

![Real review recording: grouped queue controls, investigation details, original record replay, persisted group triage, network context and a successful JSON evidence download](docs/assets/readme/review.gif)

<sub>Original-record replay → Independent group decision → Evidence export</sub>

The captured dashboard, **before the recorded triage decision**, shows:

| Canonical transactions | Underlying findings | Investigation groups | In the review queue | Additional unresolved |
|:---:|:---:|:---:|:---:|:---:|
| **1,000** | **184** | **140** | **100** | **40** |

These counts belong to this synthetic fixture. The 100-item capacity limits the current queue; the backlog remains accessible. [Capture manifest](docs/assets/readme/capture-manifest.json).

### The console · real screenshots

<table>
<tr>
<td width="50%" valign="top"><img src="docs/assets/readme/dashboard.png" alt="Real TraceX case dashboard with 1,000 transactions, 184 observations, 140 groups, 100 queued and 40 backlog" width="100%"><br><b>Case dashboard</b><br><sub>Evidence counts, grouped workload, model identity and recent case events.</sub></td>
<td width="50%" valign="top"><img src="docs/assets/readme/graph.png" alt="Real UTXO graph showing a synthetic peeling candidate, verified spend links, output amounts and evidence context" width="100%"><br><b>Graph explorer</b><br><sub>Transaction paths, output values, source tracing and coverage.</sub></td>
</tr>
<tr>
<td valign="top"><img src="docs/assets/readme/groups.png" alt="Real Findings Feed with durable investigation groups and separate review-capacity controls" width="100%"><br><b>Investigation queue</b><br><sub>Group propositions, independent dispositions and an accessible backlog.</sub></td>
<td valign="top"><img src="docs/assets/readme/evidence.png" alt="Real finding evidence package with its procedure, supporting observations and source references" width="100%"><br><b>Evidence package</b><br><sub>Source-linked claims, coverage, alternatives and reviewer history.</sub></td>
</tr>
<tr>
<td valign="top"><img src="docs/assets/readme/network.png" alt="Real Network Intelligence screen showing supplied relay observations and an explicit missing offline GeoIP cache" width="100%"><br><b>Network intelligence</b><br><sub>Observed endpoint context; missing GeoIP enrichment is visible.</sub></td>
<td valign="top"><img src="docs/assets/readme/export.png" alt="Real generated evidence export preview with JSON download controls" width="100%"><br><b>Case export</b><br><sub>A portable record of findings, evidence references, reviews and audit history.</sub></td>
</tr>
</table>

All stills come from the same successful browser run.

## Why TraceX exists

Bitcoin exports contain transaction facts. Investigation requires connecting those facts without losing their meaning or provenance.

| Investigation challenge | TraceX response |
|---|---|
| Transactions, outputs and network observations arrive in separate records | Canonical normalization and a case/snapshot-scoped UTXO graph |
| Missing prevouts make spend lineage incomplete | Explicit coverage; only resolved outpoints create spend edges |
| Interesting structures also occur in ordinary business activity | Transparent thresholds, inspectable paths, benign alternatives and human review |
| A ranking alone cannot explain its evidence | Versioned procedures, supporting observations, source hashes and exact record locators |
| Thousands of observations can overwhelm a reviewer | Bounded episode groups, configurable queue capacity and a separately accessible backlog |
| A reviewer must preserve disagreement and context | Independent group/finding decisions, recorded reasons, opposing references and review history |
| A disconnected environment cannot call cloud analysis services | Local CPU analysis and an appliance with prepared dependencies, UI assets and GeoIP resources |

## Quickstart

**Requirements:** Python 3.11+, `uv`, Git, Node.js/npm, and Docker with Compose for PostgreSQL. Dependency installation and optional GeoIP acquisition happen on a connected machine. The analysis pipeline runs locally after preparation.

### 1 · Install and initialize

```bash
git clone https://github.com/Shreshtha280407/TraceX.git
cd TraceX

uv sync --frozen --extra dev --extra ml
npm --prefix frontend ci
docker compose up -d --wait postgres

export TRACEX_DATABASE_URL='postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5433/tracex'
uv run tracex-init-db
```

The development Compose file exposes PostgreSQL on **5433**. The explicit URL below must be used by both API and worker.

### 2 · Start three terminals

**API**, from the repository root:

```bash
TRACEX_DATABASE_URL='postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5433/tracex' \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

**Worker**, from the repository root:

```bash
TRACEX_DATABASE_URL='postgresql+psycopg://tracex:tracex-dev-only@127.0.0.1:5433/tracex' \
  uv run tracex-worker
```

**Console**, from the repository root:

```bash
npm --prefix frontend run dev
```

| Open | Purpose |
|---|---|
| **http://localhost:5173** | Sign up, create a case and begin an investigation |
| **http://127.0.0.1:8000/docs** | Interactive API documentation |
| **http://127.0.0.1:8000/v1/healthz** | Database and evidence-vault health |
| **http://127.0.0.1:8000/v1/readyz** | Worker readiness; requires a recent heartbeat |

### 3 · Investigate a small synthetic source

For a compact deterministic-path example, upload [`fixtures/phase4_1/motifs.ndjson`](fixtures/phase4_1/motifs.ndjson). Its small size does not exercise the full ML reference population.

For a richer, 1,000-transaction walkthrough, generate a **new** temporary fixture directory:

```bash
uv run python -m scripts.bounded_study_fixture \
  --output /tmp/tracex-demo-1000 --rows 1000 --seed tracex-local-walkthrough
```

Create a case with **Synthetic/demo data** checked, select **Unsupervised v2**, and upload `/tmp/tracex-demo-1000/ingestion_rows.ndjson` through Evidence Intake. Only the source file is imported; generated evaluation truth is separate.

Prepare the optional local GeoIP database **before disconnecting** if the demo needs country/ASN enrichment:

```bash
uv run tracex-geoip download
uv run tracex-geoip status
```

A missing cache or insufficient evidence is reported as degraded coverage. Check analysis-stage outcomes as well as import completion.

<details>
<summary><b>Install the offline Linux appliance</b></summary>
<br>

Build the transfer bundle on a connected machine:

```bash
scripts/build_offline_bundle.sh
```

On the prepared Linux host:

```bash
tar xzf tracex-offline-<version>.tar.gz
cd tracex-offline
./install.sh
```

The bundle contains the application image, compiled React UI, Python/ML dependencies and prepared GeoIP resources. The installer loads images, creates fresh secrets and starts PostgreSQL, API and worker.

For strict native-Linux isolation, open the **host-private API bridge URL printed by the installer**. Docker Desktop needs a different routing choice; enabling its published-port opt-out changes API egress isolation. Details: [appliance installation](deploy/appliance/README.md), [MacBook runbook](docs/macbook_runbook.md).

</details>

## How it works

<img src="docs/assets/readme/architecture.svg" alt="TraceX architecture: React console calls FastAPI; a separate durable worker performs analysis; both coordinate through PostgreSQL control metadata and a shared evidence volume" width="100%">

The API handles access, uploads, bounded reads and reviewer decisions. A separate worker performs heavy computation. They coordinate through job and stage metadata in the control database and files in the evidence volume. The appliance uses PostgreSQL; small development and test paths can use SQLite.

| Stage | What becomes reviewable |
|---|---|
| **Preserve** | Original source bytes, SHA-256, size, source identity and upload/job metadata |
| **Normalize** | Integer-satoshi facts, meaningful timestamps, exact source locators and quarantine reasons |
| **Connect** | DuckDB UTXO snapshots built from accepted outputs and verified spend references |
| **Detect** | Deterministic structures, address-window observations and versioned anomaly findings |
| **Enrich** | Proposed entities, behavioral similarity, network context and coverage-aware GeoIP |
| **Group** | Atomically published investigation episodes with immutable member evidence |
| **Review and export** | Reasoned dispositions, history, source replay and case-scoped evidence bundles |

Import completion and analysis completion are distinct. Durable stage rows expose success, failure, reason and attempt. Earlier source receipts and review evidence remain available when a later stage needs recovery.

<details>
<summary><b>UTXO semantics and recovery boundaries</b></summary>
<br>

```mermaid
flowchart LR
    A[Transaction A] -->|CREATES_OUTPUT| O[Output A:vout]
    O -->|SPENT_BY: resolved prevout| B[Transaction B]
    O -->|LOCKED_TO| S[Address or script]
    N[Supplied network observation] -->|OBSERVED_TX| B
```

- `SPENT_BY` requires both `prev_txid` and `prev_vout` to resolve to an accepted output. Missing or conflicting lineage is reported.
- Address participation and relay observation have different meanings. Neither establishes a person or the origin of funds.
- Parquet fragments require database receipts. Unreceipted files are ignored during recovery.
- Source verification, job leases, checkpoints and bounded database retries preserve committed work.
- The disk-indexed grouping executor publishes complete generations atomically and renews PostgreSQL ownership during grouping. This does not establish safe competition between multiple worker services across every long stage.
- Graph continuation cursors bind the query, case and graph snapshot. A displayed neighborhood has declared limits.

Source: [graph builder](app/engine/graph/builder.py), [worker](workers/runner.py), [grouping executor](app/engine/grouping_materializer.py), [recovery report](docs/worker_retry_fix_2026-10-05.md).

</details>

## Intelligence with a traceable basis

### Transparent structural detectors

| Signal | Observable basis | Context a reviewer should consider |
|---|---|---|
| **Peeling-chain candidate** | Verified spend continuation, shrinking outputs and a bounded multi-hop path | Sequential legitimate payments and change handling |
| **CoinJoin-like structure** | Multiple inputs/outputs and repeated equal output values | Privacy collaboration, payroll and batched payouts |
| **Concentrated collection** | Multiple source transactions within an address window | Merchant receipts and deposit addresses |
| **Emerging hub** | Increased collection activity in the observed window | Exchange operations and ordinary service growth |
| **Rapid redistribution** | Inbound activity followed by verified spends within a threshold | Treasury automation and hot-wallet sweeping |

Every signal retains its procedure, evidence references and coverage. Detector strength is interpreted within its own scale. [Pattern implementation](app/engine/motifs/deterministic.py), [address-window rules](app/engine/findings/deterministic.py).

### The deployed anomaly stack

**`anomaly-stack-v2` combines global Isolation Forest and retrospective population burst scoring with equal-weight Stouffer fusion.**

```text
32 transaction features → StandardScaler → global Isolation Forest ─┐
                                                                  ├→ reference-tail transforms
900-second motif buckets → statistical population burst ──────────┘   → Stouffer fusion → retained findings
```

The reference uses the earliest 70% of time-ordered eligible transactions in the completed snapshot. The default retention threshold is the reference 99th percentile; it does not guarantee a 1% output queue. ECOD feature-tail values provide descriptive context. The burst uses completed containing-bucket counts, and reference rows are fitted in sample, so v2 is **retrospective triage**.

New cases default to `auto_eligible`: an installed candidate must pass its declared approval/domain checks; otherwise v2 remains the fallback. The reported supervised comparisons did not promote a real-case candidate. B/C/E research layers and supervised comparators are outside default v2 fusion. [Release decision](experiments/grouped_review_20261004/final/release-decision.json), [scoring and deployment](docs/recipient_history_and_deployment_2026-10-04.md).

### Entities, network context and seeded exposure

| Analysis | Method | Meaning |
|---|---|---|
| **Proposed entity association** | Common-input union-find; CoinJoin-shaped inputs excluded | A reviewable association heuristic |
| **Similar-wallet search** | Sparse wallet/transaction incidence, degree normalization, truncated SVD, cosine similarity | Similar observed behavior |
| **Network correlation** | Supplied IP/ASN observations, concentration tests and relay-flow context | Off-chain context with shared-relay alternatives |
| **Offline GeoIP** | Prepared DB-IP country / IPtoASN ranges | Location and ASN of an observed endpoint range |
| **Seeded risk propagation** | Analyst seeds, accepted UTXO flows, value-weighted haircut exposure and hop decay | Exposure under a declared modeling assumption |

Source and boundaries: [analytics](app/engine/analytics.py), [GeoIP](app/engine/geoip.py), [confidence applicability](app/engine/confidence.py).

## Human review designed around episodes

The primary review unit is a durable **investigation group**: a bounded proposition with immutable member findings, a pinned case/snapshot/procedure identity and its own decision history.

| Control | Behavior |
|---|---|
| **Queue capacity** | Default 100 unresolved groups; configurable from 0 to 10,000 |
| **Visible backlog** | Additional unresolved groups remain accessible beyond the current queue |
| **Conservative grouping** | Fixed UTC-day episodes, directly supported overlap and fixed representative anchors; at most 256 members per group |
| **Independent decisions** | A group review preserves each member finding's disposition and history |
| **Versioned generations** | New immutable inputs can create linked replacement generations without silently broadening an earlier decision |
| **Contextual review** | Record a reason; cite exact opposing records where applicable; retain unresolved questions |

Triaged, confirmed and dismissed groups free queue capacity. Open, escalated and needs-data groups remain unresolved. A confirmed group decision concerns the stated proposition; it does not label every associated transaction illicit.

Grouping follows `observed-episode-groups-v1`, with family round-robin queue ordering. The active `disk-indexed-grouping-v1` executor changes storage/transport execution while preserving the grouping contract. [Grouping design](docs/grouped_review_2026-10-04.md), [executor parity and speed](docs/grouping_speed_fix_2026-10-05.md), [queue recovery](docs/group_queue_recovery_2026-10-05.md).

## Evidence from source to decision

<img src="docs/assets/readme/evidence-contract.svg" alt="TraceX evidence contract: preserved source bytes and exact locators support versioned claims, which receive independently recorded human reviews" width="100%">

A finding's evidence package brings together:

1. **The proposition:** exact rule/scorer identity, thresholds, features and snapshot.
2. **Supporting observations:** source records, locators and integrity references.
3. **Opposing observations:** checked counter-observations or reviewer-cited records, distinguished from plausible alternatives.
4. **Coverage:** missing lineage, absent network context and calibration applicability.
5. **The review:** disposition, reason, pinned version, reviewer and prior decisions.

CSV locators identify logical records; JSON sources retain pointers/indices; XML retains element paths and ordinals. Raw replay checks the stored source bytes. Amounts normalize to **integer satoshis**, and unknown fields remain `null`.

The case JSON export contains findings, evidence references, reviewer history and audit records. Features also export as JSON or compressed Parquet. These support reproducibility; exports are not cryptographically signed, and ordinary audit rows are not a tamper-proof ledger. [Canonical schemas](schemas/v1/README.md), [structured evidence](app/engine/evidence.py).

## Measured results

Each result below is scoped to its named fixture and report. Historical measurements describe their earlier release; this README does not rerun or relabel them.

| Evidence | Recorded result | Scope and source |
|---|---|---|
| **This README's real UI capture** | **21 checks passed**; 1,000 canonical TX; original hash, group review and UI download verified | Disposable SQLite; synthetic; GeoIP enrichment missing; [capture manifest](docs/assets/readme/capture-manifest.json) |
| **Grouped executor acceptance, 5 Oct** | **7.770 s** upload → authenticated final group/member retrieval; grouping **2.180 s** | Fresh 1,000-TX CSV, real PostgreSQL/API/worker; [executor report](docs/grouping_speed_fix_2026-10-05.md) |
| **Grouped executor regression checks** | **84 audited small backend tests passed** | Reference parity, atomic publication, review preservation and ownership checks; [same report](docs/grouping_speed_fix_2026-10-05.md) |
| **Review-count browser acceptance, 5 Oct** | **67 live browser checks passed** | Separate 128-TX PostgreSQL case; [count correction](docs/review_counts_fix_2026-10-05.md) |
| **Historical million-TX pipeline** | **1,014,581 canonical TX in 1,289.993 s**; worker-tree peak **5,681 MiB** | Earlier PostgreSQL image07, approximately 16 GB RAM, before investigation grouping; [scale report](docs/scale.md) |

**Current grouped-release 1M acceptance remains blocked/not run by resource admission. The 3M target is unverified.** Use the [laptop admission runbook](docs/laptop_1m_runbook.md) before generating or importing a large source.

<details>
<summary><b>ML quality: tasks, populations and the actual deployment decision</b></summary>
<br>

The registered synthetic finals report **labelled-subset average precision (AP)**:

| Final variant | Deployed v2 motif AP | Deployed v2 surge AP | Diagnostic hybrid motif AP | Diagnostic hybrid surge AP |
|---|---:|---:|---:|---:|
| Fresh base | 0.761694 | 0.640035 | 0.961725 | 0.619101 |
| Timing/value shift | 0.768629 | 0.636899 | 0.960171 | 0.514135 |
| Missing network | 0.761694 | 0.640035 | 0.961725 | 0.619101 |

Each final labels **499 of 5,000 eligible transactions**. Variants are correlated copies. Hybrid was frozen for diagnostic transfer, and its surge regression prevented a joint success claim. V2 was retained.

AP is a ranking metric, not overall accuracy. Complete eligible-population labels, applicable group truth and sufficient queue coverage are absent: **group P@100 remains not evaluable**. Neither these scores nor the captured UI establish real-world fraud precision or reduced merchant false positives.

[Grouped quality report](docs/grouped_review_2026-10-04.md) · [Compact final results](experiments/grouped_review_20261004/final/README.md) · [Fail-closed metric reporting](docs/quality_matrix_reporting_2026-10-04.md)

</details>

<details>
<summary><b>Repeat the relevant development checks</b></summary>
<br>

```bash
uv run ruff check app workers scripts tests
uv run pytest -q
npm --prefix frontend run lint
npm --prefix frontend run build
node --test frontend/e2e/graph-window.test.mjs frontend/e2e/investigation-counts.test.mjs
git diff --check
```

The full pytest suite includes fixture-specific tests and may require their documented generated data. Live browser acceptance needs a running backend and a small synthetic source; see [the browser harness](frontend/e2e/acceptance.mjs) and [small acceptance runner](scripts/small_browser_acceptance.py). Frontend lint has reported existing warnings in implementation reports; successful execution is not a warning-free claim.

To regenerate the README recordings with Chrome and FFmpeg installed:

```bash
uv run python scripts/capture_readme.py
node scripts/render_readme_art.mjs
```

The capture starts and stops its own temporary local API/worker and creates a synthetic source in `/tmp`. It writes the README media to `docs/assets/readme/`; the art renderer produces the illustrative cover separately. [Full media guide](docs/readme-media.md).

</details>

## Technology and repository

| Layer | Building blocks |
|---|---|
| **Investigator console** | React 19 · TypeScript 6 · Vite 8 · React Router · locally bundled fonts |
| **API and control plane** | Python 3.11+ · FastAPI · Uvicorn · SQLAlchemy · PostgreSQL/psycopg |
| **Evidence and analytics** | DuckDB · PyArrow · Parquet · ijson · defusedxml |
| **Statistical intelligence** | scikit-learn · NumPy · SciPy · joblib · threadpoolctl |
| **Verification and delivery** | pytest · Ruff · Playwright · Oxlint · Docker Compose · `uv.lock` / npm lockfile |

```text
TraceX/
├── app/
│   ├── api/                  Cases, imports, graph, reviews, groups and analytics
│   ├── auth/                 Passwords, sessions and membership checks
│   ├── engine/               Canonical facts, UTXO graph, patterns and evidence
│   │                         Analytics, GeoIP and disk-indexed grouping
│   ├── ml/                   V2 scoring, frozen candidates and promotion gates
│   ├── jobs/                 Durable stages, leases and recovery
│   └── storage/              Original-source preservation
├── workers/                  Separate analysis worker
├── frontend/                 Investigator console and browser harnesses
├── schemas/v1/               Evidence and canonical data contracts
├── fixtures/                 Synthetic source generators and compact examples
├── scripts/                  Acceptance, admission, research and media tooling
├── experiments/              Scoped model decisions and compact reported results
├── deploy/appliance/         Offline installer and deployment Compose
├── docs/                     Technical guides, implementation reports and media
├── Dockerfile                Compiled UI + Python/ML appliance image
└── docker-compose.yml        Development PostgreSQL
```

## API at a glance

The API uses `/v1` routes and signed bearer sessions. Case reads require server-side membership; non-members receive `404`. Finding/group review decisions require the appropriate case role. Inspect the running `/docs` for complete schemas.

| Surface | Representative route |
|---|---|
| Identity | `POST /v1/auth/signup` · `POST /v1/auth/login` |
| Cases and membership | `GET /v1/cases` · `POST /v1/cases` · `/v1/cases/{id}/members` |
| Evidence intake | `POST /v1/cases/{id}/imports` with an `Idempotency-Key` |
| Progress and readiness | `GET /v1/jobs/{id}` · `GET /v1/cases/{id}/events` · `/v1/readyz` |
| Graph and flow | `GET /v1/cases/{id}/graph` · `/v1/cases/{id}/graph/flow` |
| Findings and evidence | `GET /v1/cases/{id}/findings` · `/v1/findings/{id}/evidence` |
| Original record replay | `GET /v1/evidence/{source_id}/records?locator=…` |
| Grouped review | `GET /v1/cases/{id}/investigation-queue` · `/v1/investigation-groups/{id}` |
| Decisions | `POST /v1/findings/{id}/reviews` · `/v1/investigation-groups/{id}/reviews` |
| Analytics | `/v1/cases/{id}/entities` · `/network` · `/risk` |
| Export | `GET /v1/cases/{id}/findings/export` · `/features/export.parquet` |

Source: [case/evidence routes](app/api/routes.py), [group routes](app/api/group_routes.py), [analysis routes](app/api/analysis_routes.py), [analytics routes](app/api/analytics_routes.py).

## Documentation

<table>
<tr>
<td width="25%" valign="top">
<b>01 / FIRST INVESTIGATION</b><br><br>
<sub>Install the system, prepare a source and follow the console.</sub><br><br>
<a href="#quickstart">Quickstart →</a><br>
<a href="deploy/appliance/README.md">Offline installation →</a><br>
<a href="docs/readme-media.md">Recorded demo guide →</a>
</td>
<td width="25%" valign="top">
<b>02 / INSIDE THE ENGINE</b><br><br>
<sub>Understand the data, spend semantics and scoring procedure.</sub><br><br>
<a href="docs/phase3.md">UTXO graph design →</a><br>
<a href="schemas/v1/README.md">Canonical contracts →</a><br>
<a href="docs/grouped_review_2026-10-04.md">Scoring and group review →</a>
</td>
<td width="25%" valign="top">
<b>03 / EVIDENCE AND OPERATIONS</b><br><br>
<sub>Recover jobs, inspect boundaries and admit real workloads.</sub><br><br>
<a href="docs/worker_retry_fix_2026-10-05.md">Worker recovery →</a><br>
<a href="docs/group_queue_recovery_2026-10-05.md">Queue recovery →</a><br>
<a href="docs/laptop_1m_runbook.md">Resource admission →</a>
</td>
<td width="25%" valign="top">
<b>04 / VERIFY AND EXTEND</b><br><br>
<sub>Review measurements, reproduce checks and work on the code.</sub><br><br>
<a href="docs/grouping_speed_fix_2026-10-05.md">Current executor checks →</a><br>
<a href="docs/scale.md">Historical scale evidence →</a><br>
<a href="docs/README.md">Full documentation map →</a>
</td>
</tr>
</table>

| Follow the implementation | Evaluate the intelligence | Prepare a deployment |
|---|---|---|
| [Integrated implementation](docs/integrated_implementation_2026-10-04.md) | [Grouped quality and release decision](docs/grouped_review_2026-10-04.md) | [Offline Linux appliance](deploy/appliance/README.md) |
| [Group materializer speed/parity](docs/grouping_speed_fix_2026-10-05.md) | [Recipient history and eligibility](docs/recipient_history_and_deployment_2026-10-04.md) | [MacBook execution runbook](docs/macbook_runbook.md) |
| [Review counts and independence](docs/review_counts_fix_2026-10-05.md) | [Metric-reporting correction](docs/quality_matrix_reporting_2026-10-04.md) | [Laptop 1M admission runbook](docs/laptop_1m_runbook.md) |

<sub>Project context: [requirement register](docs/requirements.md) · [PS mapping](docs/ps26146_compliance.md). The project associates TraceX with SIH 2026 PS 26146; official wording and listing still require submission-time confirmation. Scale and quality targets are project objectives.</sub>

## Trust boundaries and current limits

- **Metadata bounds the investigation.** Missing prevouts, timestamps or network observations limit the conclusions the system can support. TraceX contains no real-world address-attribution dataset or live blockchain-node integration.
- **Quality is task- and population-specific.** Quantitative ML results are synthetic. Calibration is pinned and applicability-limited; anomaly tails and network significance are not criminality probabilities.
- **Resource admission matters.** Disk-backed graph work can spill, while ML, sorting, dictionaries and embeddings still require global memory. A GPU does not replace host RAM or scratch space for the measured CPU pipeline.
- **Operate one worker service per host.** Internal task parallelism differs from competing job claimers. Grouping lease renewal and bounded retries do not establish multi-worker safety for the entire pipeline.
- **Evidence integrity has a defined boundary.** Hashes, receipts, source replay and preserved reviews provide checks. The vault is not enforced WORM storage; audit rows and exports are not cryptographically signed or hash-chained.
- **Public deployment needs additional controls.** Password hashing, signed tokens, role checks and non-development secret guards exist. MFA, session revocation, authentication rate limiting and built-in TLS are not implemented. Browser tokens use localStorage.

The local development database uses a public example password. For a non-development environment, configure a unique `TRACEX_SECRET_KEY`, set `TRACEX_ENV` appropriately, keep `TRACEX_ALLOW_DEV_ACTOR_HEADER=0`, and use a reviewed database, routing, storage and session policy. [Runtime configuration](app/config.py), [authentication](app/auth/security.py), [appliance guide](deploy/appliance/README.md).

## Next milestones

| Priority | Evidence needed to close it |
|---|---|
| **Exact-release scale acceptance** | Admitted fresh 1M/group-stage run, including authenticated final retrieval; separately evaluate 3M |
| **Group-review quality** | Applicable independently defined group truth, full queue coverage, fragmentation and overmerging measurements |
| **History-aware prioritization** | Frozen studies with representative merchant/benign controls and entity-disjoint transfer |
| **Operational hardening** | Public-network auth/session/TLS review, continuous ownership coverage, storage and recovery validation |

These are development objectives. The implemented baseline remains v2, evidence-backed review and local operation.

## Contributing and license

Start with the [documentation map](docs/README.md), preserve case/source/version boundaries, and include relevant verification with a proposed change. Use synthetic fixtures for reproducible examples; keep credentials, private evidence, generated datasets and model artifacts out of Git.

TraceX is released under the [MIT License](LICENSE), copyright © 2026 Shreshtha280407. Offline GeoIP resources retain their own attribution: DB-IP Lite **CC BY 4.0**, IPtoASN **PDDL 1.0**; see the [cache guide](deploy/appliance/geoip-cache/README.md).

---

<div align="center">

**Every lead has a source. Every decision has a record.**

<sub>TraceX · Bitcoin intelligence, built around evidence.</sub>

</div>
