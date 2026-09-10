"""Scenario 17: Unicode normalization is stable."""

from __future__ import annotations

from app.modules.communication_processing.aliases.normalize import (
    comparison_key,
    normalize_unicode,
    normalize_whitespace,
    safe_tokenize,
)

# Explicit escapes (never hand-typed glyphs) so the two forms are
# unambiguously different code point sequences representing the same
# visual character: precomposed "e with acute" vs "e" + combining acute.
_COMPOSED_E_ACUTE = "é"
_DECOMPOSED_E_ACUTE = "é"


def test_nfc_normalization_is_idempotent() -> None:
    text = "caf" + _DECOMPOSED_E_ACUTE
    once = normalize_unicode(text)
    twice = normalize_unicode(once)
    assert once == twice


def test_nfc_unifies_composed_and_decomposed_forms() -> None:
    assert _COMPOSED_E_ACUTE != _DECOMPOSED_E_ACUTE  # different code points before normalization
    assert normalize_unicode(_COMPOSED_E_ACUTE) == normalize_unicode(_DECOMPOSED_E_ACUTE)
    assert normalize_unicode(_DECOMPOSED_E_ACUTE) == _COMPOSED_E_ACUTE  # NFC recomposes


def test_whitespace_normalization_collapses_and_strips() -> None:
    assert normalize_whitespace("  Ram   Singh \n\t") == "Ram Singh"


def test_comparison_key_is_case_and_whitespace_insensitive() -> None:
    assert comparison_key("  Ram Singh ") == comparison_key("ram   singh")


def test_comparison_key_is_deterministic() -> None:
    assert comparison_key("Test Value") == comparison_key("Test Value")


def test_safe_tokenize_splits_on_whitespace() -> None:
    assert safe_tokenize("Ram   Singh\tKumar") == ["Ram", "Singh", "Kumar"]


def test_safe_tokenize_empty_string() -> None:
    assert safe_tokenize("   ") == []
