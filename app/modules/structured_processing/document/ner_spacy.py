"""Real local NER: spaCy's small English pipeline, loaded from a bootstrapped directory.

`spacy` (the library) is an ordinary pip dependency, exactly like
`onnxruntime`/`pytesseract` — but the *model data* (`en_core_web_sm`, a
~13 MB pretrained pipeline) is a large, licensed, third-party asset this
repository never commits or downloads automatically. An operator runs

    uv run python -m app.modules.structured_processing.bootstrap_ner_model

once, exactly the same "explicit trusted-operator action" posture
`media_processing.bootstrap_models` already established for the real
object-detection model — see that module's docstring for the identical
reasoning. `SpacyNerAdapter.__init__` loads directly from the bootstrapped
local directory path (`spacy.load(Path(...))`), never by package name —
this repository never runs `pip install`/`spacy download` at any point, in
tests, in the worker, or in this bootstrap script itself (see
`bootstrap_ner_model.py`'s own docstring for why a plain download-and-
verify-and-extract step is sufficient and no package installation is ever
needed).

## Model provenance

- Package: `en_core_web_sm` (spaCy's small English pipeline: tagger,
  parser, lemmatizer, attribute ruler, and the NER component this adapter
  actually uses).
- Version: `3.8.0` (pinned, not "latest").
- Source: `github.com/explosion/spacy-models`, release tag
  `en_core_web_sm-3.8.0`, asset `en_core_web_sm-3.8.0-py3-none-any.whl`.
- License: MIT.
- SHA-256 of the wheel: see `Settings.ner_model_sha256` /
  `bootstrap_ner_model.DEFAULT_NER_MODEL_SHA256`.

## Label mapping

spaCy's `en_core_web_sm` NER component emits its own label vocabulary
(`PERSON`, `ORG`, `GPE`, `LOC`, `DATE`, `MONEY`, ...). Only the three
entries in `_SPACY_LABEL_MAP` below are ever translated into this
project's `SUPPORTED_NER_LABELS`; every other spaCy label (including
`DATE` — see `ner.py`'s module docstring for why dates are deliberately
regex-only) is dropped, not passed through under an unexpected name.

## Honest accuracy note

This is a small, general-purpose English model, not fine-tuned for Indian
names, places, or investigative-document vocabulary — it will miss some
real mentions and occasionally mislabel others (e.g. an unfamiliar Indian
given name might be tagged `PRODUCT` or missed entirely). `extraction_
confidence` reflects this: see `ner.py`'s `CONFIDENCE_NER_MODEL` and its
surrounding note on why no native per-entity score is available.
"""

from __future__ import annotations

from pathlib import Path

from app.modules.structured_processing.document.ner import CONFIDENCE_NER_MODEL, NerMention
from app.modules.structured_processing.errors import ErrorCode, ProcessingError

NER_MODEL_ADAPTER_NAME = "spacy_ner_en_core_web_sm_v1"
NER_MODEL_VERSION = "3.8.0"

_SPACY_LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORGANIZATION",
    "GPE": "LOCATION",  # geo-political entity (country/city/state)
    "LOC": "LOCATION",  # non-GPE location (e.g. a named region/water body)
}


class SpacyNerAdapter:
    """Real, local spaCy-backed `NerAdapter`. See module docstring.

    Raises `ProcessingError(NER_RUNTIME_UNAVAILABLE)` at construction if
    `model_path` doesn't contain a loadable spaCy pipeline — never falls
    back to downloading one. `worker.py` catches this and degrades to
    `ner_fallback.DeterministicNerAdapter` instead of failing the job (see
    that module's docstring).
    """

    def __init__(self, model_path: Path) -> None:
        try:
            import spacy
        except ImportError as exc:  # pragma: no cover - spacy is a pinned runtime dependency
            raise ProcessingError(
                ErrorCode.NER_RUNTIME_UNAVAILABLE, "the spacy package is not installed"
            ) from exc

        if not model_path.is_dir():
            raise ProcessingError(
                ErrorCode.NER_RUNTIME_UNAVAILABLE,
                f"no local NER model directory at '{model_path}' -- run "
                "'uv run python -m app.modules.structured_processing.bootstrap_ner_model' first",
            )
        try:
            self._nlp = spacy.load(model_path)
        except (OSError, ValueError) as exc:
            raise ProcessingError(
                ErrorCode.NER_RUNTIME_UNAVAILABLE,
                f"the local NER model at '{model_path}' could not be loaded",
            ) from exc

    def extract(self, text: str) -> list[NerMention]:
        doc = self._nlp(text)
        mentions: list[NerMention] = []
        for ent in doc.ents:
            label = _SPACY_LABEL_MAP.get(ent.label_)
            if label is None:
                continue
            mentions.append(
                NerMention(
                    text=ent.text,
                    label=label,
                    start=ent.start_char,
                    end=ent.end_char,
                    confidence=CONFIDENCE_NER_MODEL,
                    adapter_name=NER_MODEL_ADAPTER_NAME,
                    adapter_version=NER_MODEL_VERSION,
                    attributes={"spacy_label": ent.label_},
                )
            )
        return mentions


__all__ = ["NER_MODEL_ADAPTER_NAME", "NER_MODEL_VERSION", "SpacyNerAdapter"]
