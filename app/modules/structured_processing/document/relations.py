"""Transparent, deterministic rule-based relation/event extraction over document text.

Every rule here looks for two or more *already-extracted* mentions (regex
mentions from `fir_report.py`, or NER mentions from `ner_fallback.py`/
`ner_spacy.py`) that are close together on the same page, and emits one new
`RawMention` describing that co-occurrence — never a new fact invented from
nothing, and never a conclusion stronger than "these things appear near
each other in the source text." Rules are named, versioned, and each cites
the exact combined source span its constituent mentions came from.

This is explicitly **not** identity resolution, entity merging, or a graph
relationship: every observation produced here is exactly as reviewable and
exactly as provisional as any other `ObservationV1` — a later phase's
entity resolution and graph projection may or may not choose to act on it.
None of these rules ever asserts guilt, criminal-network membership, or any
certainty beyond "these source-extracted facts co-occur in this document"
(see `CLAUDE.md`'s "No automatic identity merge, guilt conclusion" rule).

## Rules

- `person_contact_proximity_v1`: a `PERSON` NER mention near a
  `phone_number_mention`/`email_address_mention` regex mention ->
  `person_contact_association`.
- `dated_communication_reference_v1`: a `date_time_mention` near a
  `phone_number_mention`/`email_address_mention` -> `dated_communication_reference`.
- `transaction_claim_v1`: an `amount_mention` near a
  `financial_identifier_mention` -> `transaction_claim`.
- `incident_event_v1`: on a page carrying a `fir_reference` or
  `police_station_mention`, together with at least one `date_time_mention`
  and at least one `PERSON`/`LOCATION` mention -> one `incident_event_mention`
  per page, listing exactly what was found (never inferring anything not
  literally present).

"Near" means within `PROXIMITY_MAX_CHARS` characters of each other in the
same page's normalized text — a fixed, documented threshold, not a learned
or tunable-per-request one.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.core.canonical import canonical_sha256
from app.modules.structured_processing.models import RawMention

RELATIONS_RULESET_NAME = "document_relation_rules_v1"
RELATIONS_RULESET_VERSION = "1.0.0"

#: A relation/event observation is an *inference from proximity*, weaker
#: evidence than either an exact regex match or a direct NER mention on its
#: own — see module docstring.
CONFIDENCE_RELATION_INFERRED = 0.60

PROXIMITY_MAX_CHARS = 200

_CONTACT_OBSERVATION_TYPES = frozenset({"phone_number_mention", "email_address_mention"})
_FINANCIAL_IDENTIFIER_TYPE = "financial_identifier_mention"
_AMOUNT_TYPE = "amount_mention"
_DATE_TYPE = "date_time_mention"
_FIR_TYPES = frozenset({"fir_reference", "police_station_mention"})


def relations_config_hash() -> str:
    return canonical_sha256(
        {
            "ruleset": RELATIONS_RULESET_NAME,
            "version": RELATIONS_RULESET_VERSION,
            "proximity_max_chars": PROXIMITY_MAX_CHARS,
        }
    )


@dataclass(frozen=True)
class _PageMention:
    mention: RawMention
    page: int | None
    start: int
    end: int


def extract_relations(mentions: list[RawMention]) -> list[RawMention]:
    """Apply every rule to `mentions`, grouped by page, returning new derived mentions.

    `page=None` (DOCX/TXT — formats with no page concept, see `models.
    TextSegment`) is a valid group of its own: every such mention shares
    one single-segment "page", so proximity/incident rules still apply
    across the whole document, not skipped outright.
    """
    by_page: dict[int | None, list[_PageMention]] = {}
    for mention in mentions:
        page = mention.locator.page
        start = mention.locator.span_start
        end = mention.locator.span_end
        if start is None or end is None:
            continue
        by_page.setdefault(page, []).append(_PageMention(mention, page, start, end))

    derived: list[RawMention] = []
    for page, page_mentions in by_page.items():
        derived.extend(_person_contact_proximity(page_mentions))
        derived.extend(_dated_communication_reference(page_mentions))
        derived.extend(_transaction_claim(page_mentions))
        derived.extend(_incident_event(page, page_mentions))
    return derived


def _near(a: _PageMention, b: _PageMention) -> bool:
    gap = max(a.start, b.start) - min(a.end, b.end)
    return gap <= PROXIMITY_MAX_CHARS


def _combined_locator(a: _PageMention, b: _PageMention) -> SourceLocator:
    return SourceLocator(page=a.page, span_start=min(a.start, b.start), span_end=max(a.end, b.end))


def _person_mentions(page_mentions: list[_PageMention]) -> list[_PageMention]:
    return [pm for pm in page_mentions if pm.mention.entity_type_hint == "person"]


def _person_contact_proximity(page_mentions: list[_PageMention]) -> list[RawMention]:
    persons = _person_mentions(page_mentions)
    contacts = [
        pm for pm in page_mentions if pm.mention.observation_type in _CONTACT_OBSERVATION_TYPES
    ]
    results: list[RawMention] = []
    for person in persons:
        for contact in contacts:
            if not _near(person, contact):
                continue
            results.append(
                RawMention(
                    observation_type="person_contact_association",
                    text=f"{person.mention.text} / {contact.mention.text}",
                    locator=_combined_locator(person, contact),
                    confidence=CONFIDENCE_RELATION_INFERRED,
                    attributes={
                        "rule": "person_contact_proximity_v1",
                        "person": person.mention.text,
                        "contact_type": contact.mention.observation_type,
                        "contact_value": contact.mention.text,
                    },
                )
            )
    return results


def _dated_communication_reference(page_mentions: list[_PageMention]) -> list[RawMention]:
    dates = [pm for pm in page_mentions if pm.mention.observation_type == _DATE_TYPE]
    contacts = [
        pm for pm in page_mentions if pm.mention.observation_type in _CONTACT_OBSERVATION_TYPES
    ]
    results: list[RawMention] = []
    for date in dates:
        for contact in contacts:
            if not _near(date, contact):
                continue
            results.append(
                RawMention(
                    observation_type="dated_communication_reference",
                    text=f"{date.mention.text} / {contact.mention.text}",
                    locator=_combined_locator(date, contact),
                    confidence=CONFIDENCE_RELATION_INFERRED,
                    attributes={
                        "rule": "dated_communication_reference_v1",
                        "date_time": date.mention.text,
                        "contact_type": contact.mention.observation_type,
                        "contact_value": contact.mention.text,
                    },
                )
            )
    return results


def _transaction_claim(page_mentions: list[_PageMention]) -> list[RawMention]:
    amounts = [pm for pm in page_mentions if pm.mention.observation_type == _AMOUNT_TYPE]
    identifiers = [
        pm for pm in page_mentions if pm.mention.observation_type == _FINANCIAL_IDENTIFIER_TYPE
    ]
    results: list[RawMention] = []
    for amount in amounts:
        for identifier in identifiers:
            if not _near(amount, identifier):
                continue
            results.append(
                RawMention(
                    observation_type="transaction_claim",
                    text=f"{amount.mention.text} / {identifier.mention.text}",
                    locator=_combined_locator(amount, identifier),
                    confidence=CONFIDENCE_RELATION_INFERRED,
                    attributes={
                        "rule": "transaction_claim_v1",
                        "amount": amount.mention.text,
                        "identifier_kind": identifier.mention.attributes.get(
                            "identifier_kind", identifier.mention.entity_type_hint
                        ),
                        "identifier_value": identifier.mention.text,
                    },
                )
            )
    return results


def _incident_event(page: int | None, page_mentions: list[_PageMention]) -> list[RawMention]:
    fir_markers = [pm for pm in page_mentions if pm.mention.observation_type in _FIR_TYPES]
    if not fir_markers:
        return []
    dates = [pm for pm in page_mentions if pm.mention.observation_type == _DATE_TYPE]
    locations = [
        pm for pm in page_mentions if pm.mention.entity_type_hint in ("location", "police_station")
    ]
    participants = _person_mentions(page_mentions)
    if not dates or not (locations or participants):
        return []

    all_spans = fir_markers + dates + locations + participants
    span_start = min(pm.start for pm in all_spans)
    span_end = max(pm.end for pm in all_spans)
    attributes: dict[str, JsonValue] = {
        "rule": "incident_event_v1",
        "markers": [pm.mention.text for pm in fir_markers],
        "dates": [pm.mention.text for pm in dates],
        "locations": [pm.mention.text for pm in locations],
        "participants": [pm.mention.text for pm in participants],
    }
    return [
        RawMention(
            observation_type="incident_event_mention",
            text=f"incident_event_mention page {page}",
            locator=SourceLocator(page=page, span_start=span_start, span_end=span_end),
            confidence=CONFIDENCE_RELATION_INFERRED,
            attributes=attributes,
        )
    ]


__all__ = [
    "CONFIDENCE_RELATION_INFERRED",
    "PROXIMITY_MAX_CHARS",
    "RELATIONS_RULESET_NAME",
    "RELATIONS_RULESET_VERSION",
    "extract_relations",
    "relations_config_hash",
]
