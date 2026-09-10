"""Scenario 18: script detection handles Latin, Devanagari, Gurmukhi, and mixed/unknown text."""

from __future__ import annotations

from app.modules.communication_processing.aliases.scripts import Script, detect_script

# Devanagari "Ram" (RA + AA-matra + MA) and Gurmukhi "Ram" (RA + AA-matra + MA).
_DEVANAGARI_RAM = "राम"
_GURMUKHI_RAM = "ਰਾਮ"
_CYRILLIC_TEXT = "Привет"  # "Privet" in Cyrillic -- unrecognized script


def test_pure_latin_text() -> None:
    assert detect_script("Ram Singh") is Script.LATIN


def test_pure_devanagari_text() -> None:
    assert detect_script(_DEVANAGARI_RAM) is Script.DEVANAGARI


def test_pure_gurmukhi_text() -> None:
    assert detect_script(_GURMUKHI_RAM) is Script.GURMUKHI


def test_digits_and_punctuation_only_is_unknown() -> None:
    assert detect_script("12345-6789") is Script.UNKNOWN


def test_empty_string_is_unknown() -> None:
    assert detect_script("") is Script.UNKNOWN


def test_unrecognized_script_is_unknown() -> None:
    assert detect_script(_CYRILLIC_TEXT) is Script.UNKNOWN


def test_mixed_latin_and_devanagari_is_mixed() -> None:
    assert detect_script(f"Ram {_DEVANAGARI_RAM}") is Script.MIXED


def test_mixed_latin_and_unrecognized_is_mixed() -> None:
    assert detect_script(f"Ram {_CYRILLIC_TEXT}") is Script.MIXED


def test_detection_ignores_digits_and_whitespace() -> None:
    assert detect_script("Ram123 Singh!!") is Script.LATIN
