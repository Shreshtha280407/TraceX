"""Phase 7 Part 4 audio/social-benchmark metrics: pure functions, no I/O.

Every function here answers "what did we actually measure" -- never "what
should this model score." A metric whose required ground truth is absent
returns `None` (see each function's docstring for exactly when), matching
`BenchmarkRunV1.metrics`'s own `float | int | None` contract: the *key* is
always present on a real run, the *value* is `None` only when the metric
genuinely cannot be computed from the available labels.

No metric here measures criminality, identity certainty, association
guilt, speaker identity, or investigation truth -- every value is
extraction/model quality only, mirroring
`app.modules.communication_processing.provenance`'s existing confidence
policy.
"""

from __future__ import annotations

import platform
import resource
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.modules.communication_processing.aliases.scripts import Script, detect_script

# --- Generic aggregate helpers --------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (`pct` in `[0, 100]`). `None` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50.0)


def peak_memory_mb() -> float | None:
    """Observed peak resident-set size for this process, in MiB, or `None`.

    `ru_maxrss` is kilobytes on Linux, bytes on macOS/Darwin -- Aditya's
    Mac reports the latter. Never invented: a genuine `OSError`/read
    failure returns `None` rather than a fabricated figure.
    """
    try:
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError, ValueError):
        return None
    if max_rss <= 0:
        return None
    bytes_per_unit = 1.0 if platform.system() == "Darwin" else 1024.0
    return (max_rss * bytes_per_unit) / (1024.0 * 1024.0)


def real_time_factor(*, processing_time_ms: float, audio_duration_ms: float) -> float | None:
    """`processing_time / audio_duration` -- the standard ASR-community convention
    (RTF < 1 means faster than real-time). `None` when the audio duration is
    not positive (nothing to divide by).
    """
    if audio_duration_ms <= 0:
        return None
    return processing_time_ms / audio_duration_ms


# --- Word/character error rate (ASR) --------------------------------------


def _levenshtein_distance(a: Sequence[str], b: Sequence[str]) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous_row = list(range(len(b) + 1))
    for i, token_a in enumerate(a, start=1):
        current_row = [i]
        for j, token_b in enumerate(b, start=1):
            cost = 0 if token_a == token_b else 1
            current_row.append(
                min(
                    previous_row[j] + 1,  # deletion
                    current_row[j - 1] + 1,  # insertion
                    previous_row[j - 1] + cost,  # substitution
                )
            )
        previous_row = current_row
    return previous_row[-1]


def character_error_rate(reference: str, hypothesis: str) -> float | None:
    """`Levenshtein(reference, hypothesis) / len(reference)`. `None` if `reference` is empty."""
    if not reference:
        return None
    return _levenshtein_distance(reference, hypothesis) / len(reference)


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """Same formula as `character_error_rate`, tokenized on whitespace."""
    reference_words = reference.split()
    hypothesis_words = hypothesis.split()
    if not reference_words:
        return None
    return _levenshtein_distance(reference_words, hypothesis_words) / len(reference_words)


# --- VAD: frame-discretized precision/recall/F1/accuracy ------------------


@dataclass(frozen=True)
class TimeInterval:
    """One labelled speech interval, source-relative milliseconds."""

    start_ms: int
    end_ms: int


_DEFAULT_VAD_FRAME_MS = 30


