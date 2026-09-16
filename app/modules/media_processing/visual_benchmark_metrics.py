"""Phase 7 Part 3 visual-benchmark metrics: pure functions, no I/O.

Every function here answers "what did we actually measure" -- never "what
should this model score." A metric whose required ground truth is absent
returns `None` (see each function's docstring for exactly when), matching
`BenchmarkRunV1.metrics`'s own `float | int | None` contract: the *key* is
always present on a real run, the *value* is `None` only when the metric
genuinely cannot be computed from the available labels.

None of these functions score investigation accuracy, criminality,
identity, or guilt -- they measure detector/tracker/OCR quality only, the
same "extraction/statement quality, never a probability of guilt" policy
`app.modules.media_processing.provenance` already documents for
`extraction_confidence`.
"""

from __future__ import annotations

import platform
import resource
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.modules.media_processing.analysis.interfaces import ObjectDetection, TrackSegment
from app.modules.media_processing.image.geometry import PixelBoundingBox

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


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


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


# --- Character/word error rate and field extraction (visual text / OCR) ---


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


def field_extraction_prf(
    expected: Mapping[str, str], extracted: Mapping[str, str]
) -> tuple[float | None, float | None, float | None]:
    """Precision/recall/F1 over field values (both sides case/whitespace-normalized).

    `None` for all three when `expected` is empty (nothing to score against)
    and `extracted` is also empty (nothing attempted) -- otherwise every
    field genuinely counts as a hit or a miss, never silently dropped.
    """
    if not expected and not extracted:
        return None, None, None

    def _norm(value: str) -> str:
        return " ".join(value.strip().lower().split())

    expected_norm = {key: _norm(value) for key, value in expected.items()}
    extracted_norm = {key: _norm(value) for key, value in extracted.items()}

    true_positive = sum(
        1
        for key, value in extracted_norm.items()
        if key in expected_norm and expected_norm[key] == value
    )
    predicted_count = len(extracted_norm)
    expected_count = len(expected_norm)

    precision = true_positive / predicted_count if predicted_count else None
    recall = true_positive / expected_count if expected_count else None
    f1 = (
        (2 * precision * recall) / (precision + recall)
        if precision is not None and recall is not None and (precision + recall) > 0
        else (0.0 if precision == 0.0 or recall == 0.0 else None)
    )
    return precision, recall, f1


# --- Detection: precision / recall / mAP (IoU-based, VOC-style AP) --------


@dataclass(frozen=True)
class GroundTruthBox:
    """One labelled object in one frame/image -- never a person's identity."""

    label: str
    box: PixelBoundingBox


def _iou(a: PixelBoundingBox, b: PixelBoundingBox) -> float:
    x_min, y_min = max(a.x_min, b.x_min), max(a.y_min, b.y_min)
    x_max, y_max = min(a.x_max, b.x_max), min(a.y_max, b.y_max)
    if x_max <= x_min or y_max <= y_min:
        return 0.0
    intersection = (x_max - x_min) * (y_max - y_min)
    area_a = (a.x_max - a.x_min) * (a.y_max - a.y_min)
    area_b = (b.x_max - b.x_min) * (b.y_max - b.y_min)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


_DETECTION_IOU_THRESHOLD = 0.5


def _average_precision_for_label(
    predictions: Sequence[ObjectDetection], ground_truth: Sequence[GroundTruthBox]
) -> float:
    """All-point-interpolated Average Precision at IoU >= 0.5 for one label.

    Standard VOC-style AP over a single IoU threshold -- not COCO's
    mAP@[.5:.95] sweep. Predictions are consumed highest-confidence-first;
    each ground-truth box may be matched at most once.
    """
    if not ground_truth:
        return 0.0
    ordered = sorted(predictions, key=lambda p: p.confidence, reverse=True)
    matched_gt: set[int] = set()
    true_positive = [0] * len(ordered)
    false_positive = [0] * len(ordered)
    for rank, prediction in enumerate(ordered):
        best_index, best_iou = None, _DETECTION_IOU_THRESHOLD
        for gt_index, gt in enumerate(ground_truth):
            if gt_index in matched_gt:
                continue
            score = _iou(prediction.box, gt.box)
            if score >= best_iou:
                best_index, best_iou = gt_index, score
        if best_index is not None:
            matched_gt.add(best_index)
            true_positive[rank] = 1
        else:
            false_positive[rank] = 1

    cumulative_tp = 0
    cumulative_fp = 0
    precisions: list[float] = []
    recalls: list[float] = []
    for tp, fp in zip(true_positive, false_positive, strict=True):
        cumulative_tp += tp
        cumulative_fp += fp
        precisions.append(cumulative_tp / (cumulative_tp + cumulative_fp))
        recalls.append(cumulative_tp / len(ground_truth))

    # 11-point interpolation (recall thresholds 0.0, 0.1, ..., 1.0).
    interpolated: list[float] = []
    for threshold_index in range(11):
        recall_threshold = threshold_index / 10.0
        precisions_at_or_above = [
            p for p, r in zip(precisions, recalls, strict=True) if r >= recall_threshold
        ]
        interpolated.append(max(precisions_at_or_above) if precisions_at_or_above else 0.0)
    return sum(interpolated) / len(interpolated)


