"""Phase 4 -> Phase 5 compatibility: `sourcing.py`'s adapter against realistic
canonical observation shapes from Jasraj (document/CDR/finance), Gaurav
(media), and Sarthak (communication) workers.

All fixtures here are synthetic, invented, and non-sensitive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.intelligence.retrieval import retrieve_candidates
from app.modules.graph.intelligence.sourcing import (
    build_motif_edges,
    descriptor_from_observation,
)

_EXTRACTOR = Extractor(name="fixture", version="1.0.0", config_hash="h", model_version="n/a")


def _observation(
    *,
    case_id=None,
    evidence_id=None,
    observation_type: str,
    attributes: dict[str, object] | None = None,
    extracted_entities: list[ExtractedEntityMention] | None = None,
    locator: SourceLocator | None = None,
    event_time: datetime | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id or uuid4(),
        evidence_id=evidence_id or uuid4(),
        observation_type=observation_type,
        extracted_entities=extracted_entities or [],
        event_time=event_time,
        attributes=attributes or {},
        extraction_confidence=0.9,
        source_locator=locator or SourceLocator(page=1, span_start=0, span_end=5),
        extractor=_EXTRACTOR,
        created_at=datetime.now(UTC),
    )


# --- Scenario 3: exact blocking normalization + deterministic collision -----


def test_fir_phone_mention_maps_to_the_phone_identifier_kind() -> None:
    observation = _observation(
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="9876543210", entity_type_hint="phone_number")
        ],
    )
    descriptor = descriptor_from_observation(observation)
    assert descriptor is not None
    assert descriptor.identifiers == {"phone": "9876543210"}


def test_two_fir_mentions_of_the_same_phone_collide_deterministically() -> None:
    """Same digits, cosmetically different punctuation/whitespace -- both strip
    to the identical normalized form (`retrieval.normalise_identifier`'s
    digit-strip rule), so they must always collide."""
    case_id = uuid4()
    first = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="9876543210", entity_type_hint="phone_number")
        ],
    )
    second = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="98765 43210", entity_type_hint="phone_number")
        ],
    )
    descriptors = [
        descriptor_from_observation(first),
        descriptor_from_observation(second),
    ]
    assert all(descriptor is not None for descriptor in descriptors)
    run_a = retrieve_candidates(descriptors)  # type: ignore[arg-type]
    run_b = retrieve_candidates(descriptors)  # type: ignore[arg-type]
    assert run_a == run_b  # deterministic
    assert len(run_a) == 1
    assert "exact_identifier" in run_a[0].reasons


def test_country_coded_and_bare_phone_digits_do_not_collide() -> None:
    """Documented, pre-existing limitation of `retrieval.normalise_identifier`
    (not introduced by this adapter): it strips non-digits and prepends "+"
    but never reconciles a country code, so a CDR's E.164 "+919876543210"
    and a bare-digit "9876543210" extracted elsewhere are NOT recognized as
    the same number today. This is a real gap, not a silent guess -- the
    two values are correctly treated as distinct rather than incorrectly
    merged. See docs/qa/known-limitations.md."""
    case_id = uuid4()
    e164 = _observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={"caller_number": "+919876543210", "timestamp": "2026-01-01T10:00:00+00:00"},
    )
    bare = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="9876543210", entity_type_hint="phone_number")
        ],
    )
    descriptors = [descriptor_from_observation(e164), descriptor_from_observation(bare)]
    candidates = retrieve_candidates(descriptors)  # type: ignore[arg-type]
    assert candidates == ()


def test_upi_and_account_number_mentions_share_the_account_identifier_kind() -> None:
    """`financial_identifier_mention` covers both `upi_id` and `account_number`
    hints -- both must map to the same `account` kind, per fir_report.py."""
    case_id = uuid4()
    upi = _observation(
        case_id=case_id,
        observation_type="financial_identifier_mention",
        extracted_entities=[ExtractedEntityMention(text="alice@upi", entity_type_hint="upi_id")],
    )
    account = _observation(
        case_id=case_id,
        observation_type="financial_identifier_mention",
        extracted_entities=[
            ExtractedEntityMention(text="alice@upi", entity_type_hint="account_number")
        ],
    )
    descriptors = [descriptor_from_observation(upi), descriptor_from_observation(account)]
    candidates = retrieve_candidates(descriptors)  # type: ignore[arg-type]
    assert len(candidates) == 1
    assert "exact_identifier" in candidates[0].reasons


def test_transaction_reference_and_cell_tower_are_never_mapped_to_an_identifier() -> None:
    """Documented exclusion: a receipt number/tower ID is not a stable party identifier."""
    reference = _observation(
        observation_type="financial_identifier_mention",
        extracted_entities=[
            ExtractedEntityMention(text="TXN123456", entity_type_hint="transaction_reference")
        ],
    )
    tower = _observation(
        observation_type="cdr_tower_mention",
        extracted_entities=[ExtractedEntityMention(text="TWR-1", entity_type_hint="cell_tower_id")],
    )
    assert descriptor_from_observation(reference) is None
    assert descriptor_from_observation(tower) is None


def test_cdr_call_record_maps_only_the_caller_number_not_the_callee() -> None:
    """Documented Phase 5A limitation: a combined record's identifier kind can
    only hold one value; see `descriptor_from_observation`'s docstring."""
    observation = _observation(
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919876543210",
            "callee_number": "+919999999999",
            "timestamp": "2026-01-01T10:00:00+00:00",
        },
    )
    descriptor = descriptor_from_observation(observation)
    assert descriptor is not None
    assert descriptor.identifiers == {"phone": "+919876543210"}
    assert "+919999999999" not in descriptor.identifiers.values()


