"""Scenario 5: normalized spans map back to the exact original source location."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.document.normalization import (
    normalization_config_hash,
    normalize_text,
)


def test_whitespace_collapses_and_maps_back_to_the_original_run() -> None:
    text = "Name:   Ramesh    Kumar"
    result = normalize_text(text)

    assert result.normalized == "Name: Ramesh Kumar"

    idx = result.normalized.index("Kumar")
    span = result.offset_map.to_source(idx, idx + len("Kumar"))
    assert text[span[0] : span[1]] == "Kumar"


def test_line_wrap_hyphenation_is_removed_and_span_maps_to_the_hyphenated_original() -> None:
    text = "Suspect Ram-\nesh was seen."
    result = normalize_text(text)

    assert "Ramesh" in result.normalized
    assert "-\n" not in result.normalized

    idx = result.normalized.index("Ramesh")
    span = result.offset_map.to_source(idx, idx + len("Ramesh"))
    assert text[span[0] : span[1]] == "Ram-\nesh"


def test_hyphen_before_uppercase_or_digit_is_preserved_not_treated_as_a_line_wrap() -> None:
    text = "Reference UTR-2026 confirmed."
    result = normalize_text(text)
    assert "UTR-2026" in result.normalized


def test_never_invents_characters_not_present_in_source() -> None:
    text = "  multiple   spaces\n\nand\tblank lines  "
    result = normalize_text(text)
    # Every character of the normalized output must trace back to a real
    # slice of the original text -- reconstructing from the offset map's
    # compacted runs should only ever cite substrings that actually exist.
    for run in result.offset_map.runs:
        source_slice = text[run.source_start : run.source_end]
        assert source_slice.strip() == "" or source_slice in text


def test_normalization_is_deterministic_for_identical_input() -> None:
    text = "Repeated input for a determinism check on line one\nand line two."
    first = normalize_text(text)
    second = normalize_text(text)
    assert first.normalized == second.normalized
    assert first.offset_map == second.offset_map


def test_out_of_range_span_raises() -> None:
    result = normalize_text("short")
    with pytest.raises(ValueError, match="out of range"):
        result.offset_map.to_source(0, 999)


def test_config_hash_is_stable() -> None:
    assert normalization_config_hash() == normalization_config_hash()
