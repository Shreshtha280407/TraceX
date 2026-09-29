# Phase 5A local 100K synthetic preparation fixture (generator v2)

A deterministic, seed-controlled source generator for a local dataset with exactly
100,000 canonical synthetic Bitcoin transaction records. It supports Phase 5A/5B
data preparation and SIH demonstration only. It is not blockchain data, an
attribution dataset, proof of real-world detection performance, or a training
corpus.

```bash
make dataset
# or directly: uv run python fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson,xml,json --verify
```

Always through `uv run` (or `make dataset`, which does the same). A bare `python3`
resolves to whatever the OS shipped, which on many macOS installs is below this
generator's `>=3.11` floor and fails with a confusing syntax error rather than a
clear version message.

The generator uses the Python standard library and the versioned seed in
`fixture_config.json`. `--verify` checks every UTXO/split/raw-record invariant,
cross-reconciles all four ingestion formats by `(txid, source_record_id)`, and
checks every generated file hash against its manifest. It writes only beneath the
supplied output directory. `datasets/phase5a_100k/` is gitignored; the source,
documentation, tests and manifest schema in this directory are the reviewable
repository artifacts.

## Why v2 exists

v1 could not falsify a model. Every defect below was measured on the generated v1
data, and each one silently inflated candidate metrics:

| v1 defect | Measured on v1 | v2 |
| --- | --- | --- |
| Address-namespace collision: `index * 32 + vout` with 100 bootstrap outputs | 100,000 bootstrap outputs collapsed onto 32,068 addresses; **99.99%** of the windows `concentrated_collection` fired on were that collision | Addresses keyed on the full `(index, vout)` pair; reuse is an explicit heavy-tailed hot pool |
| Arithmetic timestamp ladder | index of dispersion ≈ 0 — no burstiness anywhere | Non-homogeneous Poisson arrivals with a diurnal rate; dispersion ≈ 58 |
| CoinJoin on a metronome | 1 per 6.6 h, never more than 1/hour or 4/day | Baseline motif rate plus 22 labelled surge episodes across all three splits; up to 43 CoinJoins/hour |
| Constant CoinJoin remainder | always 13,007–13,009 sats vs a population median of 710,560 — an ECOD detector scored P@50 0.72 by memorising it | Remainders drawn from the same change distribution as ordinary transactions; >1,400 distinct values |
| Near-absent rapid spends | 13 of 99,241 resolved spends within an hour (0.013%) | Bimodal scheduled latency; ~9.5% within an hour, plus a deliberate right-censored (unspent) fraction |
| No shape diversity | 98,046 single-output transactions; 1,000 transactions with 100 identical outputs | Realistic 1–8 in / 1–36 out mix; no bootstrap monoshape |
| Equal-output was a perfect rule | threshold rule scored precision 1.000, recall 1.000 — nothing for a model to improve | Threshold rule now scores precision 0.33 at recall 1.00 |
| Information-free network context | `geo_country` uniform, `src_ip` = `index % 29`, `asn` = `index % 19` | Zipf-skewed, with a correlated (≈9× lift, not deterministic) relay tendency during surges |
| 36 usable labels | too few to separate two candidates at any confidence | ~2,800 labelled positives plus ~3,100 labelled near-miss negatives |
| CSV and NDJSON only | the PS asks for bulk XML intake; the generator refused any other format | CSV, NDJSON, XML and JSON-array, all cross-reconciled |

## Local output

| File | Purpose |
| --- | --- |
| `ingestion_rows.{csv,ndjson,xml,json}` | Logically equivalent SIH-compatible raw ingestion rows, including all twelve published minimum fields and structured `inputs`/`outputs`. |
| `transactions.ndjson`, `inputs.ndjson`, `outputs.ndjson` | Canonical normalized-shaped facts used to validate UTXO correctness. |
| `network_observations.ndjson` | Separate TXID-linked relay/endpoint observations. Never asserts wallet ownership, transaction origin, or control of an IP. |
| `duplicate_candidates.ndjson` | Additional source-record candidates; they do not change the 100,000 canonical count. |
| `evaluation_truth.json` | Evaluation-only labels, motif episodes, expected controls, and time/group split truth. Never upload or ingest it. |
| `fixture_manifest.json` | Generated counts, deterministic hashes, invariants, and provenance locators. |

## Motif families

`evaluation_truth.json`'s `labelled_transactions` assigns one family per labelled
transaction. Positives are `coinjoin_like` and `peel_step`; everything prefixed
`nearmiss_` is a labelled negative.

| Family | Role |
| --- | --- |
| `coinjoin_like` | 3–8 inputs, 3–8 equal outputs at one of eight denominations, plus ordinary change |
| `peel_step` | one hop of a 2–8 hop chain; 0.5–9% peeled per hop |
| `batch_fanout`, `consolidation` | benign shapes that an unsupervised detector will find unusual |
| `nearmiss_two_equal` | exactly two equal outputs — one short of the rule |
| `nearmiss_tolerance` | three outputs equal within 120–900 sats, not exactly |
| `nearmiss_two_inputs` | three exactly-equal outputs but only two inputs |
| `nearmiss_batch` | a fan-out whose payments are near-equal |
| `nearmiss_rule_positive` | **satisfies the deterministic rule exactly** while being a benign recurring payroll-shaped payment |

`nearmiss_rule_positive` is the family that makes the comparison meaningful.
Without it every near-miss is one predicate short, the rule excludes them all for
free, and no measurement can distinguish a threshold from a model.

## Phase 4.1 anchors

A contiguous 50-transaction block immediately after the bootstrap block holds the
hand-built controls the deterministic detector tests depend on: a verified
three-hop peeling chain, an ordinary two-step negative control, a synthetic review
seed with a verified two-hop downstream path, an equal-output control and its
ordinary multi-output negative control, and a disconnected control address. Their
absolute indexes come from `generate.anchor_indexes(config)` — never hardcode them.

Everything after that block is drawn from the stochastic process, so the anchors
no longer carry the fixture's statistical weight the way v1's hand-placed
scenarios did.

`feature_contract.md` documents which SIH feature families the Phase 4/4.1
exporter genuinely produces. `docs/anomaly_stack.md` documents the separate
multi-grain stack that consumes this fixture directly.
