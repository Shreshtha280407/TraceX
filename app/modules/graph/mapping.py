"""Pure, versioned mapping from canonical observations to graph projections.

This module deliberately knows only the canonical ``ObservationV1`` shape.
It has no database, network, worker, or model dependency.  A plan describes
source-backed claim references and temporal events; ``projection.py`` is the
only place that turns that plan into Cypher writes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from app.contracts.observation import ObservationV1
from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid
from app.modules.graph.models import GraphModel, MediaProjectionLineage

MAPPING_REGISTRY_VERSION = "phase_3_observation_mapping_v1"
MAPPING_CONFIG_HASH = canonical_sha256(
    {
        "version": MAPPING_REGISTRY_VERSION,
        "document_claim_types": [
            "fir_reference",
            "police_station_mention",
            "date_time_mention",
            "phone_number_mention",
            "email_address_mention",
            "vehicle_identifier_mention",
            "financial_identifier_mention",
            "amount_mention",
            "legal_section_mention",
            "ner_entity_mention",
        ],
        "document_relation_types": [
            "person_contact_association",
            "dated_communication_reference",
            "transaction_claim",
            "incident_event_mention",
        ],
        "cdr_type": "cdr_call_record",
        "finance_type": "financial_transaction_record",
        "rules": "case-observation-version-fingerprint; event-first; no-resolution",
    }
)

MEDIA_MAPPING_VERSION = "phase_4_media_temporal_mapping_v1"
MEDIA_MAPPING_CONFIG_HASH = canonical_sha256(
    {
        "version": MEDIA_MAPPING_VERSION,
        "sighting_types": ["object_detection", "text_region_detection", "anonymous_track_segment"],
        "speech_types": ["transcript_segment", "diarization_speaker_turn"],
        "message_types": ["chat_message"],
        "meeting_candidate_type": "meeting_candidate",
        "rules": "event-first; source-relative-time-is-not-utc; candidate-only; no-resolution",
    }
)


class MappingStatus(StrEnum):
    APPLIED = "applied"
    UNSUPPORTED = "unsupported"
    DEFERRED = "deferred"


class SourceClaimPlan(GraphModel):
    claim_id: UUID
    claim_type: str
    role: str
    display_label: str


class TemporalEventPlan(GraphModel):
    projection_id: UUID
    event_type: str
    event_time: datetime | None = None
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    properties: dict[str, str | float | int | bool | tuple[str, ...]] = Field(default_factory=dict)
    supporting_observation_ids: tuple[UUID, ...] = ()
    contradictory_observation_ids: tuple[UUID, ...] = ()


class GraphProjectionPlan(GraphModel):
    status: MappingStatus
    reason: str
    mapping_version: str = MAPPING_REGISTRY_VERSION
    mapping_config_hash: str = MAPPING_CONFIG_HASH
    claims: tuple[SourceClaimPlan, ...] = ()
    event: TemporalEventPlan | None = None
    media_lineage: MediaProjectionLineage | None = None


_DOCUMENT_CLAIM_TYPES = frozenset(
    {
        "fir_reference",
        "police_station_mention",
        "date_time_mention",
        "phone_number_mention",
        "email_address_mention",
        "vehicle_identifier_mention",
        "financial_identifier_mention",
        "amount_mention",
        "legal_section_mention",
        "ner_entity_mention",
    }
)
_DOCUMENT_RELATION_TYPES = frozenset(
    {
        "person_contact_association",
        "dated_communication_reference",
        "transaction_claim",
        "incident_event_mention",
    }
)


def _string(attributes: dict[str, Any], name: str) -> str | None:
    value = attributes.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _fingerprint(observation: ObservationV1, values: object) -> str:
    return canonical_sha256(
        {
            "case_id": str(observation.case_id),
            "observation_id": str(observation.observation_id),
            "mapping_version": MAPPING_REGISTRY_VERSION,
            "values": values,
        }
    )


def _claim(
    observation: ObservationV1, *, claim_type: str, role: str, label: str, fingerprint: str
) -> SourceClaimPlan:
    return SourceClaimPlan(
        claim_id=deterministic_uuid(
            "phase_3_source_claim",
            str(observation.case_id),
            str(observation.observation_id),
            MAPPING_REGISTRY_VERSION,
            fingerprint,
            role,
        ),
        claim_type=claim_type,
        role=role,
        display_label=label,
    )


def _outcome(
    status: MappingStatus,
    reason: str,
    *,
    mapping_version: str = MAPPING_REGISTRY_VERSION,
    mapping_config_hash: str = MAPPING_CONFIG_HASH,
) -> GraphProjectionPlan:
    return GraphProjectionPlan(
        status=status,
        reason=reason,
        mapping_version=mapping_version,
        mapping_config_hash=mapping_config_hash,
    )


def _document_plan(observation: ObservationV1) -> GraphProjectionPlan:
    if observation.observation_type in _DOCUMENT_CLAIM_TYPES:
        if len(observation.extracted_entities) != 1:
            return _outcome(MappingStatus.DEFERRED, "document_claim_requires_one_source_mention")
        mention = observation.extracted_entities[0]
        fingerprint = _fingerprint(
            observation,
            {
                "type": observation.observation_type,
                "label": mention.text,
                "hint": mention.entity_type_hint,
            },
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="document_source_claim",
            claims=(
                _claim(
                    observation,
                    claim_type=observation.observation_type,
                    role="mention",
                    label=mention.text,
                    fingerprint=fingerprint,
                ),
            ),
        )

    if observation.observation_type in _DOCUMENT_RELATION_TYPES:
        rule = _string(dict(observation.attributes), "rule")
        if rule is None:
            return _outcome(MappingStatus.DEFERRED, "document_relation_requires_rule_provenance")
        fingerprint = _fingerprint(
            observation, {"type": observation.observation_type, "rule": rule}
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="document_relation_claim",
            claims=(
                _claim(
                    observation,
                    claim_type="document_relation_claim",
                    role="source_claim",
                    label=observation.observation_type,
                    fingerprint=fingerprint,
                ),
            ),
        )
    return _outcome(MappingStatus.UNSUPPORTED, "unsupported_observation_type")


def _cdr_plan(observation: ObservationV1) -> GraphProjectionPlan:
    attributes = dict(observation.attributes)
    caller = _string(attributes, "caller_number")
    callee = _string(attributes, "callee_number")
    timestamp = _canonical_time(_string(attributes, "timestamp"))
    if caller is None or callee is None or timestamp is None:
        return _outcome(
            MappingStatus.DEFERRED, "cdr_call_requires_caller_callee_and_canonical_time"
        )
    fingerprint = _fingerprint(
        observation,
        {
            "caller": caller,
            "callee": callee,
            "timestamp": timestamp.isoformat(),
            "attrs": attributes,
        },
    )
    properties: dict[str, str | float | int | bool | tuple[str, ...]] = {}
    call_type = _string(attributes, "call_type")
    if call_type is not None:
        properties["call_direction"] = call_type
    duration = attributes.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        properties["duration_seconds"] = duration
    tower = _string(attributes, "cell_tower_id")
    if tower is not None:
        properties["cell_site_claim"] = tower
    claims = (
        _claim(
            observation,
            claim_type="communication_endpoint",
            role="caller",
            label=caller,
            fingerprint=fingerprint,
        ),
        _claim(
            observation,
            claim_type="communication_endpoint",
            role="callee",
            label=callee,
            fingerprint=fingerprint,
        ),
    )
    return GraphProjectionPlan(
        status=MappingStatus.APPLIED,
        reason="cdr_call_event",
        claims=claims,
        event=TemporalEventPlan(
            projection_id=deterministic_uuid(
                "phase_3_temporal_event",
                str(observation.case_id),
                str(observation.observation_id),
                MAPPING_REGISTRY_VERSION,
                fingerprint,
            ),
            event_type="cdr_call",
            event_time=timestamp,
            properties=properties,
        ),
    )


def _finance_plan(observation: ObservationV1) -> GraphProjectionPlan:
    attributes = dict(observation.attributes)
    source = _string(attributes, "sender_account")
    destination = _string(attributes, "receiver_account")
    amount = _string(attributes, "amount")
    currency = _string(attributes, "currency")
    timestamp = _canonical_time(_string(attributes, "timestamp"))
    if source is None or destination is None:
        return _outcome(
            MappingStatus.DEFERRED, "transaction_requires_source_and_destination_instruments"
        )
    if amount is None or currency is None or timestamp is None:
        return _outcome(
            MappingStatus.DEFERRED, "transaction_requires_amount_currency_and_canonical_time"
        )
    try:
        Decimal(amount)
    except InvalidOperation:
        return _outcome(MappingStatus.DEFERRED, "transaction_amount_is_not_canonical_decimal")
    fingerprint = _fingerprint(
        observation,
        {
            "source": source,
            "destination": destination,
            "amount": amount,
            "currency": currency,
            "timestamp": timestamp.isoformat(),
            "attrs": attributes,
        },
    )
    properties: dict[str, str | float | int | bool | tuple[str, ...]] = {
        "amount": amount,
        "currency": currency,
    }
    for source_name, target_name in (
        ("direction", "transaction_direction"),
        ("transaction_id", "transaction_reference"),
        ("reference", "transaction_reference"),
        ("channel", "channel"),
    ):
        value = _string(attributes, source_name)
        if value is not None:
            properties[target_name] = value
    claims = (
        _claim(
            observation,
            claim_type="financial_instrument",
            role="source",
            label=source,
            fingerprint=fingerprint,
        ),
        _claim(
            observation,
            claim_type="financial_instrument",
            role="destination",
            label=destination,
            fingerprint=fingerprint,
        ),
    )
    return GraphProjectionPlan(
        status=MappingStatus.APPLIED,
        reason="financial_transaction_event",
        claims=claims,
        event=TemporalEventPlan(
            projection_id=deterministic_uuid(
                "phase_3_temporal_event",
                str(observation.case_id),
                str(observation.observation_id),
                MAPPING_REGISTRY_VERSION,
                fingerprint,
            ),
            event_type="financial_transaction",
            event_time=timestamp,
            properties=properties,
        ),
    )


def _bounded_string(attributes: dict[str, Any], name: str, *, maximum: int = 256) -> str | None:
    value = _string(attributes, name)
    return value if value is not None and len(value) <= maximum else None


def _media_claims(
    observation: ObservationV1, values: tuple[tuple[str, str, str], ...], fingerprint: str
) -> tuple[SourceClaimPlan, ...]:
    return tuple(
        _claim(
            observation,
            claim_type=claim_type,
            role=role,
            label=label,
            fingerprint=fingerprint,
        )
        for claim_type, role, label in values
    )


def _media_time_properties(observation: ObservationV1) -> dict[str, str | int | bool]:
    """Represent canonical time without converting relative offsets into UTC.

    ``ObservationV1`` already validates each range.  This function merely
    records which already-canonical basis was supplied; it never derives a
    wall-clock instant from a frame number, FPS, or source-relative offset.
    """
    props: dict[str, str | int | bool] = {}
    locator = observation.source_locator
    if observation.event_time is not None:
        props["temporal_precision"] = "exact_instant"
    elif observation.time_window is not None:
        if observation.time_window.start is not None and observation.time_window.end is not None:
            props["temporal_precision"] = "bounded_window"
        else:
            props["temporal_precision"] = "partial_window"
    elif locator.time_start_ms is not None or locator.time_end_ms is not None:
        props["temporal_precision"] = "source_relative_ms"
    else:
        props["temporal_precision"] = "unknown"
        props["temporal_precision_insufficient"] = True
    if locator.time_start_ms is not None:
        props["source_time_start_ms"] = locator.time_start_ms
    if locator.time_end_ms is not None:
        props["source_time_end_ms"] = locator.time_end_ms
    if locator.frame_number is not None:
        props["source_frame_number"] = locator.frame_number
    if observation.event_time is not None and observation.time_window is not None:
        start, end = observation.time_window.start, observation.time_window.end
        if (start is not None and observation.event_time < start) or (
            end is not None and observation.event_time > end
        ):
            props["temporal_conflict"] = True
    return props


def _media_event(
    observation: ObservationV1,
    *,
    event_type: str,
    fingerprint_values: dict[str, Any],
    properties: dict[str, str | float | int | bool | tuple[str, ...]],
    supporting_observation_ids: tuple[UUID, ...] = (),
    contradictory_observation_ids: tuple[UUID, ...] = (),
) -> TemporalEventPlan:
    fingerprint = _fingerprint(observation, fingerprint_values)
    return TemporalEventPlan(
        projection_id=deterministic_uuid(
            "phase_4_temporal_event",
            str(observation.case_id),
            str(observation.observation_id),
            MEDIA_MAPPING_VERSION,
            fingerprint,
        ),
        event_type=event_type,
        event_time=observation.event_time,
        time_window_start=observation.time_window.start if observation.time_window else None,
        time_window_end=observation.time_window.end if observation.time_window else None,
        properties={**_media_time_properties(observation), **properties},
        supporting_observation_ids=supporting_observation_ids,
        contradictory_observation_ids=contradictory_observation_ids,
    )


def _media_plan(observation: ObservationV1) -> GraphProjectionPlan:
    attributes = dict(observation.attributes)
    observation_type = observation.observation_type
    base = {
        "type": observation_type,
        "locator": observation.source_locator.model_dump(mode="json"),
        "event_time": observation.event_time.isoformat() if observation.event_time else None,
        "time_window": observation.time_window.model_dump(mode="json")
        if observation.time_window
        else None,
    }

    if observation_type in {
        "object_detection",
        "text_region_detection",
        "anonymous_track_segment",
    }:
        label = _bounded_string(attributes, "detected_label", maximum=128)
        # Tracking labels are source-local classifications, never identities.
        claims: tuple[SourceClaimPlan, ...] = ()
        if label is not None:
            fingerprint = _fingerprint(observation, {**base, "label": label})
            claims = _media_claims(
                observation, (("visual_label", "observed_label", label),), fingerprint
            )
        event = _media_event(
            observation,
            event_type="sighting",
            fingerprint_values={**base, "label": label},
            properties={"sighting_kind": observation_type, "candidate_only": False},
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="media_sighting_event",
            mapping_version=MEDIA_MAPPING_VERSION,
            mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            claims=claims,
            event=event,
        )

    if observation_type in {"transcript_segment", "diarization_speaker_turn"}:
        claims = ()
        if (
            observation_type == "diarization_speaker_turn"
            and len(observation.extracted_entities) == 1
        ):
            label = observation.extracted_entities[0].text
            fingerprint = _fingerprint(observation, {**base, "speaker": label})
            claims = _media_claims(
                observation, (("speaker_label_local", "speaker_candidate", label),), fingerprint
            )
        language = _bounded_string(attributes, "language_hint", maximum=64)
        props: dict[str, str | float | int | bool | tuple[str, ...]] = {
            "segment_kind": observation_type,
            "candidate_only": False,
        }
        if language is not None:
            props["language_hint"] = language
        event = _media_event(
            observation,
            event_type="speech_segment",
            fingerprint_values={**base, "segment_kind": observation_type, "language": language},
            properties=props,
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="media_speech_segment_event",
            mapping_version=MEDIA_MAPPING_VERSION,
            mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            claims=claims,
            event=event,
        )

    if observation_type == "chat_message":
        participant_values: list[tuple[str, str, str]] = []
        sender = _bounded_string(attributes, "sender")
        if sender is not None:
            participant_values.append(("platform_handle_claim", "sender_candidate", sender))
        participants = attributes.get("participants")
        if isinstance(participants, list):
            for index, participant in enumerate(participants):
                if (
                    isinstance(participant, str)
                    and participant.strip()
                    and len(participant.strip()) <= 256
                ):
                    participant_values.append(
                        (
                            "platform_handle_claim",
                            f"recipient_candidate_{index}",
                            participant.strip(),
                        )
                    )
        fingerprint = _fingerprint(observation, {**base, "participants": participant_values})
        claims = _media_claims(observation, tuple(participant_values), fingerprint)
        props = {"candidate_only": False}
        for source, target, maximum in (
            ("platform", "platform", 64),
            ("conversation_id", "channel_reference", 256),
            ("timestamp_source_timezone", "timestamp_source_timezone", 128),
        ):
            value = _bounded_string(attributes, source, maximum=maximum)
            if value is not None:
                props[target] = value
        event = _media_event(
            observation,
            event_type="message",
            fingerprint_values={**base, "participants": participant_values, "props": props},
            properties=props,
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="media_message_event",
            mapping_version=MEDIA_MAPPING_VERSION,
            mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            claims=claims,
            event=event,
        )

    if observation_type == "meeting_candidate":
        reason = _bounded_string(attributes, "candidate_reason_category", maximum=128)
        status = _bounded_string(attributes, "candidate_status", maximum=32)
        if reason is None or status != "candidate":
            return _outcome(
                MappingStatus.DEFERRED,
                "meeting_candidate_requires_explicit_candidate_status_and_reason",
                mapping_version=MEDIA_MAPPING_VERSION,
                mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            )

        def referenced_ids(name: str) -> tuple[UUID, ...] | None:
            raw = attributes.get(name, [])
            if not isinstance(raw, list):
                return None
            try:
                values = tuple(UUID(value) for value in raw if isinstance(value, str))
            except ValueError:
                return None
            return values if len(values) == len(raw) and len(set(values)) == len(values) else None

        supporting = referenced_ids("contributing_observation_ids")
        contradictory = referenced_ids("contradictory_observation_ids")
        if supporting is None or contradictory is None:
            return _outcome(
                MappingStatus.DEFERRED,
                "meeting_candidate_has_invalid_observation_references",
                mapping_version=MEDIA_MAPPING_VERSION,
                mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            )
        event = _media_event(
            observation,
            event_type="meeting_candidate",
            fingerprint_values={
                **base,
                "reason": reason,
                "supporting": [str(value) for value in supporting],
                "contradictory": [str(value) for value in contradictory],
            },
            properties={
                "candidate_only": True,
                "candidate_status": "candidate",
                "candidate_reason_category": reason,
                "temporal_precision_insufficient": _media_time_properties(observation).get(
                    "temporal_precision"
                )
                in {"unknown", "partial_window"},
            },
            supporting_observation_ids=supporting,
            contradictory_observation_ids=contradictory,
        )
        return GraphProjectionPlan(
            status=MappingStatus.APPLIED,
            reason="explicit_meeting_candidate_event",
            mapping_version=MEDIA_MAPPING_VERSION,
            mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
            event=event,
        )

    return _outcome(
        MappingStatus.UNSUPPORTED,
        "unsupported_observation_type",
        mapping_version=MEDIA_MAPPING_VERSION,
        mapping_config_hash=MEDIA_MAPPING_CONFIG_HASH,
    )


def map_observation(
    observation: ObservationV1, media_lineage: MediaProjectionLineage | None = None
) -> GraphProjectionPlan:
    """Return one stable plan or an explicit safe outcome for an observation."""
    if observation.observation_type == "cdr_call_record":
        plan = _cdr_plan(observation)
    elif observation.observation_type == "financial_transaction_record":
        plan = _finance_plan(observation)
    elif observation.observation_type in _DOCUMENT_CLAIM_TYPES | _DOCUMENT_RELATION_TYPES:
        plan = _document_plan(observation)
    else:
        plan = _media_plan(observation)
    return plan.model_copy(update={"media_lineage": media_lineage}) if media_lineage else plan
