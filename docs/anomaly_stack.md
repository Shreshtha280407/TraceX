# TraceX anomaly stack

Six scoring layers over four grains, fused into one ranked review queue, plus an
ablation harness that runs every layer alone and every combination against the
deterministic Phase 4.1 rule so the best combination is chosen from measurements
rather than asserted.

```bash
make dataset               # regenerate the 100K fixture (all four ingestion formats)
make anomaly-stack         # unsupervised only — the deployable configuration
make anomaly-stack-demo    # adds the supervised comparator (demo/research)
make anomaly-stack-holdout # the final holdout, once
make anomaly-stack-test
```

## Two configurations, and only one of them ships

| | `make anomaly-stack` | `make anomaly-stack-demo` |
| --- | --- | --- |
| Layers | A–E, unsupervised | A–E plus supervised layer S |
| Labels used in any `fit()` | none | the fixture's generator truth |
| Deployable | **yes** | no — research comparator |
| What its numbers prove | the ranking a case actually gets | an upper bound if real labels existed |

Layer S trains on the generator's own shape families, which are close to the
deterministic rule's predicate. It is reported because it bounds the headroom, not
because it is shippable. It becomes legitimate the moment it trains on analyst
review decisions instead — `app/ml/findings.py::review_decision_labels` reads them,
and deliberately returns `None` until enough exist, so the deployed ranking cannot
silently start depending on a model that has nothing real to learn from.

## Why the layers exist

The frozen Phase 4.1 feature table keys everything on `(address, 15-minute
window)`. Equal-output shape is a property of a *transaction*, spend latency of an
*outpoint*, surge of an *entity over time*, and motif bursts of the *population
over time*. Collapsing four questions onto one key is why a 4-output CoinJoin
becomes four rows that each record exactly one inbound transaction — identical to
the ~77% of rows that are ordinary single receipts.

| Layer | Grain | Answers | Method |
| --- | --- | --- | --- |
| **A** structure | transaction | equal-output shape, peel geometry, batch/consolidation | Isolation Forest + ECOD, rank-averaged; optionally stratified by a MiniBatchKMeans shape family |
| **B** latency | outpoint → transaction | rapid-spend behaviour | Kaplan–Meier spend-latency baseline per value decile; unspent outputs are right-censored, not slow |
| **C** history | entity × window | activity surge, value surge, recurrence, reactivation, prior motif frequency | Poisson surprisal against an empirical-Bayes-shrunk baseline built only from strictly-earlier windows |
| **D** population | motif count series | *sudden surge in CoinJoin and peeling* | Poisson EWMA control chart + Bayesian online change-point detection |
| **E** graph | transaction | bounded k≤2 neighbourhood, plus the PS endpoint/geo fields the exporter currently drops | deterministic graph features + Isolation Forest/ECOD |
| **F** fusion | transaction | what a reviewer opens first | per-layer ECDF p-values → weighted Stouffer → conformal quantile at a fixed budget |
| **S** supervised | transaction | *optional comparator* | HistGradientBoostingClassifier over every grain at once |

Layer S is the only one that reads labels, and it is opt-in (`--with-supervised`).
On this fixture its labels are the generator's own shape families, which is close
to circular for the motif task and genuinely informative for discrimination. In
production the non-circular label source is the reviewer decisions already
recorded through `POST /v1/findings/{id}/reviews`.

## The three evaluation tasks

One headline number would hide that the layers are good at different things, so
every candidate is scored on three separate questions:

| Task | Positive set | Population |
| --- | --- | --- |
| `motif` | CoinJoin-like and peeling transactions | every transaction in the split |
| `surge` | transactions inside a labelled burst episode | every transaction in the split |
| `discrimination` | true motifs | motifs **plus** labelled near-miss negatives only |

`discrimination` is the one that decides whether a model is worth anything over a
threshold. It only became meaningful once the fixture gained
`nearmiss_rule_positive` — benign recurring payments that satisfy the
deterministic rule's predicate exactly (≥3 inputs, ≥3 outputs, ≥3 exactly-equal
outputs) while differing in everything a model can see: round payroll amounts,
recipients that recur, and outputs that are held rather than re-spent in minutes.
Without that family every near-miss is one predicate short, the rule excludes them
all for free, and no measurement can separate a threshold from a model.

