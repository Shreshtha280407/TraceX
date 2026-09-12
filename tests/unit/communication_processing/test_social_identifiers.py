"""`social/identifiers.py`: deterministic phone/email/url/handle extraction from chat text."""

from __future__ import annotations

from app.contracts.common import SourceLocator
from app.modules.communication_processing.social.common import ChatMessageRecord
from app.modules.communication_processing.social.identifiers import (
    extract_mentioned_identifiers,
)

_LOCATOR = SourceLocator(message_id="m1")


def _record(text: str | None) -> ChatMessageRecord:
    return ChatMessageRecord(
        platform="whatsapp",
        conversation_id="c1",
        message_id="m1",
        sender="Alice",
        participants=(),
        timestamp_raw=None,
        timestamp_utc=None,
        timestamp_source_timezone=None,
        text=text,
        reply_to=None,
        locator=_LOCATOR,
    )


def test_extracts_phone_number() -> None:
    mentions = extract_mentioned_identifiers(_record("call me at 9876543210 today"))
    phone = [m for m in mentions if m.observation_type == "phone_number"]
    assert len(phone) == 1
    assert phone[0].text == "9876543210"
    assert phone[0].locator == _LOCATOR


def test_extracts_email_address() -> None:
    mentions = extract_mentioned_identifiers(_record("reach alice@example.com anytime"))
    emails = [m for m in mentions if m.observation_type == "email_address"]
    assert len(emails) == 1
    assert emails[0].text == "alice@example.com"


def test_extracts_url_and_trims_trailing_punctuation() -> None:
    mentions = extract_mentioned_identifiers(_record("see http://example.com/x, thanks"))
    urls = [m for m in mentions if m.observation_type == "url"]
    assert len(urls) == 1
    assert urls[0].text == "http://example.com/x"


def test_extracts_www_url() -> None:
    mentions = extract_mentioned_identifiers(_record("visit www.example.com now"))
    urls = [m for m in mentions if m.observation_type == "url"]
    assert urls[0].text == "www.example.com"


def test_extracts_handle_but_not_email_local_part() -> None:
    mentions = extract_mentioned_identifiers(_record("cc @bob, ping alice@example.com"))
    handles = [m for m in mentions if m.observation_type == "username_or_handle"]
    assert [m.text for m in handles] == ["@bob"]


def test_no_text_returns_no_mentions() -> None:
    assert extract_mentioned_identifiers(_record(None)) == []


def test_no_matches_returns_empty_list() -> None:
    assert extract_mentioned_identifiers(_record("just plain text, nothing here")) == []


def test_multiple_identical_matches_share_the_parent_locator() -> None:
    """Scenario 28-adjacent: the message *is* the accurate locator; disambiguation
    of the resulting (observation_type, locator) collision happens in worker.py."""
    mentions = extract_mentioned_identifiers(_record("call 9876543210 or 9876543210 again"))
    phones = [m for m in mentions if m.observation_type == "phone_number"]
    assert len(phones) == 2
    assert phones[0].locator == phones[1].locator == _LOCATOR


def test_never_emits_graph_relationship_or_entity_types() -> None:
    """Hard boundary: only the four approved contract types, nothing resolved."""
    mentions = extract_mentioned_identifiers(
        _record("9876543210 alice@example.com http://example.com @bob")
    )
    assert {m.observation_type for m in mentions} == {
        "phone_number",
        "email_address",
        "url",
        "username_or_handle",
    }
    for mention in mentions:
        assert mention.entity_type_hint is None
