"""Scenarios 19-21: transliteration stability, no-invented-candidates, and non-name exclusion."""

from __future__ import annotations

from app.modules.communication_processing.aliases.scripts import Script
from app.modules.communication_processing.aliases.transliteration import (
    CATEGORY_DETERMINISTIC_TRANSLITERATION,
    CATEGORY_EXACT_NORMALIZED,
    CATEGORY_NOT_GENERATED,
    generate_candidates,
    transliterate_devanagari_token,
    transliterate_gurmukhi_token,
)

_DEVANAGARI_RAM = "राम"
_DEVANAGARI_SINGH = "सिंह"
_GURMUKHI_RAM = "ਰਾਮ"
_CYRILLIC_TEXT = "Привет"


def test_devanagari_transliteration_is_stable_and_deterministic() -> None:
    first = transliterate_devanagari_token(_DEVANAGARI_RAM)
    second = transliterate_devanagari_token(_DEVANAGARI_RAM)
    assert first == second == "raama"


def test_devanagari_transliteration_handles_anusvara() -> None:
    assert transliterate_devanagari_token(_DEVANAGARI_SINGH) == "sinha"


def test_gurmukhi_transliteration_is_stable() -> None:
    first = transliterate_gurmukhi_token(_GURMUKHI_RAM)
    second = transliterate_gurmukhi_token(_GURMUKHI_RAM)
    assert first == second == "raama"


def test_unsupported_script_produces_no_candidate() -> None:
    """Scenario 20: Cyrillic isn't covered by any mapping -- no invented transliteration."""
    result = generate_candidates(_CYRILLIC_TEXT)
    assert result.category == CATEGORY_NOT_GENERATED
    assert result.script is Script.UNKNOWN


def test_unmappable_devanagari_character_produces_no_candidate() -> None:
    """A Devanagari character outside the small documented table (e.g. a digit) fails closed."""
    devanagari_digit_one = "१"  # DEVANAGARI DIGIT ONE, not in the letter/matra table
    assert transliterate_devanagari_token(devanagari_digit_one) is None
    result = generate_candidates(devanagari_digit_one)
    assert result.category in {CATEGORY_NOT_GENERATED, CATEGORY_EXACT_NORMALIZED}
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION


def test_latin_candidate_is_exact_normalized_only() -> None:
    result = generate_candidates("Ram Singh")
    assert result.category == CATEGORY_EXACT_NORMALIZED
    assert result.candidates == ("ram singh",)


def test_devanagari_candidate_includes_both_exact_and_transliteration() -> None:
    result = generate_candidates(_DEVANAGARI_RAM)
    assert result.category == CATEGORY_DETERMINISTIC_TRANSLITERATION
    assert "raama" in result.candidates
    assert _DEVANAGARI_RAM in result.candidates


def test_candidate_exposes_method_and_version() -> None:
    result = generate_candidates("Ram")
    assert result.method
    assert result.method_version


def test_phone_number_is_never_transliterated_as_a_name() -> None:
    """Scenario 21: phone numbers are excluded from transliteration."""
    result = generate_candidates("9876543210")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION
    assert "excluded" in (result.reason or "")


def test_email_is_never_transliterated_as_a_name() -> None:
    result = generate_candidates("alice@example.com")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION


def test_url_is_never_transliterated_as_a_name() -> None:
    result = generate_candidates("https://example.com/path")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION


def test_handle_is_never_transliterated_as_a_name() -> None:
    result = generate_candidates("@some_handle")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION


def test_digit_bearing_id_is_never_transliterated_as_a_name() -> None:
    result = generate_candidates("ACC12345")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION


def test_devanagari_token_with_digit_is_never_transliterated() -> None:
    result = generate_candidates(_DEVANAGARI_RAM + "123")
    assert result.category != CATEGORY_DETERMINISTIC_TRANSLITERATION