## Measured results

Generator v2 fixture, 100,000 transactions, 1% review budget, strictly causal
features. Validation used for selection; final holdout evaluated once afterwards.
Average precision, tie-aware.

**Deployable configuration — unsupervised, final holdout:**

| Task | Best candidate | AP | Rule | Lift | P@100 | P@budget | Recall |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| surge | `A_global+D_burst` | 0.5927 | 0.2388 | **2.48x** | 0.750 | 0.696 | 33.8% |
| motif | `A_global+D_burst` | 0.6282 | 0.3799 | 1.65x | 0.890 | 0.844 | 24.3% |
| discrimination | *rule baseline wins* | 0.9569 | 0.9569 | 1.00x | 0.880 | 1.000 | 4.0% |

**Demo configuration — with the supervised comparator, final holdout:**

| Task | Best candidate | AP | Rule | Lift |
| --- | --- | ---: | ---: | ---: |
| surge | `D_burst+S_supervised` | 0.7371 | 0.2388 | 3.09x |
| motif | `S_supervised` | 0.8239 | 0.3799 | 2.17x |
| discrimination | `S_supervised` | 0.9961 | 0.9569 | 1.04x |

### The result that matters most

**No unsupervised combination beats the rule on discrimination.** That is not a
tuning failure, it is the structure of the problem: both classes are anomalous. A
real CoinJoin and a benign payroll run that satisfies the same predicate are *both*
unusual against ordinary traffic, so an outlier detector has no reason to prefer
one. Separating them needs supervision, and the only honest supervision is analyst
feedback.

So the deployable ranking earns its place on **surge detection and triage
ordering**, where it is 2.5x the rule and cuts the queue from 2,221 flags to a few
hundred at far higher precision — not on deciding which equal-output transaction is
really a mixer. That decision stays with the reviewer until reviewer decisions
exist to learn from.

## What the ablation showed that a guess would not

- **Stratification cuts both ways.** `A_structure` (scored within a KMeans shape
  family) consistently loses to `A_global` here, because the target *is* a shape
  family — its members are the majority of their own cluster and stop looking odd.
  Stratification is still the right default when the target is unknown; both are
  computed so the trade-off is measured rather than assumed.
- **More layers is not better.** Equal-weight Stouffer fusion lets a weak layer
  drown a strong one: five-layer combinations score below two-layer ones on every
  task. The ablation exists precisely to catch this.
- **Layer B and E add little on their own** and mostly help by widening recall at
  the budget. Keep them only if that trade is worth it for your queue.

## Strict causality

Every feature for transaction `t` uses only facts timestamped at or before
`tx_time[t]`. This is a deployability constraint before it is a correctness one:
when a case is scored, the transaction has just been observed, and a feature that
needs the future cannot be computed at all.

An audit found three classes of future leak and removed them:

| Class | What leaked | Fix |
| --- | --- | --- |
| Outcome features | `spent_output_share`, `distinct_next_tx`, `chain_depth_forward` — all read whether and how outputs were later spent | removed; no causal version exists. Input *ages* are the backward-looking counterpart |
| Whole-dataset statistics | `recipient_reuse_share` and `country_rarity` counted over the entire file, so an early transaction saw the whole future distribution | expanding-window counts over strictly-earlier rows (`prior_recipient_reuse_*`, `country_rarity_prior`) |
| Containing-bucket aggregates | endpoint/ASN co-occurrence counted inside the bucket holding the transaction, so a transaction early in a bucket saw later ones | trailing windows, `[t - W, t)` (`*_cooccurrence_trailing`) |

Layer B had a fourth: the survival curve was fitted on durations that ran past the
end of the training period, so spends from validation and holdout shaped the
baseline every later row was scored against. It now fits on a latency table
censored at the last reference-split transaction. Rank normalisation is likewise
fitted on reference rows, so scoring one transaction gives the same number as
scoring it inside a batch — a transductive whole-dataset rank could not be
reproduced by a deployed scorer.

