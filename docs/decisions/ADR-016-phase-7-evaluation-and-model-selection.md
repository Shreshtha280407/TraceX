# ADR-016: Phase 7 evaluation contract and model-selection policy

Status: **accepted**. Owner: Nipun. Related: `docs/architecture/
phase-7-evaluation-and-model-governance.md` (full design), `docs/qa/
known-limitations.md` ("Phase 7 Part 1").

## Context

Phase 7 will select and integrate reproducible local models across OCR,
CDR/finance extraction, detection, tracking, ASR, diarization, language
identification, and cross-link correlation, plus decide whether to replace
the existing Phase 5 frozen rules baseline with a learned correlation
model. Five different owners (Jasraj, Gaurav, Sarthak, Aditya, Shreshtha)
will benchmark independently, in parallel, across Parts 2-6. Without a
shared, typed, frozen contract fixed *before* any benchmark runs, two
failure modes become likely: (a) each owner defines "success" slightly
differently, making results incomparable across candidates or across
owners, and (b) the definition of success quietly shifts *after* seeing a
candidate's results, which is exactly the kind of post-hoc rationalization
a genuine benchmark must prevent.

This ADR records the decisions that make Part 1's contract a real
constraint on Parts 2-6, not just documentation they could reasonably
ignore.

## Decision 1: split by case, and freeze the split before any tuning happens

**Case-level splitting, never observation/frame/page/row/clip/message-level.**
A model can look artificially strong if train and evaluation data share
frames from the same video, rows from the same CDR export, or pages from
the same document — the model isn't generalizing, it's memorizing the
specific source. Splitting whole cases into development/validation/holdout
groups (`SyntheticCasePlanV1.case_groups`) and structurally rejecting a
`case_id` in two groups (`CaseGroupV1`'s own field validator, checked
again at the plan level) closes this the same way case isolation already
closes it for graph retrieval elsewhere in this project.

**The split is frozen in Part 1, before Parts 2-4 ever see a candidate's
output.** This is deliberate: if the split were decided *after* seeing
early results, an owner could unconsciously (or consciously) shape which
cases land in "development" to favor their preferred candidate. Freezing
`synthetic-case-plan.v1.json` now, as a committed, typed artifact, means
any later change to case membership is a visible diff against a frozen
baseline, not a silent adjustment.

## Decision 2: Operation Nightfall is structurally separate from tuning

Operation Nightfall is not case #13-15 of the 12-15 planned synthetic
cases — it is a wholly separate dataset entry (`operation_nightfall_v1`,
`role: private_showcase_holdout`) that never appears inside
`SyntheticCasePlanV1.case_groups` at all. This is stronger than simply
putting it in the "holdout" group: a holdout group is still *part of the
same tunable plan*, evaluated repeatedly as different candidates are
compared. Nightfall is evaluated exactly once, at final showcase time,
after every modality candidate and the correlation approach are already
frozen — closer to a final exam than a held-out validation fold. Its truth
file is private and is never committed to Git or shown in the investigator
UI (`truth_file_policy`), so it can score TraceX's own system output
honestly without that scoring becoming a feedback loop a later part could
tune against.

## Decision 3: scores are typed as `float | int | None`, never a free string

`BenchmarkRunV1.metrics: dict[str, float | int | None]` is a narrow type
on purpose. The task's own requirement — "metrics must be typed/value-
validated and cannot contain raw text, media, secret values, real PII, or
full local private paths" — is satisfied structurally: a metric value
*cannot be a string at all*, so raw text or a secret value cannot appear
as a value under any key, by construction, not by a scanner that might
miss a case. The scanner (`validation.check_forbidden_tokens`) still
exists, narrowly, for metric *keys* (`FORBIDDEN_METRIC_KEY_TOKENS`) — a
key literally named `"phone_number"` on an otherwise-innocuous numeric
field is exactly how a real identifier could be smuggled through a "just a
number" value, which the type constraint alone cannot catch.

