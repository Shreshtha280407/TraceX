# TraceX anomaly stack

Six scoring layers over four grains, fused into one ranked review queue, plus an
ablation harness that runs every layer alone and every combination against the
deterministic Phase 4.1 rule so the best combination is chosen from measurements
rather than asserted.

```bash
make dataset          # regenerate the 100K fixture (all four ingestion formats)
make anomaly-stack    # every layer, every combination, all three tasks
make anomaly-stack-test
```

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

Generator v2 fixture, seed `tracex-phase5a-prep-100k-v2`, 100,000 transactions,
1% review budget, validation split used for selection and the final holdout
evaluated once afterwards. Ranked by average precision (tie-aware).

| Task | Best candidate | AP | Rule baseline AP | Lift |
| --- | --- | ---: | ---: | ---: |
| motif | `S_supervised` | 0.8253 | 0.3799 | 2.17× |
| surge | `D_burst+S_supervised` | 0.7283 | 0.2388 | **3.05×** |
| discrimination | `D_burst+S_supervised` | 0.9852 | 0.9569 | 1.03× |

Best **unsupervised** combination on validation: `A_global+D_burst` — surge AP
0.7165 vs the rule's 0.3479 (2.06×), AUC 0.971. At a 1% budget it flags 250 of
15,000 transactions and catches 65.4% of surge positives at 70.4% precision, where
the rule needs 2,221 flags to reach the same recall.

Read those numbers with three caveats:

1. **Synthetic-fixture evaluation only.** It shows which combination separates the
   fixture's labelled motifs from its labelled near-misses. It does not prove
   real-world detection accuracy.
2. **The motif task is close to circular.** Its labels are the generator's shape
   families and the rule's predicate is nearly the same predicate. `discrimination`
   is the honest test, and there the margin over the rule is small (1.03×) — real
   but modest.
3. **Layer D consumes layer A's family assignment**, so `D_burst` is not evidence
   independent of the structure predicate. It is the rule plus timing, which is
   exactly the intended design, not an accidental leak.

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
| **total** | **~19** | **peak RSS 575 MB** |

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
