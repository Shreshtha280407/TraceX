"""Scenario 9: rule-based event/relation observations are source-supported and traceable."""

from __future__ import annotations

from app.contracts.common import SourceLocator
from app.modules.structured_processing.document.relations import (
    PROXIMITY_MAX_CHARS,
    extract_relations,
)
from app.modules.structured_processing.models import RawMention


def _mention(
    observation_type: str, text: str, *, page: int, start: int, end: int, hint: str
) -> RawMention:
    return RawMention(
        observation_type=observation_type,
        text=text,
        locator=SourceLocator(page=page, span_start=start, span_end=end),
        confidence=0.9,
        entity_type_hint=hint,
    )


def test_person_near_a_phone_number_produces_a_traceable_association() -> None:
    mentions = [
        _mention("person_mention", "Ramesh Kumar", page=1, start=0, end=12, hint="person"),
        _mention(
            "phone_number_mention", "9876543210", page=1, start=20, end=30, hint="phone_number"
        ),
    ]
    relations = extract_relations(mentions)

    assert len(relations) == 1
    relation = relations[0]
    assert relation.observation_type == "person_contact_association"
    assert relation.attributes["rule"] == "person_contact_proximity_v1"
    assert relation.locator.page == 1
    assert relation.locator.span_start == 0
    assert relation.locator.span_end == 30


def test_person_far_from_a_phone_number_produces_no_association() -> None:
    far_start = PROXIMITY_MAX_CHARS + 1000
    mentions = [
        _mention("person_mention", "Ramesh Kumar", page=1, start=0, end=12, hint="person"),
        _mention(
            "phone_number_mention",
            "9876543210",
            page=1,
            start=far_start,
            end=far_start + 10,
            hint="phone_number",
        ),
    ]
    assert extract_relations(mentions) == []


def test_relation_on_a_different_page_never_matches() -> None:
    mentions = [
        _mention("person_mention", "Ramesh Kumar", page=1, start=0, end=12, hint="person"),
        _mention(
            "phone_number_mention", "9876543210", page=2, start=0, end=10, hint="phone_number"
        ),
    ]
    assert extract_relations(mentions) == []


def test_incident_event_requires_a_fir_marker_a_date_and_a_participant_or_location() -> None:
    mentions = [
        _mention("fir_reference", "42/2026", page=1, start=0, end=7, hint="fir_number"),
        _mention("date_time_mention", "05/01/2026", page=1, start=10, end=20, hint="date_time"),
        _mention("location_mention", "Mumbai", page=1, start=30, end=36, hint="location"),
    ]
    relations = extract_relations(mentions)
    incidents = [r for r in relations if r.observation_type == "incident_event_mention"]
    assert len(incidents) == 1
    assert incidents[0].attributes["markers"] == ["42/2026"]
    assert incidents[0].attributes["locations"] == ["Mumbai"]


def test_incident_event_does_not_fire_without_a_fir_marker() -> None:
    mentions = [
        _mention("date_time_mention", "05/01/2026", page=1, start=10, end=20, hint="date_time"),
        _mention("location_mention", "Mumbai", page=1, start=30, end=36, hint="location"),
    ]
    relations = extract_relations(mentions)
    assert not any(r.observation_type == "incident_event_mention" for r in relations)


def test_every_relation_cites_a_span_within_the_source_page() -> None:
    mentions = [
        _mention("amount_mention", "50000", page=3, start=100, end=105, hint="amount"),
        _mention(
            "financial_identifier_mention",
            "UTR123456",
            page=3,
            start=108,
            end=117,
            hint="transaction_reference",
        ),
    ]
    relations = extract_relations(mentions)
    assert len(relations) == 1
    assert relations[0].locator.page == 3
    assert relations[0].locator.span_start is not None
    assert relations[0].locator.span_end is not None
