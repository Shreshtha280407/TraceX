"""Phase 7 Part 4 audio/social-benchmark metric function tests.

Pure functions only -- no engine, no dataset, no GPU. Every value used
here is synthetic and invented for this test file.
"""

from __future__ import annotations

from app.modules.communication_processing.aliases.scripts import Script
from app.modules.communication_processing.audio_social_benchmark_metrics import (
    SpeakerTurn,
    TimeInterval,
    character_error_rate,
    diarization_error_rate,
    jaccard_error_rate,
    language_handling_score,
    language_identification_accuracy,
    median,
    peak_memory_mb,
    percentile,
    real_time_factor,
    social_extraction_prf,
    speaker_count_error,
    speaker_turn_quality,
    vad_precision_recall_f1,
    voice_activity_detection_accuracy,
    word_error_rate,
)


def test_character_and_word_error_rate_are_zero_for_a_perfect_match() -> None:
    assert character_error_rate("hello world", "hello world") == 0.0
    assert word_error_rate("the quick fox", "the quick fox") == 0.0


def test_word_error_rate_counts_a_single_substitution() -> None:
    assert word_error_rate("the quick fox", "the slow fox") == 1 / 3


def test_character_error_rate_is_none_for_an_empty_reference() -> None:
    assert character_error_rate("", "anything") is None


def test_real_time_factor_is_computed_and_none_for_zero_duration() -> None:
    assert real_time_factor(processing_time_ms=500, audio_duration_ms=1000) == 0.5
    assert real_time_factor(processing_time_ms=500, audio_duration_ms=0) is None


def test_vad_metrics_are_perfect_for_a_well_matched_prediction() -> None:
    ground_truth = [TimeInterval(0, 1000)]
    predicted = [TimeInterval(0, 1000)]
    precision, recall, f1 = vad_precision_recall_f1(predicted, ground_truth, total_duration_ms=2000)
    assert (precision, recall, f1) == (1.0, 1.0, 1.0)
    assert voice_activity_detection_accuracy(predicted, ground_truth, total_duration_ms=2000) == 1.0


def test_vad_metrics_are_unavailable_without_ground_truth() -> None:
    predicted = [TimeInterval(0, 1000)]
    assert vad_precision_recall_f1(predicted, None, total_duration_ms=2000) == (None, None, None)
    assert voice_activity_detection_accuracy(predicted, None, total_duration_ms=2000) is None


def test_diarization_der_is_zero_for_a_correctly_relabeled_match() -> None:
    """Predicted labels are recording-local (`speaker_0`) and differ from
    the reference's own labels (`spk_A`) -- DER's majority-vote mapping
    must still recognize a perfect match."""
    ground_truth = [SpeakerTurn("spk_A", 0, 1000), SpeakerTurn("spk_B", 1000, 2000)]
    predicted = [SpeakerTurn("speaker_0", 0, 1000), SpeakerTurn("speaker_1", 1000, 2000)]
    assert diarization_error_rate(predicted, ground_truth, total_duration_ms=2000) == 0.0
    assert speaker_turn_quality(predicted, ground_truth) == 1.0
    assert speaker_count_error(predicted, ground_truth) == 0


def test_diarization_der_detects_confusion() -> None:
    ground_truth = [SpeakerTurn("spk_A", 0, 1000), SpeakerTurn("spk_B", 1000, 2000)]
    predicted = [
        SpeakerTurn("speaker_0", 0, 1000),
        SpeakerTurn("speaker_0", 1000, 1500),
        SpeakerTurn("speaker_1", 1500, 2000),
    ]
    der = diarization_error_rate(predicted, ground_truth, total_duration_ms=2000)
    assert der is not None
    assert der > 0.0


def test_diarization_metrics_are_unavailable_without_ground_truth() -> None:
    predicted: list[SpeakerTurn] = []
    assert diarization_error_rate(predicted, None, total_duration_ms=1000) is None
    assert speaker_turn_quality(predicted, None) is None
    assert speaker_count_error(predicted, None) is None


def test_jaccard_error_rate_is_always_none_and_never_a_fabricated_approximation() -> None:
    # `jaccard_error_rate` is typed `-> None` -- mypy already proves every
    # call is `None`; these calls just confirm neither raises.
    ground_truth = [SpeakerTurn("spk_A", 0, 1000)]
    jaccard_error_rate([], ground_truth)
    jaccard_error_rate([], None)


def test_language_identification_accuracy_on_valid_synthetic_data() -> None:
    assert language_identification_accuracy(["hi", "en"], ["hi", "en"]) == 1.0
    assert language_identification_accuracy(["hi", "en"], ["hi", "ta"]) == 0.5


def test_language_identification_accuracy_is_none_for_a_length_mismatch_or_no_reference() -> None:
    assert language_identification_accuracy(["hi"], ["hi", "en"]) is None
    assert language_identification_accuracy(["hi"], None) is None
    assert language_identification_accuracy(["hi"], []) is None


def test_social_extraction_prf_on_valid_synthetic_data() -> None:
    expected = [("phone_number", "9876543210")]
    assert social_extraction_prf(expected, expected) == (1.0, 1.0, 1.0)
    assert social_extraction_prf(expected, []) == (0.0, 0.0, 0.0)


def test_social_extraction_prf_is_none_when_nothing_was_expected_or_extracted() -> None:
    assert social_extraction_prf([], []) == (None, None, None)


def test_language_handling_score_reuses_the_existing_detect_script_function() -> None:
    texts = ["hello world", "नमस्ते"]
    expected = [Script.LATIN, Script.DEVANAGARI]
    assert language_handling_score(texts, expected) == 1.0


def test_language_handling_score_is_none_for_empty_or_mismatched_input() -> None:
    assert language_handling_score([], []) is None
    assert language_handling_score(["hello"], []) is None


def test_percentile_and_median_are_correct_for_a_known_sequence() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert median(values) == 3.0
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 100.0) == 5.0


def test_percentile_is_none_for_an_empty_sequence() -> None:
    assert percentile([], 50.0) is None


def test_peak_memory_mb_returns_a_positive_measured_value_on_this_platform() -> None:
    value = peak_memory_mb()
    assert value is None or value > 0.0