# --- Scenario 4: weak alias/transliteration candidates stay candidate-only --


def test_chat_message_sender_and_transliteration_become_weak_retrieval_signal() -> None:
    observation = _observation(
        observation_type="chat_message",
        attributes={
            "platform": "whatsapp",
            "sender": "राहुल शर्मा",
            "sender_transliteration_candidates": [
                {"original_text": "राहुल", "candidates": ["राहुल", "raahula"]},
                {"original_text": "शर्मा", "candidates": ["शर्मा", "sharmaa"]},
            ],
        },
    )
    descriptor = descriptor_from_observation(observation)
    assert descriptor is not None
    assert descriptor.aliases == ("राहुल शर्मा",)
    assert descriptor.transliterations == ("राहुल", "raahula", "शर्मा", "sharmaa")
    assert descriptor.platform == "whatsapp"
    # No exact identifier is ever derived from a name/transliteration alone.
    assert descriptor.identifiers == {}


def test_transliteration_only_match_produces_a_candidate_never_a_verified_status() -> None:
    case_id = uuid4()
    first = _observation(
        case_id=case_id,
        observation_type="chat_message",
        attributes={
            "sender": "Rahul",
            "sender_transliteration_candidates": [
                {"original_text": "राहुल", "candidates": ["raahula"]}
            ],
        },
    )
    second = _observation(
        case_id=case_id,
        observation_type="chat_message",
        attributes={"sender": "raahula"},
    )
    candidates = retrieve_candidates(
        [descriptor_from_observation(first), descriptor_from_observation(second)]  # type: ignore[list-item]
    )
    assert len(candidates) == 1
    assert "transliteration_candidate" in candidates[0].reasons
    # `CandidateStatus` has exactly three values, none of which is a verified
    # identity -- see `app.modules.graph.intelligence.models.CandidateStatus`.
    from app.modules.graph.intelligence.models import CandidateStatus

    assert set(CandidateStatus) == {
        CandidateStatus.CANDIDATE,
        CandidateStatus.NEEDS_REVIEW,
        CandidateStatus.REJECTED,
    }


def test_transliteration_match_is_symmetric_regardless_of_observation_id_ordering() -> None:
    """Regression test: `retrieval.retrieve_candidates` fixes which of two
    descriptors is treated as "left" by comparing their `observation_id`
    strings (`_ordered`), independent of the caller's list order -- so the
    transliteration-vs-alias overlap check must be symmetric in both
    directions, or its result would depend on which random UUID happened to
    sort first. Runs the same pair under both possible ID orderings to
    prove the reason fires either way."""
    from app.modules.graph.intelligence.models import ObservationDescriptor

    case_id = uuid4()
    low_id, high_id = sorted([uuid4(), uuid4()], key=str)
    assert str(low_id) < str(high_id)

    transliterated = ObservationDescriptor(
        case_id=case_id,
        observation_id=low_id,
        evidence_id=uuid4(),
        source_locator_reference="message_id=m1",
        aliases=("Rahul",),
        transliterations=("raahula",),
    )
    plain_alias = ObservationDescriptor(
        case_id=case_id,
        observation_id=high_id,
        evidence_id=uuid4(),
        source_locator_reference="message_id=m2",
        aliases=("raahula",),
    )
    # `low_id` sorts first regardless of call order -- so `transliterated`
    # lands as "left" here...
    candidates_a = retrieve_candidates([transliterated, plain_alias])
    assert len(candidates_a) == 1
    assert "transliteration_candidate" in candidates_a[0].reasons

    # ...and as "right" once the IDs are swapped, exercising the
    # previously-missing reverse-direction check.
    transliterated_high = ObservationDescriptor(
        case_id=case_id,
        observation_id=high_id,
        evidence_id=transliterated.evidence_id,
        source_locator_reference=transliterated.source_locator_reference,
        aliases=transliterated.aliases,
        transliterations=transliterated.transliterations,
    )
    plain_alias_low = ObservationDescriptor(
        case_id=case_id,
        observation_id=low_id,
        evidence_id=plain_alias.evidence_id,
        source_locator_reference=plain_alias.source_locator_reference,
        aliases=plain_alias.aliases,
    )
    candidates_b = retrieve_candidates([transliterated_high, plain_alias_low])
    assert len(candidates_b) == 1
    assert "transliteration_candidate" in candidates_b[0].reasons


