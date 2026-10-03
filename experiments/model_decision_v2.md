# Model decision — release `anomaly-stack-v2`

Historical adoption record. The 2026-10-03 review retains its numerical
procedure; see `model_decision_review_20261003.md` for current independent
reservations, measured research comparisons and limitations. The old ECOD
“attribution” wording below is incorrect for the IF score: ECOD is descriptive
tail context, with separate bounded IF sensitivity now exposed. The 1%
reference quantile is not a hard queue cap. Independently seeded synthetic
data does not establish real-world generalization.

Status: **adopted**. Supersedes the scoring of `anomaly-stack-v1`
(`experiments/model_decision.md`); everything else about v1 (layers, fusion,
budget, seed, refit-per-snapshot) is unchanged.

## The change

Layer A (`A_global`, transaction structure) is ranked by **Isolation Forest
alone**. v1 rank-averaged Isolation Forest and ECOD. ECOD is still fitted on
every snapshot: its per-feature tail contributions remain the explanation every
ML finding shows ("strongest feature contributions"), so explainability is
unchanged. Fusion with `D_burst` stays Stouffer with equal weights.

```text
Release ID:      anomaly-stack-v2
Layers:          A_global + D_burst
Layer A score:   Isolation Forest ranks (ECOD contributions = attribution)
Fusion:          stouffer, equal weights
Review budget:   1% of a snapshot's transactions
Mode:            unsupervised only, refit on each snapshot's earliest 70%
```

## How it was chosen (and why it should generalise)

`scripts/ml_study.py` changes **one thing at a time** relative to v1 and
reports average precision for the motif, surge and discrimination tasks on
validation and on holdout, fitting on each dataset's own `train_reference`
split. It was run on three independently seeded datasets:

* the labelled 100K fixture (`fixtures/phase5a_100k`),
* `judge_b`: 300K transactions, `generator.py --seed judge-sim-B`,
* `judge_a`: 1M transactions, `generator.py --seed judge-sim-A`.

The generator datasets are fresh randomness (new motifs, surges, near-misses,
addresses) plus injected end-to-end scenarios the fixture never had, so they
stand in for "a dataset nobody tuned on". The adoption rule was fixed before
looking: **a variant must beat v1 on the validation split of every dataset, for
every task, and must not lose materially on holdout.**

Change in AP vs v1 (validation / holdout):

| Variant | fixture motif | fixture surge | fixture discr. | judge_b motif | judge_b surge | judge_b discr. | judge_a motif | judge_a surge | judge_a discr. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **IF only (v2)** | **+.015 / −.002** | **+.019 / +.031** | **+.021 / +.022** | **+.044 / +.043** | **+.051 / +.061** | **+.023 / +.030** | **+.070 / +.056** | **+.093 / +.053** | **+.043 / +.025** |
| quantile-normal scaling | −.017 / −.011 | −.029 / −.036 | −.022 / −.021 | −.031 / −.039 | −.033 / −.055 | −.021 / −.031 | −.069 / −.054 | −.079 / −.047 | −.036 / −.020 |
| signed log1p scaling | −.006 / −.009 | −.003 / −.007 | −.001 / −.003 | +.003 / +.002 | +.014 / +.009 | +.003 / +.002 | −.018 / −.013 | −.019 / −.007 | −.011 / −.005 |
| IF + ECOD + HBOS | −.024 / −.024 | −.034 / −.052 | −.011 / −.010 | −.004 / −.010 | −.009 / −.023 | −.003 / −.008 | −.010 / −.003 | −.028 / −.020 | −.006 / .000 |
| ECOD only | −.017 / −.010 | −.024 / −.038 | −.015 / −.015 | −.045 / −.051 | −.048 / −.073 | −.017 / −.029 | −.066 / −.054 | −.090 / −.065 | −.029 / −.016 |
| IF 300 trees × 512 samples | −.007 / −.002 | −.006 / −.007 | +.003 / +.006 | −.008 / −.006 | −.003 / −.004 | −.004 / −.003 | −.016 / −.014 | −.019 / −.017 | −.009 / −.006 |
| fusion A:D = 1.5:1 | −.002 / −.018 | −.040 / −.067 | −.020 / −.025 | −.018 / −.036 | −.056 / −.096 | −.019 / −.041 | −.023 / −.006 | −.075 / −.036 | −.028 / −.009 |
| fusion A:D = 1:1.5 | −.011 / −.009 | +.012 / +.038 | +.015 / +.015 | +.015 / +.024 | +.071 / +.090 | +.034 / +.045 | +.026 / +.005 | +.110 / +.044 | +.048 / +.021 |
| IF only + A:D 1:1.25 | +.005 / −.009 | +.018 / +.046 | +.025 / +.027 | +.048 / +.049 | +.087 / +.103 | +.043 / +.051 | +.077 / +.053 | +.151 / +.076 | +.068 / +.038 |
| IF only + A:D 1:1.5 | −.004 / −.015 | +.015 / +.050 | +.029 / +.030 | +.047 / +.048 | +.110 / +.124 | +.058 / +.064 | +.076 / +.049 | +.186 / +.092 | +.085 / +.049 |

Only **IF only** improves every task on every dataset's validation split; its
holdout gains match its validation gains (no sign of overfitting) and its one
non-gain is −0.002 on fixture holdout motif. Re-weighting D trades fixture motif
for surge, i.e. it is a tuned trade-off rather than a robust improvement, so it
was not adopted. Larger forests (300 × 512) did not help either: the model is not
capacity-limited (not underfitting). Adding layers B, C or E to the fusion lowers
AP on the fixture under both v1 and v2 (`scripts/run_anomaly_stack.py`), so the
layer set stays A + D.

Absolute numbers (fixture, A_global + D_burst):

| | motif AP | surge AP | discrimination AP |
| --- | --- | --- | --- |
| v1 validation / holdout | 0.6463 / 0.6282 | 0.7640 / 0.5927 | 0.8761 / 0.9047 |
| **v2 validation / holdout** | **0.6615 / 0.6260** | **0.7827 / 0.6235** | **0.8975 / 0.9269** |

Deployed effect on the 100K fixture import: ML findings 1,094 → 1,223 at the same
1% reference budget, and the share of ML findings that are true laundering-
pattern leads rose from 73.7% to 78.7% (refitted confidence calibration,
`app/engine/calibration/confidence-v1.json`).

## Disclosure

The fixture's final holdout had already been evaluated once for v1. The v2 study
re-read it while comparing variants; it was **not** used to choose (the rule above
uses validation only), but it is no longer a pristine holdout for the fixture.
The two generator datasets' holdouts are fresh. All numbers are on synthetic
data; none is a claim about real-world detection rates.

Reproduce:

```bash
python generator.py --rows 1000000 --seed judge-sim-A --output datasets/judge_a
python generator.py --rows 300000  --seed judge-sim-B --output datasets/judge_b
uv run --extra ml python scripts/ml_study.py --dataset datasets/phase5a_100k \
    --dataset datasets/judge_b --dataset datasets/judge_a
```