def detection_precision_recall_map(
    predictions: Sequence[ObjectDetection], ground_truth: Sequence[GroundTruthBox] | None
) -> tuple[float | None, float | None, float | None]:
    """Precision/recall (single IoU=0.5 threshold, all predictions counted) and mAP.

    `None` for all three when `ground_truth` is `None` or empty -- this
    dataset/sample genuinely carries no labels to score against, and this
    function never fabricates a value in that case.
    """
    if not ground_truth:
        return None, None, None

    matched_gt: set[int] = set()
    true_positive = 0
    for prediction in predictions:
        best_index, best_iou = None, _DETECTION_IOU_THRESHOLD
        for gt_index, gt in enumerate(ground_truth):
            if gt_index in matched_gt or gt.label != prediction.label:
                continue
            score = _iou(prediction.box, gt.box)
            if score >= best_iou:
                best_index, best_iou = gt_index, score
        if best_index is not None:
            matched_gt.add(best_index)
            true_positive += 1

    predicted_count = len(predictions)
    precision = true_positive / predicted_count if predicted_count else 0.0
    recall = true_positive / len(ground_truth)

    labels = sorted({gt.label for gt in ground_truth})
    average_precisions = [
        _average_precision_for_label(
            [p for p in predictions if p.label == label],
            [gt for gt in ground_truth if gt.label == label],
        )
        for label in labels
    ]
    mean_average_precision = _mean(average_precisions)
    return precision, recall, mean_average_precision


# --- Tracking: IDF1 / MOTA / ID switches (HOTA intentionally None) --------


@dataclass(frozen=True)
class GroundTruthTrack:
    """One labelled within-video track -- a technical label, never an identity."""

    track_id: str
    label: str
    boxes_by_time_ms: Mapping[int, PixelBoundingBox]


_TRACKING_IOU_THRESHOLD = 0.5


def _match_per_frame(
    predicted: Sequence[TrackSegment], ground_truth: Sequence[GroundTruthTrack]
) -> dict[int, dict[str, str]]:
    """For each timestamp, map each matched ground-truth track_id -> predicted local_track_id."""
    all_times = sorted({t for gt in ground_truth for t in gt.boxes_by_time_ms})
    matches_by_time: dict[int, dict[str, str]] = {}
    for time_ms in all_times:
        gt_at_time = [
            (gt, gt.boxes_by_time_ms[time_ms])
            for gt in ground_truth
            if time_ms in gt.boxes_by_time_ms
        ]
        pred_at_time = [
            (track, track.boxes_by_time_ms[time_ms])
            for track in predicted
            if time_ms in track.boxes_by_time_ms
        ]
        used_pred: set[int] = set()
        frame_matches: dict[str, str] = {}
        for gt, gt_box in gt_at_time:
            best_index, best_iou = None, _TRACKING_IOU_THRESHOLD
            for pred_index, (track, pred_box) in enumerate(pred_at_time):
                if pred_index in used_pred or track.label != gt.label:
                    continue
                score = _iou(gt_box, pred_box)
                if score >= best_iou:
                    best_index, best_iou = pred_index, score
            if best_index is not None:
                used_pred.add(best_index)
                frame_matches[gt.track_id] = pred_at_time[best_index][0].local_track_id
        matches_by_time[time_ms] = frame_matches
    return matches_by_time


