# Model decision: retain v2; E4 is synthetic research only

Decision date: 2026-10-03. No production scoring procedure or release ID is
changed. The frozen production baseline is `anomaly-stack-v2`: global
Isolation Forest ranks plus retrospective containing-bucket D burst,
equal-weight Stouffer fusion, seed 42, refitted on each completed snapshot's
earliest 70%. ECOD describes feature tails; it does not attribute the IF score.
The reference 99th-percentile threshold is not a hard final queue cap.
Reference rows are in-sample, not online forecasts. The separate distinct-TX
queue uses floor(K/fraction), lexical ties, no unflagged fill and snapshot scope.

Production eligibility: unsupervised v2 only. The synthetic-trained selected
comparator is **not eligible for automatic adoption**, even when it beats v2.
There is no v3 release, shipped XGBoost/LightGBM model or claim of real-world
precision improvement. Operational reliability and tested scoring parity are
separate from research ranking gains.

## Frozen protocol and selection

Protocol: `experiments/release_review_protocol_20261003.json`, SHA-256
`e025b2d987d80574407b99057a68b5905f6bb50dc3e4644ee6da9daf939fea7c`.
Production release manifest SHA-256:
`eaa862db090baf74db92ff69cddc53eb89e44389f48734eae381f9180542dda4`.
Research feature-column contract SHA-256:
`34db72ce46c2a53e963b7e8c452cf2ffb21c3adcfd05b3c10ff20b19e414d9b8`.
The frozen manifest's historical “ECOD attribution” wording is not a valid
interpretation; the current finding explanations and UI explicitly correct it.

Development report: `experiments/runs/controlled-development-20261003-final-01.json`.
Selection: `experiments/runs/frozen-research-selection-20261003.json`.
Final report: `experiments/runs/reserved-final-evaluation-20261003.json`.
All are retained locally; raw reports are intentionally ignored by Git.

The controlled grid includes E1 HGB, E2 training-only balanced HGB, E3 five
feature-group ablations, E4 XGBoost, E5 LightGBM, E6 supervised/A/D hybrid and
E7 separate isotonic calibration. Each task has its own classifier/target.
The fixed seed is 42 and the budget is 100 iterations per candidate, not an
unbounded hyperparameter search. Versions are recorded: sklearn 1.9.1,
numpy 2.5.3, scipy 1.18.1, xgboost-cpu 3.4.1, lightgbm 4.7.0.
Research dependencies are optional and excluded from the production image.

Features use structure, backwards input ages, strictly prior recipient
activity/value/gaps, last completed motif bucket, bounded historical graph
context and prior endpoint/country context. No IDs, scenario/family labels,
source locators, final-snapshot clusters or future spends are model inputs.
Equal-time peers do not enter prior-history features. Truncation/cutoff tests
cover the new causal feature procedure, not a claim that v2 D is causal.

Every supervised fit uses the first 80% of the chronological reference period;
the remaining 20% is calibration. Validation alone selects the candidate by
worst motif/surge AP over all three development datasets, subject to a 0.01
discrimination-AP regression tolerance against the matched v2 comparator.
E4 XGBoost wins with minimum validation AP 0.8356817371. Final reservations
were written exclusively before final generation; final evaluation markers
prevent reuse. The frozen procedure is fitted on each dataset's own earlier
reference data: this is **not transfer of frozen fitted weights**.
The runner also rejects changes to the registered iteration budget, verifies
the frozen validation report hashes/baseline identity and checks the feature
contract before a future final fit. These guards were checked against the saved
development report without reopening or rerunning either evaluated final.

## Per-dataset ranking evidence

AP is threshold-independent. Discrimination uses only motifs and labelled
near-misses, not all transactions. Counts below are evaluation populations.

| Dataset / split | Population motif/surge | v2 AP motif / surge / discr. | E4 AP motif / surge / discr. | E4 P@100 motif / surge |
| --- | ---: | --- | --- | --- |
| 100K development / validation | 15,000 | .6615 / .7827 / .8975 | .8618 / .8357 / .9997 | .97 / .98 |
| 303,850 development / validation | 101,436 | .6546 / .7012 / .8017 | .9301 / .9270 / 1.0000 | 1.00 / 1.00 |
| 1,014,581 development / validation | 101,763 | .5493 / .5724 / .7558 | .9923 / .9252 / 1.0000 | 1.00 / 1.00 |
| Final A / reserved final | 102,951 | .6014 / .5789 / .7917 | .9884 / .8846 / .9999 | 1.00 / 1.00 |
| Final B / reserved final | 105,298 | .6140 / .6828 / .7956 | .9269 / .8689 / .9999 | 1.00 / .99 |

The deterministic rule comparator remains a separate benchmark, not a combined
production rank. Its AP (motif / surge / restricted discrimination) is 100K
.4005/.3479/.9093; development 300K .3197/.2166/.8436; development 1M
.3025/.2050/.8401; final A .3118/.2005/.8567; final B .3348/.2695/.8434.

