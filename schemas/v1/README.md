# TraceX canonical records v1

These JSON Schema documents freeze the interchange contract; Parquet/Arrow physical layouts added in Phase 2 must preserve these meanings. `additionalProperties: false` is deliberate at the canonical boundary. Adapter-specific raw fields belong in preserved source records, not silently in canonical facts.

All identifiers are case-scoped. All evidence locators point to a particular evidence source hash. `null` means unknown; consumers must not replace it with a guessed value.

| Record | Identity / purpose |
| --- | --- |
| `case` | Investigation boundary; access control and data isolation attach here. |
| `evidence_source` | Immutable original bytes and acquisition context. |
| `transaction` | `(case_id, network, txid)` transaction fact; conflicting variants are explicit. |
| `tx_input` | An input and its optional prior outpoint reference. |
| `tx_output` | An output at `(txid, vout)`, valued in integer satoshis. |
| `address_script` | Script/address representation, not an identity claim. |
| `network_observation` | Off-chain capture observation with no ownership assertion. |
| `candidate_link` | Reversible, uncertain proposition with evidence for and against it. |
| `finding` | Versioned triage/finding result tied to snapshot and evidence coverage. |
| `review_decision` | Human review record with optimistic-concurrency predecessor. |

