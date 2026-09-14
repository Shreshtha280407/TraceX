# Phase 5 graph intelligence and transparent rules baseline

Owner: Shreshtha. Status: in progress.

## Boundary

This module uses Nipun's durable `CorrelationSubmission` and `correlation.upserted.v1` replay seam.
It returns reviewable candidates only; it never resolves or merges identities.
PostgreSQL is authoritative and Neo4j is an idempotent, bounded metadata projection.

| Input | Output | Provenance | Time policy |
|---|---|---|---|
| Stable phone, email, account, device, vehicle, platform handle | exact candidate | case, observation, evidence, locator, normalization version | no event inferred |
| Alias/transliteration/handle | bounded candidate | source-backed observation/evidence references | no event inferred |
| Local hashed pgvector snapshot | weak candidate | provider/configuration/source snapshot hashes | no event inferred |
| Bounded windows | hot-window contribution | both evidence paths and start/end | both ranges required |
| Durable correlation | `Correlation` node | Nipun EvidencePathSnapshot | no timeless entity edge |

Document, CDR, finance, media, audio, and social claims retain existing mappings.
Calls, transfers, sightings, meetings, and messages remain event-first and time-bounded.

## Invariants

Exact blocking uses only documented stable categories after explicit normalization.
Aliases, transliterations, and vectors are retrieval reasons and never verification.
Candidates retain observations, evidence, locator references, named contributions, contradictions, and hashes.
Mixed-case input is rejected before retrieval, scoring, correlation, vector search, analytics, or Cypher projection.
The semantic handler starts at case-scoped `Evidence-[:YIELDED_OBSERVATION]->Observation` paths.
Missing graph provenance makes Cypher a no-op; `MERGE (case_id, projection_key)` makes replay idempotent.
Neo4j receives no raw source, object URI, credential, claim token, SQL, or driver error.

## Preliminary rules and pgvector

`phase5_preliminary_rules_v1`: exact identifier `+60`, platform handle `+45`, exact alias `+12`,
normalized alias `+8`, transliteration `+6`, local vector `+3`, hot window `+10`, contradiction `-40`.
These weights are preliminary review guidance, never probability, guilt, truth, or automatic merge.
The configuration and feature snapshot use canonical SHA-256 hashes.
The vector is a 32-dimensional deterministic token hash, not a semantic trained model.
pgvector searches filter case before ordering and record provider/version/configuration/source snapshot hashes.
`VectorSearchUnavailable` is explicit; no substitute semantic result is fabricated.

## Analytics and motif

The explicit one-case snapshot returns deterministic PageRank, unweighted Brandes betweenness, WCC, and seeded
Leiden modularity communities through `igraph`/`leidenalg`.
The motif is a source-backed, ordered communication → transfer → movement/meeting chain within one hour.
Analytics and motifs are review-priority signals only.

## Evaluation and deferral

`deferred_evaluation_report` is a versioned, hashable future-offline reporting shape only.
Operation Nightfall truth evaluation, measured Precision@K/Recall@K/false-link rate, P99 bridge validation,
final rules-weight freeze, full real-dataset validation, and LAN end-to-end validation will run after all
Phase 5 contributors have merged their work.
Trained ranking, entity resolution, automatic merges, hypotheses, criminality conclusions, and external AI
APIs are deferred. Algorithm quality and operational performance remain unmeasured.
