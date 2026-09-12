"""Local named-entity recognition: `NerAdapter` protocol + supported labels.

Two implementations exist behind this one interface — see their own module
docstrings for the reasoning behind each:

- `ner_fallback.DeterministicNerAdapter`: a fixed, rule-based/gazetteer
  adapter with no ML model at all. Always available, fully deterministic,
  used by every unit test and as the automatic runtime fallback when the
  real model asset below hasn't been bootstrapped.
- `ner_spacy.SpacyNerAdapter`: a real local NER model (spaCy's small
  English pipeline), loaded only from an operator-bootstrapped local model
  directory (`bootstrap_ner_model.py`) — never downloaded automatically at
  worker runtime.

Neither adapter ever converts a mention into an `EntityV1` or performs
identity merging — `NerMention`s become `ExtractedEntityMention`s inside an
`ObservationV1` (see `worker.py`), exactly like a regex mention already
does; entity resolution remains explicit, later-phase, human-reviewable
work (see `CLAUDE.md`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

#: The only entity type labels either adapter may emit. A model output
#: outside this set is dropped, not silently relabeled or passed through —
#: see each adapter's own label-mapping table for the exact translation
#: from its native label vocabulary to this one. `DATE` is deliberately
#: excluded: `document/fir_report.py`'s existing `date_time_mention` regex
#: already covers dates deterministically and exactly (an explicit
#: DD/MM/YYYY or ISO format match) — adding a second, statistical/heuristic
#: date extractor here would only produce redundant, lower-confidence
#: observations for the same fact.
SUPPORTED_NER_LABELS: frozenset[str] = frozenset({"PERSON", "ORGANIZATION", "LOCATION"})

# Deterministic confidence tiers for NER mentions. Distinct from
# `provenance.CONFIDENCE_REGEX_EXACT_MATCH`'s meaning on purpose (see
# module docstring in provenance.py and this module's own docstring): a
# regex match is an exact deterministic pattern match, while every NER
# mention here — real model or deterministic fallback — is a *statistical
# or heuristic* judgment about a span of text, always weaker evidence than
# an explicit label/structural match, and always extraction-quality only,
# never identity certainty.
CONFIDENCE_NER_FALLBACK = 0.50  # the deterministic gazetteer/heuristic adapter
CONFIDENCE_NER_MODEL = 0.75  # the real local spaCy model

# `en_core_web_sm`'s NER component does not expose a native per-entity
# confidence score through spaCy's standard pipeline output (that would
# require a different model architecture/extension) — these two constants
# are fixed extraction-quality tiers assigned per adapter, not a per-
# instance model probability. Documented explicitly rather than fabricating
# false per-entity precision -- see docs/architecture/document-structured-
# processing.md.


@dataclass(frozen=True)
class NerMention:
    """One named-entity mention found in a (normalized) text segment.

    `start`/`end` are character offsets into the exact text string passed
    to `NerAdapter.extract` — the caller is responsible for mapping them
    back through an `OffsetMap` to the original source span before
    building a `SourceLocator`, exactly like a regex match's span.
    """

    text: str
    label: str
    start: int
    end: int
    confidence: float
    adapter_name: str
    adapter_version: str
    attributes: dict[str, JsonValue] = field(default_factory=dict)


@runtime_checkable
class NerAdapter(Protocol):
    """A local named-entity recognizer over one text string.

    Implementations must never call a network API, never download a model
    at call time, and never raise for "no entities found" (an empty list
    is the normal, common outcome) — only for a genuine runtime failure.
    """

    def extract(self, text: str) -> list[NerMention]: ...


__all__ = [
    "CONFIDENCE_NER_FALLBACK",
    "CONFIDENCE_NER_MODEL",
    "SUPPORTED_NER_LABELS",
    "NerAdapter",
    "NerMention",
]
