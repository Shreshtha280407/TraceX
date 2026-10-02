# SIH 2026 PS 26146 — requirement-by-requirement compliance

Problem statement: *AI-Powered Monitoring & Analysis of Bitcoin Transaction
Traffic* (Problem Statement 5, NTRO, theme Cryptocurrency). Every row names the
code that implements it and the evidence that it works. Measured numbers are
from the 15 GiB / 4-CPU Linux development host.

## Challenge objectives

| PS requirement | Status | Implementation | Evidence |
| --- | --- | --- | --- |
| Ingest & parse bulk metadata (CSV / JSON / XML): timestamp, src/dst IP & port, TXID, input/output addresses, amounts, fee, script type | **Implemented** | Streaming CSV, NDJSON, JSON-array and XML adapters with validation and quarantine (`app/engine/adapters/source.py`, `app/engine/canonical/normalize.py`). Source-schema profiles map real-world header spellings (`Source IP`, `tx_hash`, `from_addresses`, `inputs_value_sats`, `Country`, ...) onto the v1 fields; list cells may be JSON, Python-style, or `;` / `|` / `,` separated; timestamps may be ISO-8601 with zone, `... UTC`, or Unix epoch s/ms (`app/engine/canonical/profiles.py`). | `tests/unit/test_phase_two.py`, `tests/unit/test_dataset_intake.py`; `tracex-dataset inspect FILE --full` reports PS field coverage for any file |
| Correlate network-layer (IP / port / timing) observations with blockchain-layer (wallet / TXID / amount) data | **Implemented** | (1) Every observation is a graph node linked to its transaction and to `network_endpoint` nodes. (2) Per-wallet relay-concentration tests: exact binomial tail of a wallet's spends on one endpoint / ASN against that endpoint's base rate, Bonferroni-corrected, written as `network-correlation-v1` findings. (3) Reported country / ASN checked against the offline Geo-IP database, contradictions written as `reported_geo_mismatch` findings. (4) Relay latency (observer time − block time) per endpoint. (5) Every anomaly-stack finding carries its network context (relay endpoint, ASN, country, endpoint/ASN activity in the preceding hour, network-context percentile). | `app/engine/analytics.py`, `app/ml/findings.py`, `app/ml/grains.py::build_network_context`; `tests/unit/test_entities_network_risk.py::test_network_correlation_and_geoip_mismatch_findings`; UI: *Network Intelligence* |
| Build an entity / transaction graph linking IPs, wallets and transactions | **Implemented** | UTXO graph (transactions, outputs, addresses, observations, endpoints) in DuckDB (`app/engine/graph/`), plus the entity layer: address → entity cluster, entity → linking transactions, wallet → relay endpoints, value-flow tables (`app/engine/analytics.py`). | `tests/unit/test_phase_three.py`, `tests/unit/test_entities_network_risk.py`; UI: *Graph Explorer* (entity and risk chips on address nodes), *Entities & Risk* |
| AI/ML detection use case with a working model, not just rules | **Implemented** | Unsupervised anomaly stack wired into every import: Isolation Forest + ECOD transaction-structure layer and a Poisson-EWMA / Bayesian change-point burst layer, Stouffer-fused, refit on each snapshot's own reference period (`app/ml/`). | `tests/unit/test_anomaly_stack.py`, `tests/unit/test_ml_pipeline_integration.py`; release `anomaly-stack-v1` (`experiments/model_decision.md`) |
| Ranked, explainable alert list (why a wallet / transaction was flagged) | **Implemented** | Case-wide ranks; per-finding claim, explanations, feature contributions, benign alternatives, opposing evidence, graph path, source records. | `/v1/cases/{id}/findings`, `/v1/findings/{id}/evidence`; UI: *Findings Feed*, *Evidence Package* |
| ... with a confidence score | **Implemented** | `app/engine/confidence.py`: calibrated P(true laundering-pattern lead \| rule, score) via per-rule isotonic regression fitted on the labelled synthetic truth (training split; reliability measured on the time-ordered validation split; final holdout never read), with Brier score, ECE, AUC and a reliability grade; statistical confidence (1 − adjusted p) for network tests; label-free anomaly p-value for ML findings. Computed at read time, so refitting never rewrites a stored finding. | `scripts/calibrate_confidence.py`, `app/engine/calibration/confidence-v1.json`; `tests/unit/test_entities_network_risk.py::test_confidence_is_calibrated_and_bounded` |
| Present findings via a simple dashboard / link-analysis visualisation | **Implemented** | React UI: Dashboard, Evidence Intake (live stage tracker), Graph Explorer (source-centred curved fund flow, pattern highlighting), Findings Feed, Evidence Package, Entities & Risk, Network Intelligence, Export. | `frontend/` |

