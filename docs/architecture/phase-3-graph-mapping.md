# Phase 3 deterministic observation-to-graph mapping

Owner: Shreshtha. This layer consumes an accepted canonical `ObservationV1` only after Nipun's batch/result transaction has written the existing graph-projection outbox row. It is a pure registry (`app/modules/graph/mapping.py`), followed by the existing retrying projector and Neo4j repository boundary. Source workers do not write Neo4j.

## Frozen rules

- All graph identities include `case_id`; every Cypher `MATCH`, `MERGE`, and relationship endpoint is case-scoped. Equal identifiers in two cases never share a node or relationship.
- `SourceClaim` is an evidence-local label or identifier claim. It is never an `EntityV1`, and no mapping resolves, merges, scores, or aliases it.
- Calls and transactions are `TemporalEvent` nodes linked to `SourceClaim` participants. No direct endpoint-to-endpoint relationship is created.
- Each specialised node begins from the existing `(:Evidence)-[:YIELDED_OBSERVATION]->(:Observation)` chain and is linked by `PROJECTS_CLAIM` or `PROJECTS_EVENT`. A specialised projection therefore cannot be orphaned from its evidence and observation.
- `mapping_version` and a deterministic `mapping_config_hash` are stored on the parent observation and every specialised node. A changed mapping version/configuration produces distinct projection identities; operators must reset/requeue the derived graph outbox deliberately for any reprojection. Historical shapes are never silently rewritten.

## Mapping table

| Canonical observation type | Projection | Required fields | Time and locator policy | Incomplete/unsupported outcome |
| --- | --- | --- | --- | --- |
| `fir_reference`, `police_station_mention`, `date_time_mention`, `phone_number_mention`, `email_address_mention`, `vehicle_identifier_mention`, `financial_identifier_mention`, `amount_mention`, `legal_section_mention`, `ner_entity_mention` | one `SourceClaim` | exactly one source-backed extracted entity mention | existing observation page/span/bbox locator is preserved on the linked canonical observation | `deferred: document_claim_requires_one_source_mention` |
| `person_contact_association`, `dated_communication_reference`, `transaction_claim`, `incident_event_mention` | one `SourceClaim` of type `document_relation_claim` | documented extractor `rule` | source span is preserved on the linked observation; no event is fabricated from an unnormalised document date | `deferred: document_relation_requires_rule_provenance` |
| `cdr_call_record` | `TemporalEvent(event_type=cdr_call)` plus caller/callee `SourceClaim`s | canonical `caller_number`, `callee_number`, timezone-aware canonical `timestamp` | event time is canonical timestamp; validated duration, call direction, and cell site are copied only if present; row/sheet/JSON locator remains on observation | `deferred: cdr_call_requires_caller_callee_and_canonical_time` |
| `financial_transaction_record` | `TemporalEvent(event_type=financial_transaction)` plus source/destination instrument claims | `sender_account`, `receiver_account`, exact canonical decimal-string `amount`, `currency`, timezone-aware canonical `timestamp` | event time is canonical timestamp; exact amount string is retained without float arithmetic; row/sheet/JSON locator remains on observation | `deferred: transaction_requires_source_and_destination_instruments`, `transaction_requires_amount_currency_and_canonical_time`, or `transaction_amount_is_not_canonical_decimal` |
| Any media, communication, generic, or unknown type | no specialised mapping | n/a | canonical observation projection remains intact | `unsupported: unsupported_observation_type` |

The structured financial normalizer now retains its already-source-backed `sender_account` and `receiver_account` values on the parent `financial_transaction_record`. It continues to emit individual account mentions. This avoids unsafe joins between separate observations based on a row locator.

## Provenance and bounded data

`SourceClaim` and `TemporalEvent` carry case, evidence, observation, source observation type, extraction confidence, extractor name/version/config/model version, and mapping version/configuration hash. Their relationship to the canonical observation makes the complete `SourceLocator` traversable, including document page/span/normalised bbox and structured row/sheet/JSON path. The open-ended canonical `attributes` bags and raw evidence payloads are never copied into Neo4j. Only the mapping allow-list writes event properties.

## Explicit non-goals

This layer performs no entity resolution, entity merging, fuzzy or alias matching, cross-case retrieval, candidate or relationship scoring, graph analytics, hypotheses, guilt/risk conclusions, ML, LLM calls, worker security changes, or raw-source storage.

## Synthetic fixtures

`tests/unit/graph/test_mapping.py` uses labelled synthetic document, CDR, and finance observations. `tests/integration/graph/test_phase_3_mapping_live.py` self-skips unless live Neo4j is configured and verifies the same three paths, provenance traversal, case scoping, and a second idempotent projection.