def id_switches(
    predicted: Sequence[TrackSegment], ground_truth: Sequence[GroundTruthTrack] | None
) -> int | None:
    """Count of times a ground-truth track's matched predicted ID changes frame-to-frame.

    `None` when there is no ground truth to compare against.
    """
    if not ground_truth:
        return None
    matches_by_time = _match_per_frame(predicted, ground_truth)
    switches = 0
    last_matched_id: dict[str, str] = {}
    for time_ms in sorted(matches_by_time):
        for track_id, predicted_id in matches_by_time[time_ms].items():
            previous = last_matched_id.get(track_id)
            if previous is not None and previous != predicted_id:
                switches += 1
            last_matched_id[track_id] = predicted_id
    return switches


def mota(
    predicted: Sequence[TrackSegment], ground_truth: Sequence[GroundTruthTrack] | None
) -> float | None:
    """Multi-Object Tracking Accuracy: `1 - (FN + FP + IDSW) / total_ground_truth_boxes`.

    `None` when there is no ground truth, or it contains zero boxes.
    """
    if not ground_truth:
        return None
    total_gt_boxes = sum(len(gt.boxes_by_time_ms) for gt in ground_truth)
    if total_gt_boxes == 0:
        return None
    matches_by_time = _match_per_frame(predicted, ground_truth)
    false_negatives = 0
    for time_ms, frame_matches in matches_by_time.items():
        gt_ids_at_time = {gt.track_id for gt in ground_truth if time_ms in gt.boxes_by_time_ms}
        false_negatives += len(gt_ids_at_time) - len(frame_matches)
    total_pred_boxes = sum(len(track.boxes_by_time_ms) for track in predicted)
    total_matched = sum(len(frame_matches) for frame_matches in matches_by_time.values())
    false_positives = total_pred_boxes - total_matched
    switches = id_switches(predicted, ground_truth) or 0
    return 1.0 - (false_negatives + false_positives + switches) / total_gt_boxes


def idf1(
    predicted: Sequence[TrackSegment], ground_truth: Sequence[GroundTruthTrack] | None
) -> float | None:
    """A simplified, majority-vote IDF1: not the optimal network-flow assignment.

    For each ground-truth track, the predicted `local_track_id` matched to
    it most often becomes its "identity assignment"; IDF1 is then computed
    from that assignment's true/false positive/negative frame counts. A
    full IDF1 (e.g. as `py-motmetrics` computes it) solves a global
    bipartite assignment instead -- documented as a deliberate scope
    boundary for this benchmark harness, not an unstated approximation.
    `None` when there is no ground truth.
    """
    if not ground_truth:
        return None
    matches_by_time = _match_per_frame(predicted, ground_truth)

    id_tp = 0
    id_fn = 0
    id_fp = 0
    for gt in ground_truth:
        votes: dict[str, int] = {}
        for time_ms in gt.boxes_by_time_ms:
            matched_id = matches_by_time.get(time_ms, {}).get(gt.track_id)
            if matched_id is not None:
                votes[matched_id] = votes.get(matched_id, 0) + 1
        if not votes:
            id_fn += len(gt.boxes_by_time_ms)
            continue
        assigned_id = max(votes, key=lambda key: votes[key])
        for time_ms in gt.boxes_by_time_ms:
            matched_id = matches_by_time.get(time_ms, {}).get(gt.track_id)
            if matched_id == assigned_id:
                id_tp += 1
            else:
                id_fn += 1
        assigned_track = next((t for t in predicted if t.local_track_id == assigned_id), None)
        if assigned_track is not None:
            id_fp += sum(
                1
                for time_ms in assigned_track.boxes_by_time_ms
                if matches_by_time.get(time_ms, {}).get(gt.track_id) != assigned_id
            )

    denominator = 2 * id_tp + id_fp + id_fn
    return (2 * id_tp) / denominator if denominator > 0 else None


def hota(
    predicted: Sequence[TrackSegment], ground_truth: Sequence[GroundTruthTrack] | None
) -> None:
    """Always `None` in this harness.

    HOTA requires a geometric-mean-of-detection/association-accuracy sweep
    across multiple IoU/alpha thresholds -- genuinely complex to implement
    correctly, and a partial/simplified HOTA would be easy to misread as
    the real metric. Reporting `None` here is the honest choice this
    task's own rules require ("never fabricate a value where a
    label/measurement is unavailable") rather than shipping an
    approximation under the real metric's name.
    """
    return None


__all__ = [
    "GroundTruthBox",
    "GroundTruthTrack",
    "character_error_rate",
    "detection_precision_recall_map",
    "field_extraction_prf",
    "hota",
    "id_switches",
    "idf1",
    "median",
    "mota",
    "peak_memory_mb",
    "percentile",
    "word_error_rate",
]
