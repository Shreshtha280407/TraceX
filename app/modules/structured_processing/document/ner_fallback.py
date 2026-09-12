"""Deterministic, rule-based `NerAdapter` — no ML model, always available.

This is the adapter every unit test runs against by default, and the
automatic runtime fallback `worker.py` selects when the real
`ner_spacy.SpacyNerAdapter` model asset hasn't been bootstrapped (see
`docs/architecture/document-structured-processing.md`'s "Local NER
bootstrap" for the graceful-degradation policy — the exact same posture
`media_processing.worker._build_analysis_components` already established
for a missing detector/OCR model, applied here to NER).

Three narrow, documented heuristics, each intentionally conservative
(false negatives over false positives — a missed mention is silently
absent, a wrong one is an incorrect investigative claim):

- `PERSON`: two or three consecutive Title-Case words, excluding a fixed
  stopword list (months, weekdays, common report/organization vocabulary)
  and excluding anything already recognized as a `LOCATION` or
  `ORGANIZATION` by the rules below.
- `ORGANIZATION`: one or more Title-Case words immediately followed by a
  documented organizational suffix (`Bank`, `Ltd`, `Pvt`, `Inc`,
  `Corporation`, `Company`, `Enterprises`, `Industries`, `Traders`,
  `Associates`, `Police Station`).
- `LOCATION`: an exact (case-sensitive) match against a small, fixed
  gazetteer of common Indian city/state names.

None of this is a trained statistical model — it is a fixed, versioned
rule set, exactly as narrow and exactly as honestly documented as
`document/fir_report.py`'s own regex patterns (see that module's
docstring for the identical "no NLP, no fuzzy matching" framing). See
`docs/qa/known-limitations.md` for this gazetteer/rule set's known,
intentional narrowness.
"""

from __future__ import annotations

import re

from app.modules.structured_processing.document.ner import (
    CONFIDENCE_NER_FALLBACK,
    NerMention,
)

NER_FALLBACK_ADAPTER_NAME = "deterministic_ner_fallback_v1"
NER_FALLBACK_ADAPTER_VERSION = "1.0.0"

#: Common capitalized words that are never themselves a person's name —
#: kept short and fixed, not exhaustive (see module docstring).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "The",
        "This",
        "That",
        "These",
        "Those",
        "Police",
        "Station",
        "Report",
        "First",
        "Information",
        "Section",
        "Bank",
        "Limited",
        "Private",
        "Company",
        "Reference",
        "Transaction",
        "Account",
        "Amount",
        "Date",
        "Time",
        "State",
        "Government",
        "District",
        "City",
        "Sir",
        "Madam",
    }
)

_ORG_SUFFIXES = (
    "Bank",
    "Ltd",
    "Ltd.",
    "Pvt",
    "Pvt.",
    "Inc",
    "Inc.",
    "Corporation",
    "Company",
    "Enterprises",
    "Industries",
    "Traders",
    "Associates",
    "Police Station",
)

#: A small, fixed set of common Indian city/state names — intentionally
#: not exhaustive; see module docstring and known-limitations.md.
_LOCATION_GAZETTEER: frozenset[str] = frozenset(
    {
        "Mumbai",
        "Delhi",
        "New Delhi",
        "Bangalore",
        "Bengaluru",
        "Chennai",
        "Kolkata",
        "Hyderabad",
        "Pune",
        "Ahmedabad",
        "Jaipur",
        "Lucknow",
        "Kanpur",
        "Nagpur",
        "Indore",
        "Bhopal",
        "Patna",
        "Surat",
        "Chandigarh",
        "Gurgaon",
        "Gurugram",
        "Noida",
        "Maharashtra",
        "Karnataka",
        "Tamil Nadu",
        "Uttar Pradesh",
        "Gujarat",
        "Rajasthan",
        "West Bengal",
        "Punjab",
        "Haryana",
        "Kerala",
        "Telangana",
        "Bihar",
        "Odisha",
        "Assam",
        "Goa",
    }
)

_TITLE_WORD = r"[A-Z][a-z]+"
#: Organization name components may also be a short all-caps acronym
#: (`HDFC`, `ICICI`, `SBI`) — real Indian bank/company names commonly lead
#: with one. Deliberately not extended to the `PERSON` pattern below: an
#: all-caps word is far more likely to be an acronym/abbreviation than part
#: of a person's name in this fixture corpus.
_ORG_WORD = r"(?:[A-Z][a-z]+|[A-Z]{2,6})"
_ORG_SUFFIX_PATTERN = "|".join(re.escape(suffix) for suffix in _ORG_SUFFIXES)
_ORGANIZATION_RE = re.compile(
    rf"\b((?:{_ORG_WORD}\s+){{0,3}}{_ORG_WORD}\s+(?:{_ORG_SUFFIX_PATTERN}))\b"
)
_LOCATION_RE = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in sorted(_LOCATION_GAZETTEER, key=len, reverse=True))
    + r")\b"
)
_PERSON_RE = re.compile(rf"\b({_TITLE_WORD}(?:\s+{_TITLE_WORD}){{1,2}})\b")


class DeterministicNerAdapter:
    """See module docstring. Stateless, safe to reuse across calls."""

    def extract(self, text: str) -> list[NerMention]:
        mentions: list[NerMention] = []
        claimed: list[tuple[int, int]] = []

        for match in _ORGANIZATION_RE.finditer(text):
            mentions.append(self._mention(match, "ORGANIZATION"))
            claimed.append(match.span())

        for match in _LOCATION_RE.finditer(text):
            span = match.span()
            if _overlaps(span, claimed):
                continue
            mentions.append(self._mention(match, "LOCATION"))
            claimed.append(span)

        for match in _PERSON_RE.finditer(text):
            span = match.span()
            if _overlaps(span, claimed):
                continue
            candidate = match.group(1)
            words = candidate.split()
            if any(word in _STOPWORDS for word in words):
                continue
            mentions.append(self._mention(match, "PERSON"))
            claimed.append(span)

        return sorted(mentions, key=lambda m: m.start)

    def _mention(self, match: re.Match[str], label: str) -> NerMention:
        return NerMention(
            text=match.group(1),
            label=label,
            start=match.start(1),
            end=match.end(1),
            confidence=CONFIDENCE_NER_FALLBACK,
            adapter_name=NER_FALLBACK_ADAPTER_NAME,
            adapter_version=NER_FALLBACK_ADAPTER_VERSION,
        )


def _overlaps(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(other_start < end and start < other_end for other_start, other_end in others)


__all__ = [
    "NER_FALLBACK_ADAPTER_NAME",
    "NER_FALLBACK_ADAPTER_VERSION",
    "DeterministicNerAdapter",
]