Final A contains 302,079 canonical transactions, scenario prevalence multiplier
0.5, and registered missing-endpoint/prevout stress. Final B contains 306,873,
prevalence multiplier 1.5, minute-rounded timing and endpoint concentration
skew/missingness. Seeds, generator hashes, every source hash and truth hash
are in their retained dataset manifests and the final report.

| Reserved final / task | Positives / population (random AP) | AP 95% interval | P@1% / recall@1% | Threshold P / R / F1 | TP / FP / FN / TN |
| --- | --- | --- | --- | --- | --- |
| A motif | 3,378 / 102,951 (.03281) | [.98160, .99093] | 1.0000 / .3046 | 1.0000 / .3840 / .5549 | 1297 / 0 / 2081 / 99573 |
| A surge | 1,677 / 102,951 (.01629) | [.80204, .91814] | .9514 / .5838 | .8242 / .7800 / .8015 | 1308 / 279 / 369 / 100995 |
| B motif | 3,085 / 105,298 (.02930) | [.90722, .94426] | .9952 / .3394 | .9929 / .3611 / .5296 | 1114 / 8 / 1971 / 102205 |
| B surge | 1,411 / 105,298 (.01340) | [.76856, .91747] | .9097 / .6782 | .9018 / .7030 / .7901 | 992 / 108 / 419 / 103779 |

Discrimination populations are 6,528 (A) and 6,254 (B), positive prevalences
.51746 and .49328. Their AP intervals are [.999818, .999951] and
[.999722, .999945]. At 1% of that restricted population, capacity is only
65/62 and recall is .01924/.02010, despite precision 1.0. Do not hide this
small coverage behind P@100=1.0. Complete discrimination confusion counts,
threshold metrics and reliability bins are in the raw report.

Across the two reserved finals, E4 mean/worst AP is motif .957616/.926867,
surge .876772/.868913, discrimination .999885/.999867. Matched v2 means are
.607680/.630852/.793624. Across all five evaluation sets E4 worst motif AP
is .861775 and worst surge AP .835682. Initial .80 point targets and >=.90
P@100 pass. Final point estimates exceed the .85 preferred target; the .90
surge stretch target is missed. The final-B surge interval extends below .80.
Intervals use 100 scenario/episode/recipient-entity cluster bootstrap draws,
conditional on the fitted models, not independent row bootstrap or a guarantee.

## Calibration and limitations

E7 calibrates only on the separate earlier calibration period; it does not
change ranking. E4 motif Brier/ECE: A .003115/.001820; surge A
.004762/.001282, B .004095/.001544. These describe synthetic targets only.
No such research calibration is installed as a real-case percentage.

The separately regenerated deterministic/ML confidence diagnostic is
`experiments/runs/confidence-reliability-20261003.json`. It is **not the active
registry**, despite its historical `confidence-v1` identifier/“shipped” wording.
Its latest-cited-time split correctly excludes final-period source references.
Validation ML Brier .187840 versus constant-rate .204742, ECE .093288,
n=306. CoinJoin Brier .226755, ECE .205253, n=176. Collection/hub validation
positives are only 4/2. Peeling Brier .042780 is worse than constant .016144;
rapid-redistribution Brier .872810 and ECE .879798 are poor. These maps are
not promoted and do not establish uniform calibration improvement.

The shipped registry's numerical maps stay unchanged. Provenance is pinned
per finding, lookup is compound-versioned and shifted/unregistered/legacy
data remains unknown rather than acquiring a synthetic percentage. Network
1-adjusted-p is a transformed test statistic, not a posterior. Fused Gaussian
tails have unvalidated dependence/distribution assumptions.

Important limitations: same generator, independently seeded—not independently
authored datasets or real-world validation. Observable registered stress and
varied prevalence are additional construction rules, not generator tuning.
Reference/final recipient and scenario-cluster overlap is zero in both finals;
validation/final overlap remains documented. Only benign payroll is excluded
from supervised training/calibration, with 4/8 held-family final rows and no
flags; this is **not positive scenario-family generalization**. Strong positive
family holdouts and independent real-data validation remain NOT RUN.

Analyst labels are proposition-scoped, versioned, case/snapshot/source/feature
hash/cutoff-linked: confirmed positive, dismissed negative, unresolved unknown.
Minimum 50 resolved labels and 10 per class do not resolve selection bias;
representative review policy and independent validation are still required.
No automatic production supervised training is enabled.

## Reproduction

Use new output names; do not re-open reserved finals for selection. Their
canonical inputs were retired under the user's explicit disposable-data
approval after hashes/evaluation were saved; manifests, truth and reservation
markers remain. Regeneration must match their recorded hashes and does not
make a previously evaluated holdout pristine again.

```bash
uv run --extra ml --extra research python scripts/ml_study.py --help
uv run --extra ml --extra research python scripts/freeze_research_selection.py --help
uv run --extra ml python scripts/ml_output_parity.py RUN_A RUN_B --output NEW.json
```

The final acceptance report separately records scale, offline installation,
browser, resource limits and regression checks. Passing this synthetic ranking
experiment cannot substitute for a failed scale or production-quality gate.