def _frame_flags(
    intervals: Sequence[TimeInterval], *, total_duration_ms: int, frame_size_ms: int
) -> list[bool]:
    num_frames = max(1, total_duration_ms // frame_size_ms)
    flags = [False] * num_frames
    for interval in intervals:
        first = max(0, interval.start_ms // frame_size_ms)
        last = min(num_frames, (interval.end_ms + frame_size_ms - 1) // frame_size_ms)
        for index in range(first, last):
            flags[index] = True
    return flags


def vad_precision_recall_f1(
    predicted: Sequence[TimeInterval],
    ground_truth: Sequence[TimeInterval] | None,
    *,
    total_duration_ms: int,
    frame_size_ms: int = _DEFAULT_VAD_FRAME_MS,
) -> tuple[float | None, float | None, float | None]:
    """Frame-discretized (default 30ms) precision/recall/F1 over speech/non-speech.

    `None` for all three when `ground_truth` is `None` or `total_duration_ms`
    is not positive -- this recording genuinely carries no labels to score
    against.
    """
    if ground_truth is None or total_duration_ms <= 0:
        return None, None, None
    pred_flags = _frame_flags(
        predicted, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    gt_flags = _frame_flags(
        ground_truth, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    true_positive = sum(1 for p, g in zip(pred_flags, gt_flags, strict=True) if p and g)
    false_positive = sum(1 for p, g in zip(pred_flags, gt_flags, strict=True) if p and not g)
    false_negative = sum(1 for p, g in zip(pred_flags, gt_flags, strict=True) if not p and g)
    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive) > 0
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative) > 0
        else 0.0
    )
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def voice_activity_detection_accuracy(
    predicted: Sequence[TimeInterval],
    ground_truth: Sequence[TimeInterval] | None,
    *,
    total_duration_ms: int,
    frame_size_ms: int = _DEFAULT_VAD_FRAME_MS,
) -> float | None:
    """Frame-level speech/non-speech agreement. `None` without ground truth."""
    if ground_truth is None or total_duration_ms <= 0:
        return None
    pred_flags = _frame_flags(
        predicted, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    gt_flags = _frame_flags(
        ground_truth, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    correct = sum(1 for p, g in zip(pred_flags, gt_flags, strict=True) if p == g)
    return correct / len(gt_flags)


# --- Diarization: simplified DER / speaker-turn quality / speaker count --


@dataclass(frozen=True)
class SpeakerTurn:
    """One labelled speaker turn. `speaker_label` is recording-local only --
    never a person identity (see `CLAUDE.md`)."""

    speaker_label: str
    start_ms: int
    end_ms: int


_DEFAULT_DIARIZATION_FRAME_MS = 250
_DEFAULT_TURN_BOUNDARY_TOLERANCE_MS = 250


def _frame_speaker_labels(
    turns: Sequence[SpeakerTurn], *, total_duration_ms: int, frame_size_ms: int
) -> list[str | None]:
    num_frames = max(1, total_duration_ms // frame_size_ms)
    labels: list[str | None] = [None] * num_frames
    for turn in turns:
        first = max(0, turn.start_ms // frame_size_ms)
        last = min(num_frames, (turn.end_ms + frame_size_ms - 1) // frame_size_ms)
        for index in range(first, last):
            labels[index] = turn.speaker_label  # last-writer-wins on overlap (documented below)
    return labels


def diarization_error_rate(
    predicted: Sequence[SpeakerTurn],
    ground_truth: Sequence[SpeakerTurn] | None,
    *,
    total_duration_ms: int,
    frame_size_ms: int = _DEFAULT_DIARIZATION_FRAME_MS,
) -> float | None:
    """A simplified, frame-discretized Diarization Error Rate.

    `DER = (missed + false_alarm + confusion) / total_reference_speech_frames`,
    using a majority-vote frame-overlap mapping from each predicted
    (recording-local) speaker label to the reference label it overlaps
    most -- not the full DER definition (no collar exclusion around
    reference boundaries, no explicit overlapped-speech accounting, an
    overlapping turn at the same frame resolves last-writer-wins). See
    `docs/qa/known-limitations.md` for the full documented simplification.
    `None` when there is no reference, or the reference has zero speech.
    """
    if ground_truth is None or total_duration_ms <= 0:
        return None
    gt_labels = _frame_speaker_labels(
        ground_truth, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    pred_labels = _frame_speaker_labels(
        predicted, total_duration_ms=total_duration_ms, frame_size_ms=frame_size_ms
    )
    total_reference_speech_frames = sum(1 for g in gt_labels if g is not None)
    if total_reference_speech_frames == 0:
        return None

    votes: dict[str, dict[str, int]] = {}
    for predicted_label, reference_label in zip(pred_labels, gt_labels, strict=True):
        if predicted_label is None or reference_label is None:
            continue
        label_votes = votes.setdefault(predicted_label, {})
        label_votes[reference_label] = label_votes.get(reference_label, 0) + 1
    mapping = {
        predicted_label: max(reference_votes, key=lambda key: reference_votes[key])
        for predicted_label, reference_votes in votes.items()
    }

    missed = sum(
        1 for p, g in zip(pred_labels, gt_labels, strict=True) if g is not None and p is None
    )
    false_alarm = sum(
        1 for p, g in zip(pred_labels, gt_labels, strict=True) if p is not None and g is None
    )
    confusion = sum(
        1
        for p, g in zip(pred_labels, gt_labels, strict=True)
        if p is not None and g is not None and mapping.get(p) != g
    )
    return (missed + false_alarm + confusion) / total_reference_speech_frames


def speaker_turn_quality(
    predicted: Sequence[SpeakerTurn],
    ground_truth: Sequence[SpeakerTurn] | None,
    *,
    tolerance_ms: int = _DEFAULT_TURN_BOUNDARY_TOLERANCE_MS,
) -> float | None:
    """This harness's own defined "speaker turn quality": the fraction of
    reference turn *start* boundaries with a matching predicted turn start
    within `tolerance_ms`. Not a standardized external metric name -- a
    boundary-timing-accuracy measure specific to this benchmark. `None`
    without a non-empty reference.
    """
    if not ground_truth:
        return None
    predicted_starts = sorted(turn.start_ms for turn in predicted)
    matched = sum(
        1
        for turn in ground_truth
        if any(abs(turn.start_ms - start) <= tolerance_ms for start in predicted_starts)
    )
    return matched / len(ground_truth)


def speaker_count_error(
    predicted: Sequence[SpeakerTurn], ground_truth: Sequence[SpeakerTurn] | None
) -> int | None:
    """`|distinct predicted speaker labels - distinct reference speaker labels|`."""
    if ground_truth is None:
        return None
    predicted_count = len({turn.speaker_label for turn in predicted})
    reference_count = len({turn.speaker_label for turn in ground_truth})
    return abs(predicted_count - reference_count)


def jaccard_error_rate(
    predicted: Sequence[SpeakerTurn], ground_truth: Sequence[SpeakerTurn] | None
) -> None:
    """Always `None` in this harness.

    A correct Jaccard Error Rate requires the same optimal bipartite
    reference/hypothesis speaker assignment a full diarization-metrics
    toolkit (e.g. `pyannote.metrics`) solves -- this harness's own
    majority-vote mapping (see `diarization_error_rate`) is not that
    assignment, and reusing it here would risk shipping a value under
    JER's real name that does not mean what JER means. Reporting `None`
    is the honest choice this task's own rules require ("never fabricate
    a value where a label/measurement is unavailable") rather than a
    mislabeled approximation.
    """
    return None


# --- Language identification ------------------------------------------


def language_identification_accuracy(
    predicted: Sequence[str], reference: Sequence[str] | None
) -> float | None:
    """`None` without a non-empty reference, or if the lengths disagree
    (a genuine scoring mismatch this function refuses to guess through)."""
    if not reference or len(predicted) != len(reference):
        return None
    correct = sum(1 for p, r in zip(predicted, reference, strict=True) if p == r)
    return correct / len(reference)


# --- Social/chat structured extraction ------------------------------------


def social_extraction_prf(
    expected: Sequence[tuple[str, str]], extracted: Sequence[tuple[str, str]]
) -> tuple[float | None, float | None, float | None]:
    """Precision/recall/F1 over `(mention_type, normalized_value)` pairs.

    `None` for all three when both sides are empty (nothing to score);
    otherwise every expected/extracted mention genuinely counts.
    """
    if not expected and not extracted:
        return None, None, None
    expected_set = set(expected)
    extracted_set = set(extracted)
    true_positive = len(expected_set & extracted_set)
    precision = true_positive / len(extracted_set) if extracted_set else 0.0
    recall = true_positive / len(expected_set) if expected_set else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def language_handling_score(
    texts: Sequence[str], expected_scripts: Sequence[Script]
) -> float | None:
    """Fraction of `texts` whose detected script (the existing, unmodified
    `aliases.scripts.detect_script`) matches the paired `expected_scripts`
    entry. `None` when `expected_scripts` is empty or the lengths disagree.
    """
    if not expected_scripts or len(texts) != len(expected_scripts):
        return None
    matches = sum(
        1
        for text, expected in zip(texts, expected_scripts, strict=True)
        if detect_script(text) == expected
    )
    return matches / len(expected_scripts)


def normalization_validity(records: Sequence[Mapping[str, object]]) -> float | None:
    """Fraction of extracted records that carry every one of this harness's
    documented required keys (`mention_type`, `value`, `locator_present`).
    `None` when there are no records to check -- an empty accepted set is
    not the same claim as "every record was invalid."
    """
    if not records:
        return None
    required_keys = {"mention_type", "value", "locator_present"}
    valid = sum(1 for record in records if required_keys.issubset(record.keys()))
    return valid / len(records)


__all__ = [
    "SpeakerTurn",
    "TimeInterval",
    "character_error_rate",
    "diarization_error_rate",
    "jaccard_error_rate",
    "language_handling_score",
    "language_identification_accuracy",
    "median",
    "normalization_validity",
    "peak_memory_mb",
    "percentile",
    "real_time_factor",
    "social_extraction_prf",
    "speaker_count_error",
    "speaker_turn_quality",
    "voice_activity_detection_accuracy",
    "vad_precision_recall_f1",
    "word_error_rate",
]