def test_username_or_handle_maps_to_a_platform_scoped_handle_not_an_identifier() -> None:
    observation = _observation(
        observation_type="username_or_handle",
        extracted_entities=[ExtractedEntityMention(text="@alice_k")],
        attributes={"platform": "whatsapp"},
    )
    descriptor = descriptor_from_observation(observation)
    assert descriptor is not None
    assert descriptor.handles == ("@alice_k",)
    assert descriptor.identifiers == {}


# --- Scenario 12: Phase 4 provenance (frame/time/message-id/json-path) survives --


def test_locator_reference_distinguishes_message_id_json_path_and_frame_locators() -> None:
    message = _observation(
        observation_type="chat_message",
        attributes={"sender": "A"},
        locator=SourceLocator(message_id="m1"),
    )
    json_path = _observation(
        observation_type="phone_number_mention",
        extracted_entities=[ExtractedEntityMention(text="123", entity_type_hint="phone_number")],
        locator=SourceLocator(json_path="$.records[3]"),
    )
    frame = _observation(
        observation_type="chat_message",
        attributes={"sender": "A"},
        locator=SourceLocator(frame_number=42, time_start_ms=1000, time_end_ms=2000),
    )
    references = {
        descriptor_from_observation(message).source_locator_reference,  # type: ignore[union-attr]
        descriptor_from_observation(json_path).source_locator_reference,  # type: ignore[union-attr]
        descriptor_from_observation(frame).source_locator_reference,  # type: ignore[union-attr]
    }
    assert len(references) == 3  # all distinct, all bounded, all deterministic
    assert "message_id=m1" in references
    assert "json_path=$.records[3]" in references
    assert any("frame=42" in ref and "time=1000:2000" in ref for ref in references)


def test_locator_reference_is_deterministic_for_the_same_observation() -> None:
    observation = _observation(observation_type="chat_message", attributes={"sender": "A"})
    from app.modules.graph.intelligence.sourcing import _locator_reference

    assert _locator_reference(observation) == _locator_reference(observation)


# --- Scenario 13: raw content excluded from graph-facing properties ---------


def test_raw_message_text_never_appears_in_the_derived_descriptor() -> None:
    """A chat message's own body text must never leak into retrieval input --
    only the bounded sender/alias/handle/platform fields do."""
    sensitive_text = "meet me at the warehouse, bring the package, tell no one"
    observation = _observation(
        observation_type="chat_message",
        attributes={"sender": "Alice", "text": sensitive_text, "text_present": True},
    )
    descriptor = descriptor_from_observation(observation)
    assert descriptor is not None
    dumped = descriptor.model_dump_json()
    assert sensitive_text not in dumped
    assert "warehouse" not in dumped
    assert "package" not in dumped


def test_unmapped_observation_types_are_skipped_not_guessed_at() -> None:
    for observation_type in (
        "transcript_segment",
        "audio_metadata",
        "diarization_speaker_turn",
        "url",
        "amount_mention",
        "object_detection",
    ):
        observation = _observation(
            observation_type=observation_type,
            attributes={"text": "irrelevant"},
            extracted_entities=[
                ExtractedEntityMention(text="x", entity_type_hint="speaker_label_local")
            ],
        )
        assert descriptor_from_observation(observation) is None


# --- Motif adapter: real cross-modal shapes ---------------------------------


def test_motif_edges_are_deterministic_for_the_same_observations() -> None:
    case_id = uuid4()
    call = _observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919876543210",
            "callee_number": "+919999999999",
            "timestamp": "2026-01-01T10:00:00+00:00",
        },
    )
    transfer = _observation(
        case_id=case_id,
        observation_type="financial_transaction_record",
        attributes={
            "sender_account": "9999999999",
            "receiver_account": "ACC-000",
            "timestamp": "2026-01-01T10:20:00+00:00",
        },
    )
    first = build_motif_edges([call, transfer])
    second = build_motif_edges([call, transfer])
    assert first == second
    assert len(first) == 2


def test_motif_edges_never_reference_a_fabricated_observation_id() -> None:
    """Every edge's `evidence_observation_ids` must be a real, traceable
    observation the caller passed in -- never a synthesized ID."""
    call = _observation(
        observation_type="cdr_call_record",
        attributes={"caller_number": "+919876543210", "timestamp": "2026-01-01T10:00:00+00:00"},
    )
    edges = build_motif_edges([call])
    assert len(edges) == 1
    assert edges[0].evidence_observation_ids == (call.observation_id,)
