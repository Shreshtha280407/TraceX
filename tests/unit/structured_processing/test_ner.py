"""Scenarios 7-8: local NER emits supported labels with exact provenance;
NER fallback/missing local model fails safely or uses a documented
deterministic adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.structured_processing.document.ner import SUPPORTED_NER_LABELS
from app.modules.structured_processing.document.ner_fallback import DeterministicNerAdapter
from app.modules.structured_processing.errors import ErrorCode, ProcessingError


def test_fallback_adapter_finds_person_organization_and_location() -> None:
    adapter = DeterministicNerAdapter()
    text = "Ramesh Kumar met Suresh Sharma at HDFC Bank in Mumbai yesterday."
    mentions = adapter.extract(text)

    labels_found = {m.label for m in mentions}
    assert labels_found == {"PERSON", "ORGANIZATION", "LOCATION"}
    assert labels_found <= SUPPORTED_NER_LABELS

    for mention in mentions:
        assert text[mention.start : mention.end] == mention.text


def test_fallback_adapter_rejects_stopword_capitalized_sequences() -> None:
    adapter = DeterministicNerAdapter()
    text = "The Police Station filed the First Information Report on Monday."
    mentions = adapter.extract(text)
    assert not any(m.label == "PERSON" for m in mentions)


def test_fallback_adapter_is_deterministic() -> None:
    adapter = DeterministicNerAdapter()
    text = "Ramesh Kumar visited Chennai to meet ICICI Bank officials."
    first = adapter.extract(text)
    second = adapter.extract(text)
    assert first == second


def test_fallback_adapter_never_raises_on_empty_or_plain_text() -> None:
    adapter = DeterministicNerAdapter()
    assert adapter.extract("") == []
    assert adapter.extract("no capitalized words here at all") == []


def test_spacy_adapter_raises_a_safe_error_when_the_model_directory_is_missing(
    tmp_path: Path,
) -> None:
    from app.modules.structured_processing.document.ner_spacy import SpacyNerAdapter

    missing = tmp_path / "does-not-exist"
    with pytest.raises(ProcessingError) as exc_info:
        SpacyNerAdapter(missing)
    assert exc_info.value.code == ErrorCode.NER_RUNTIME_UNAVAILABLE
    # The configured local path is safe to report (a bootstrap destination,
    # never a secret); no raw library/OS exception text is ever appended.
    assert "Traceback" not in str(exc_info.value)


def test_spacy_adapter_loads_real_bootstrapped_model_and_extracts_entities() -> None:
    """Self-skips (never fabricates a pass) if the model asset hasn't been bootstrapped."""
    from app.core.config import get_settings
    from app.modules.structured_processing.document.ner_spacy import SpacyNerAdapter

    model_path = get_settings().ner_model_path
    if not model_path.is_dir():
        pytest.skip(
            "NER model asset not bootstrapped -- run "
            "'uv run python -m app.modules.structured_processing.bootstrap_ner_model'"
        )

    adapter = SpacyNerAdapter(model_path)
    text = "Officials at HDFC Bank confirmed the transfer took place in Mumbai."
    mentions = adapter.extract(text)

    assert any(m.label == "ORGANIZATION" for m in mentions)
    assert any(m.label == "LOCATION" for m in mentions)
    for mention in mentions:
        assert mention.label in SUPPORTED_NER_LABELS
        assert text[mention.start : mention.end] == mention.text