## Suggested AI/ML focus areas

| Focus area | Status | Implementation |
| --- | --- | --- |
| Entity clustering — common-input-ownership + graph embeddings | **Implemented** | Common-input-ownership union-find over every multi-input transaction, excluding CoinJoin-shaped transactions (>= 3 inputs and >= 3 equal-valued outputs); clusters are reviewable propositions with their linking transactions. Spectral graph embeddings (truncated SVD of the degree-normalised wallet × transaction incidence matrix, 16 dimensions) for every wallet active in >= 2 transactions; cosine "behaviourally similar wallets". (`app/engine/analytics.py`) |
| Anomaly detection — statistically unusual transactions / flows | **Implemented** | Anomaly stack above; relay-concentration statistics. |
| Peeling-chain / mixing detection | **Implemented** | Deterministic peeling-chain walk and CoinJoin-like structure detector, grouped one finding per pattern (`app/engine/motifs/deterministic.py`, `app/engine/findings/deterministic.py`). |
| Risk scoring — propagate risk from seed illicit wallets | **Implemented** | Analyst seeds (address or entity, reason, weight) propagated over the UTXO value-flow graph: downstream haircut taint (share of a wallet's received value traceable to a seed, ×0.85 per hop) and upstream exposure (share of its spending that reached a seed), combined 1 − (1−d)(1−u). Recomputed in well under a second at 100K transactions. (`app/engine/analytics.py::propagate_risk`, `/v1/cases/{id}/risk*`) |

## Dataset

| PS statement | Status |
| --- | --- |
| "Participants will work with a synthetic dataset modelled on real Bitcoin P2P/transaction fields (no real seized or live-intercept data will be provided)." Dataset link: Nil. | **Implemented.** TraceX ships a seed-controlled generator (`fixtures/phase5a_100k/generate.py`, 100K transactions, labelled motif episodes, benign near-misses, all PS fields) and `scripts/scale_fixture.py` for multi-million-row load tests. `data_manifest.json` records that the PS document publishes no dataset; any file supplied at the event is verified with `tracex-dataset inspect --full --register` before import. |
| Minimum fields incl. geo_country / ASN — *integrate an open-source downloadable Geo-IP database* | **Implemented.** DB-IP IP-to-Country Lite (CC BY 4.0) + IPtoASN (PDDL), IPv4 and IPv6, downloaded once (`tracex-geoip download`) or imported from copied files (`tracex-geoip import`), compiled to a local range table, looked up offline. Baked into the appliance image. |

## Expected deliverables

| Deliverable | Status |
| --- | --- |
| Workable complete offline solution for Linux | **Implemented.** `Dockerfile` + `deploy/appliance/` (PostgreSQL, API serving the web UI, worker); the worker and database sit on a network with no external route. `scripts/build_offline_bundle.sh` produces one tarball (images + compose + installer) that installs on an air-gapped host. Verified here: image build, air-gapped install from the bundle with all images deleted, end-to-end import through the API, Geo-IP lookups, and a failed outbound connection from the worker. |
| Working prototype (code repo) with ingestion, correlation and AI/ML model | **Implemented** (this repository). |
| Short technical write-up: approach, model choice, explainability method | `docs/final_report.md`, `docs/anomaly_stack.md`, `experiments/model_decision.md`, this document. |
| Dashboard / visualisation showing flagged entities and evidence for each flag | **Implemented** (UI pages listed above). |

## Scale

| Requirement | Status |
| --- | --- |
| Bulk datasets well beyond 100K rows | **Implemented.** The machine profile (`app/resources.py`, cgroup-aware) decides per import whether to run in memory or in the bounded on-disk mode (`app/engine/bounded.py`), which keeps one partition of the snapshot resident. Feature rows go to a compressed Parquet store instead of the database (100K rows: 3.9 GB of SQLite → one Parquet file; end-to-end 163 s → 111 s). See `docs/scale.md` for the 1M and 3M runs. |