A free-text description field (`known_limitations`, `allowed_tasks`,
`license_notes`) uses a deliberately *narrower* forbidden-token set
(`FORBIDDEN_CONTENT_TOKENS`, credential-shaped tokens only) than metric
keys do. An early draft of this contract used the same broad list for
both and rejected legitimate prose — "local ASR/**transcription** quality
benchmarking" was refused because "transcript" is a substring of
"transcription". The fix was not to special-case that one word, but to
recognize the actual risk differs by field: a free-text field describing
*what a task covers* should be able to use ordinary domain vocabulary
(transcription, phone number, email, CDR) freely; a *metric key* is a
terse machine identifier where the same vocabulary is a much stronger
signal something is being smuggled through a numeric field. Two token
sets, matched to two different real risks, replaced one token set trying
to serve both.

## Decision 4: a candidate cannot start "selected"

`ModelCandidateV1.selection_status` accepts `candidate`/`conditional`/
`selected` as an enum (so the *type* stays forward-compatible for Parts
2-6 to actually select a winner), but a dedicated field validator on the
model itself raises if `selected` is ever passed during construction. This
means the *data* — this catalogue, right now — cannot describe a winner,
even if a future edit to `model-candidates.v1.json` tried to claim one
prematurely; only code written in a later part (after real benchmark
numbers exist) can ever construct a `selected` record, and even then only
by defining a new code path this ADR does not create.

## Decision 5: the correlation model-selection policy is decided now, not after seeing results

The frozen Phase 5 rules baseline (`phase5-rules-baseline` in the
catalogue) is compared against exactly three ML candidates — Logistic
Regression, XGBoost, LightGBM — on untouched case-level holdout data, per
this fixed policy, decided in Part 1 rather than left to Part 6's
discretion once numbers are in hand:

- ML is selected only if it shows a **material, reproducible** holdout
  improvement, with no unacceptable false-positive or explainability
  regression.
- If Logistic Regression is within a small, documented tolerance of a
  boosted-tree candidate, **Logistic Regression is preferred**, for
  transparency — an investigator-facing "lead for review" system benefits
  more from an explainable linear model than from a marginally sharper
  black box.
- If no ML candidate materially improves on the rules baseline, **the
  rules baseline is retained** — mirroring `ADR-006`'s own "if no tested
  alternative improves the baseline safely, retain the existing profile
  and document that decision" precedent from Phase 5.
- Model output is never a guilt score, regardless of which candidate wins
  — `InvestigatorLeadReportRequirementsV1.prohibited_language` structurally
  requires guilt-conclusion vocabulary to be named as forbidden.

Deciding this policy *before* Part 6 runs any comparison is the point: it
prevents the selection criteria from being quietly adjusted to favor
whichever candidate happens to score best, which would turn a benchmark
into a foregone conclusion dressed up as an evaluation.

## Decision 6: no biometric/face/person-ReID candidate is catalogued

Tracking's only candidate is ByteTrack (motion/IoU-based, evidence-local
track IDs, exactly like the existing Phase 4/5 `IoUTracker` precedent
already documented in `docs/architecture/media-processing-worker.md`: "no
face recognition, no person re-identification — a 'person' box is an
anonymous detection", and `local_track_id` is "never a cross-evidence,
cross-case, or biometric identity claim"). No face-recognition or
appearance-based person-re-identification model is a candidate for any
task in this catalogue. This is not a Phase 7-specific choice — it is
`CLAUDE.md`'s permanent "no biometric/face identity recognition or person
ReID" boundary, restated here so a later part cannot argue Phase 7's
catalogue was silent on it and add one by omission.

## Alternatives considered

- **Let each owner (Jasraj/Gaurav/Sarthak/Aditya/Shreshtha) define their
  own benchmark contract for their own modality.** Rejected: results
  across owners would use different metric names, different split
  strategies, and potentially different leakage rules, making Gate C's
  "lock chosen modality model versions" decision impossible to make on a
  comparable basis. One shared, typed contract, frozen before any
  benchmark runs, is what makes cross-owner comparison meaningful at all.
- **Decide the ML-vs-rules-baseline correlation policy in Part 6, once
  results are available.** Rejected in favor of Decision 5 above — see its
  own reasoning.
- **Use YAML for the four benchmark configs.** Rejected: this project has
  no existing YAML dependency (the frozen dependency list uses JSON/plain
  Python config patterns throughout), and JSON plus this module's own
  pydantic validation gives the same authoring ergonomics without adding
  one.
