"""Phase 7 Part 2: pure benchmark-metric function tests. No files, no engines."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.benchmark_metrics import (
    character_error_rate,
    field_extraction_prf,
    median,
    peak_memory_mb,
    percentile,
    word_error_rate,
)


def test_cer_is_zero_for_identical_text() -> None:
    assert character_error_rate("hello world", "hello world") == 0.0


def test_cer_counts_character_edits() -> None:
    assert character_error_rate("hello", "hallo") == pytest.approx(0.2)


def test_cer_is_none_with_no_reference() -> None:
    assert character_error_rate("", "anything") is None


def test_wer_is_zero_for_identical_text() -> None:
    assert word_error_rate("the quick brown fox", "the quick brown fox") == 0.0


def test_wer_counts_word_edits() -> None:
    assert word_error_rate("the quick fox", "the slow fox") == pytest.approx(1 / 3)


def test_wer_is_none_with_no_reference() -> None:
    assert word_error_rate("", "anything") is None


def test_field_extraction_prf_perfect_match() -> None:
    expected = {"fir_reference": "A1", "phone_number": "9876543210"}
    precision, recall, f1 = field_extraction_prf(expected, dict(expected))
    assert (precision, recall, f1) == (1.0, 1.0, 1.0)


def test_field_extraction_prf_partial_match() -> None:
    expected = {"fir_reference": "A1", "phone_number": "9876543210"}
    extracted = {"fir_reference": "A1", "phone_number": "0000000000"}
    precision, recall, f1 = field_extraction_prf(expected, extracted)
    assert precision == pytest.approx(0.5)
    assert recall == pytest.approx(0.5)
    assert f1 == pytest.approx(0.5)


def test_field_extraction_prf_false_positive_lowers_precision_only() -> None:
    expected = {"fir_reference": "A1"}
    extracted = {"fir_reference": "A1", "phone_number": "9876543210"}
    precision, recall, f1 = field_extraction_prf(expected, extracted)
    assert precision == pytest.approx(0.5)
    assert recall == pytest.approx(1.0)
    assert f1 is not None


def test_field_extraction_prf_is_none_with_no_expected_fields() -> None:
    assert field_extraction_prf({}, {"a": "b"}) == (None, None, None)


def test_field_extraction_prf_zero_precision_with_no_extraction() -> None:
    precision, recall, f1 = field_extraction_prf({"a": "b"}, {})
    assert (precision, recall, f1) == (0.0, 0.0, 0.0)


def test_percentile_nearest_rank() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 50.0) == 30.0
    assert percentile(values, 100.0) == 50.0


def test_percentile_is_none_for_empty_sequence() -> None:
    assert percentile([], 50.0) is None


def test_percentile_rejects_out_of_bounds_pct() -> None:
    with pytest.raises(ValueError, match="0, 100"):
        percentile([1.0], 150.0)


def test_median_matches_percentile_50() -> None:
    values = [3.0, 1.0, 2.0]
    assert median(values) == percentile(values, 50.0)


def test_peak_memory_mb_is_a_positive_float_or_none() -> None:
    value = peak_memory_mb()
    assert value is None or value > 0.0
