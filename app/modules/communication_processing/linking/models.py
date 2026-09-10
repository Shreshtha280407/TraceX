"""Review-only communication-link candidate models.

A `CommunicationLinkCandidate` is never a graph relationship, verified
identity, criminal association, fact, or hypothesis — it is a proposal
for a human reviewer to evaluate, always in `status=PROPOSED_FOR_REVIEW`
in this phase. See `docs/decisions/ADR-005-provenance-first-communication-
processing.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class LinkType(StrEnum):
    """Every allowed deterministic reason two observations might be linked."""

    SAME_SOURCE_CONVERSATION = "same_source_conversation"
    EXPLICIT_REPLY_REFERENCE = "explicit_reply_reference"
    EXPLICIT_MESSAGE_REFERENCE = "explicit_message_reference"
    SAME_NORMALIZED_PHONE_TOKEN = "same_normalized_phone_token"
    SAME_EXACT_HANDLE_TOKEN = "same_exact_handle_token"


class LinkStatus(StrEnum):
    """Every candidate starts — and, in this phase, stays — in review."""

    PROPOSED_FOR_REVIEW = "proposed_for_review"


@dataclass(frozen=True)
class LinkReason:
    """A documented, human-readable explanation for why a candidate was proposed."""

    code: str
    description: str


@dataclass(frozen=True)
class LinkEvidence:
    """One piece of evidence backing a link candidate: a reference to a real observation."""

    observation_id: UUID
    detail: str


@dataclass(frozen=True)
class CommunicationLinkCandidate:
    """A deterministic, review-only proposal that two observations may be related.

    Not a graph relationship, verified identity, criminal association,
    fact, or hypothesis, and never built from a probabilistic score.
    `status` is always `PROPOSED_FOR_REVIEW` — no automatic acceptance or
    rejection exists in this phase.
    """

    case_id: UUID
    candidate_id: UUID
    left_observation_id: UUID
    right_observation_id: UUID
    link_type: LinkType
    reason_code: str
    evidence_refs: tuple[LinkEvidence, ...]
    status: LinkStatus
    created_at: datetime


@dataclass(frozen=True)
class LinkableMessageDescriptor:
    """The minimal, non-sensitive info needed to evaluate link candidates for one message.

    Deliberately *not* a full `ObservationV1`/`ChatMessageRecord` — linking
    logic only ever sees these specific fields, keeping it decoupled from
    the parsing layer and easy to reason about and test in isolation.
    Display names/aliases are deliberately not fields here at all: this
    keeps it structurally impossible for this module's link-finding logic
    to build a candidate from name/transliteration similarity.
    """

    observation_id: UUID
    case_id: UUID
    evidence_id: UUID
    conversation_id: str | None = None
    message_id: str | None = None
    reply_to_message_id: str | None = None
    referenced_message_id: str | None = None
    normalized_phone_token: str | None = None
    exact_handle_token: str | None = None
