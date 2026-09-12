"""`social/common.py`: `chat_message_to_mention`'s sender-transliteration wiring."""

from __future__ import annotations

from app.contracts.common import SourceLocator
from app.modules.communication_processing.social.common import (
    ChatMessageRecord,
    chat_message_to_mention,
)

_LOCATOR = SourceLocator(message_id="m1")


def _record(sender: str | None) -> ChatMessageRecord:
    return ChatMessageRecord(
        platform="whatsapp",
        conversation_id="c1",
        message_id="m1",
        sender=sender,
        participants=(),
        timestamp_raw=None,
        timestamp_utc=None,
        timestamp_source_timezone=None,
        text="hello",
        reply_to=None,
        locator=_LOCATOR,
    )


def test_no_sender_means_no_candidates() -> None:
    mention = chat_message_to_mention(_record(None))
    assert mention.attributes["sender_transliteration_candidates"] is None


def test_latin_sender_gets_exact_normalized_candidate() -> None:
    mention = chat_message_to_mention(_record("Alice"))
    candidates = mention.attributes["sender_transliteration_candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    assert candidates[0]["original_text"] == "Alice"
    assert candidates[0]["script"] == "latin"
    assert candidates[0]["category"] == "exact_normalized"


def test_multiword_devanagari_sender_produces_one_candidate_per_word() -> None:
    """Scenario 24/26: a multi-word name is tokenized, never treated as one
    unmappable blob, and never merged into a single synthesized "full name"."""
    mention = chat_message_to_mention(_record("राहुल शर्मा"))
    candidates = mention.attributes["sender_transliteration_candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 2
    assert candidates[0]["original_text"] == "राहुल"
    assert candidates[0]["category"] == "deterministic_transliteration"
    assert "raahula" in candidates[0]["candidates"]
    assert candidates[1]["original_text"] == "शर्मा"


def test_transliteration_never_marks_candidates_as_identity() -> None:
    """Section 7.2: candidates are review-only, never an interchangeable identity."""
    mention = chat_message_to_mention(_record("राहुल"))
    candidates = mention.attributes["sender_transliteration_candidates"]
    assert candidates[0]["method"] == "communication_processing.aliases.transliteration"
    assert candidates[0]["method_version"]
    # The original script text is always retained alongside any candidate.
    assert candidates[0]["original_text"] == "राहुल"


def test_sender_attribute_and_candidates_are_bounded_for_oversized_input() -> None:
    """A pathologically long sender field (e.g. an unbounded JSON string from a
    malformed export) must not fail the whole message -- it's truncated instead."""
    huge_sender = "a" * 10_000
    mention = chat_message_to_mention(_record(huge_sender))
    assert len(mention.attributes["sender"]) <= 300
    assert mention.attributes["sender_transliteration_candidates"] is not None
