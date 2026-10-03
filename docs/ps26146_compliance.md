# TraceX capability alignment — historical claims require verification

Current review correction (2026-10-03): the supplied `26146.pdf` is a team
presentation, not independently verified official problem-statement wording.
The table below is historical project documentation, **not a certified
official-PS compliance claim**. The implementation plan supplies the 3M /
30-minute and anomaly-AP targets; do not attribute them to an official PS.

Current scorer is `anomaly-stack-v2` (global IF + D burst, equal Stouffer),
not the v1 configuration described below. ECOD is descriptive tail context,
not IF attribution. Confidence is now pinned per finding and matched to rule
version, feature contract, target and narrow synthetic source applicability;
shifted or legacy records remain uncalibrated. Network 1−adjusted-p is not a
posterior probability. Review concerns a finding proposition, never automatic
transaction-level criminality labels. Triaged/escalated rows are not confirmed
positive training outcomes. The new explicit Confirm Pattern decision concerns
only its reviewed proposition; automatic supervised deployment remains disabled.

Whole-appliance offline acceptance requires API and browser evidence as well
as worker isolation; the previous worker-only outbound test does not establish
that claim. The current release contains no LLM/chat integration. The all-stage strict
scale ladder and copied-bundle/browser checks must pass before claiming their
targets. See the final `implementation_acceptance_2026-10-03.md` for measured
gates; older failures and historical reports remain preserved.

Project scope described in the supplied presentation: *AI-Powered Monitoring
& Analysis of Bitcoin Transaction Traffic*. The following is a capability map,
not verified official wording. Current measurements use the named 15.2 GiB /
12-logical-CPU host; historical reports used different resource settings.

## Challenge objectives

| PS requirement | Status | Implementation | Evidence |
| --- | --- | --- | --- |
| Ingest & parse bulk metadata (CSV / JSON / XML): timestamp, src/dst IP & port, TXID, input/output addresses, amounts, fee, script type | **Implemented** | Streaming CSV, NDJSON, JSON-array and XML adapters with validation and quarantine (`app/engine/adapters/source.py`, `app/engine/canonical/normalize.py`). Source-schema profiles map real-world header spellings (`Source IP`, `tx_hash`, `from_addresses`, `inputs_value_sats`, `Country`, ...) onto the v1 fields; list cells may be JSON, Python-style, or `;` / `|` / `,` separated; timestamps may be ISO-8601 with zone, `... UTC`, or Unix epoch s/ms (`app/engine/canonical/profiles.py`). | `tests/unit/test_phase_two.py`, `tests/unit/test_dataset_intake.py`; `tracex-dataset inspect FILE --full` reports PS field coverage for any file |
| Correlate network-layer (IP / port / timing) observations with blockchain-layer (wallet / TXID / amount) data | **Implemented** | (1) Every observation is a graph node linked to its transaction and to `network_endpoint` nodes. (2) Per-wallet relay-concentration tests: exact binomial tail of a wallet's spends on one endpoint / ASN against that endpoint's base rate, Bonferroni-corrected, written as `network-correlation-v1` findings. (3) Reported country / ASN checked against the offline Geo-IP database, contradictions written as `reported_geo_mismatch` findings. (4) Relay latency (observer time − block time) per endpoint. (5) Every anomaly-stack finding carries its network context (relay endpoint, ASN, country, endpoint/ASN activity in the preceding hour, network-context percentile). | `app/engine/analytics.py`, `app/ml/findings.py`, `app/ml/grains.py::build_network_context`; `tests/unit/test_entities_network_risk.py::test_network_correlation_and_geoip_mismatch_findings`; UI: *Network Intelligence* |
| Build an entity / transaction graph linking IPs, wallets and transactions | **Implemented** | UTXO graph (transactions, outputs, addresses, observations, endpoints) in DuckDB (`app/engine/graph/`), plus the entity layer: address → entity cluster, entity → linking transactions, wallet → relay endpoints, value-flow tables (`app/engine/analytics.py`). | `tests/unit/test_phase_three.py`, `tests/unit/test_entities_network_risk.py`; UI: *Graph Explorer* (entity and risk chips on address nodes), *Entities & Risk* |
| AI/ML detection use case with a working model, not just rules | **Implemented** | `anomaly-stack-v2`: global Isolation Forest plus retrospective Poisson-EWMA burst, equal-weight Stouffer fusion. ECOD supplies descriptive feature-tail context, not scoring attribution. Bounded IF perturbation explanations are cached for at most 20 selected transactions. Reference-period fitting is not an online forecast for in-sample rows. | `tests/unit/test_anomaly_stack.py`, `tests/unit/test_ml_pipeline_integration.py`, `tests/unit/test_research_contract.py`; `experiments/model_decision_v2.md` |
| Ranked, explainable alert list (why a wallet / transaction was flagged) | **Implemented** | Case-wide ranks; per-finding claim, explanations, feature contributions, benign alternatives, opposing evidence, graph path, source records. | `/v1/cases/{id}/findings`, `/v1/findings/{id}/evidence`; UI: *Findings Feed*, *Evidence Package* |
| ... with versioned confidence provenance | **Implemented** | Calibration is matched to rule/version/feature-contract/target and pinned in `FindingConfidence`. Only the exact registered synthetic source is applicable; shifted data and legacy records are unknown. Synthetic majority-pattern frequency is not criminality probability. Network 1−adjusted-p is a transformed hypothesis-test statistic; fused Gaussian tails are unvalidated. New calibration creates separate artifacts and never substitutes old provenance on reopening. | `scripts/calibrate_confidence.py`, `app/engine/confidence.py`, `tests/unit/test_analysis_acceptance.py` |
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
| Workable complete offline solution for Linux | `Dockerfile` + `deploy/appliance/` and checksummed copied bundle. API, worker and PostgreSQL networks are internal by default. Native Linux uses the API's host-private bridge URL; Docker Desktop requires a routing opt-out and cannot establish this same isolation claim. Current copied-install, browser and public IPv4/IPv6 checks are reported separately in `implementation_acceptance_2026-10-03.md`. The host's internet connection and unrelated images are not removed. |
| Working prototype (code repo) with ingestion, correlation and AI/ML model | **Implemented** (this repository). |
| Short technical write-up: approach, model choice, explainability method | `docs/final_report.md`, `docs/anomaly_stack.md`, `experiments/model_decision.md`, this document. |
| Dashboard / visualisation showing flagged entities and evidence for each flag | **Implemented** (UI pages listed above). |

## Scale

| Requirement | Status |
| --- | --- |
| Bulk datasets well beyond 100K rows | Typed bounded DuckDB graph/feature processing and compressed Parquet are implemented. ML, embedding and risk arrays still grow with snapshot size and have explicit admission limits; this is not constant memory. Strict 300K matched-worker results pass at workers 1/2/4, while 6/8 failed their native export budgets and are now rejected under the same 5120 MiB setting. See `docs/scale.md` and the final report for actual 1M/3M attempts, including failures. |