**This is proved, not asserted.** `app.ml.facts.truncate_facts` rebuilds the
snapshot as it looked at time T, and the property tests assert that every feature
for transactions at or before T is bit-identical to the full run. One
future-reading column makes them fail.

Measured effect of the fix on the final holdout: the leaks were *noise*, not
signal. Removing them left the supervised result unchanged (-0.001 AP) and
**improved** the unsupervised combination substantially (+0.17 AP on motif, +0.10
on surge, +0.19 on discrimination). The leak was real and had to go for the stack
to be deployable at all; on this fixture it happened not to be inflating anything.

## Running inside TraceX

`app/ml/findings.py::materialize_ml_findings` runs the stack on a committed,
receipt-approved snapshot — reading the same canonical fragments the graph builder
and the deterministic detectors read, through `app.engine.graph.builder._facts` —
and writes `FindingRecord` rows that the existing
`GET /v1/cases/{case_id}/findings` endpoint already serves.

Phase 4's boundaries are kept exactly where they were: every stored finding carries
its source locators so a reviewer can reopen the raw record, benign alternatives,
an explicit coverage limitation, and a claim worded as triage priority rather than
a verdict. Writing is idempotent per snapshot. Only unsupervised layers run there.

`tests/unit/test_ml_pipeline_integration.py` ingests a small source through the
real worker, builds the graph, and asserts the equal-output shape and both peel
steps are recovered from snapshot facts — so the offline harness and the product
path are provably reading the same evidence.

## Leakage and causality controls

Enforced by `tests/unit/test_anomaly_stack.py`, not by convention:

- Every scaler, ECDF, forest and survival curve is fitted on `train_reference`
  rows only; the conformal threshold is the reference quantile.
- `build_history_table` baselines use strictly-earlier windows of the same address
  and reset between addresses — asserted directly.
- Split assignment comes from real transaction timestamps at the split-group
  boundaries, never from row ordering, and is asserted monotone.
- A substring denylist rejects any feature column naming an address, txid,
  locator, scenario, split, episode or label. It already caught one ambiguous
  column name (`reused_address_share`, renamed to `recipient_reuse_share`).
- The four Phase 4.1 `risk_*` fields are hard-excluded from every grain, per
  `docs/phase5a_handoff.md`.
- `evaluation_truth.json` is read only by `app.ml.evaluate` (and by `run_stack`
  for the two split-boundary timestamps). No label reaches a feature table.

## Cost

100K transactions on a 12-thread i5 with 15 GiB, `TRACEX_ML_THREADS=4`:

| Stage | Seconds | Note |
| --- | ---: | --- |
| load facts | 2.1 | three NDJSON passes into integer-indexed numpy arrays |
| grain A | 7.5 | per-transaction shape features |
| layer A (×2 variants) | 6.1 | Isolation Forest + ECOD, 100 trees, `max_samples=256` |
| layers B/C/D | 1.9 | survival, causal baselines, burst detection |
| layer E | 3.0 | bounded graph + network context |
| **total** | **~19** | **peak RSS 565 MB** (unsupervised) |

For comparison, the existing `prepare_feature_package` path peaks at ~5.7 GB over
~9 minutes for the same 100K transactions. The integer-indexed array layout is
what makes the difference, and it is what has to stay if this is to scale past the
fixture — at 1M rows the dict-of-dicts path will not fit under a 24 GB threshold.

New dependencies: `numpy` and `scikit-learn` only, both already declared in the
`[ml]` extra. ECOD and HBOS are written in-tree rather than pulled from `pyod`, so
a reviewer can read the exact arithmetic behind a score — which is what the PS's
*explainable* requirement needs.

## Explaining a finding

`app.ml.stack.explain(result, transaction)` returns the per-layer scores plus the
strongest ECOD per-feature contributions, because ECOD's score is literally a sum
of per-column tail log-probabilities. The runner prints these for the top-ranked
transactions under `--explain-top`.
