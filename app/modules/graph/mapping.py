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
from app.modules.graph.models import GraphModel

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
    event_time: datetime
    properties: dict[str, str | float | int | bool] = Field(default_factory=dict)


class GraphProjectionPlan(GraphModel):
    status: MappingStatus
    reason: str
    mapping_version: str = MAPPING_REGISTRY_VERSION
    mapping_config_hash: str = MAPPING_CONFIG_HASH
    claims: tuple[SourceClaimPlan, ...] = ()
    event: TemporalEventPlan | None = None


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


def _outcome(status: MappingStatus, reason: str) -> GraphProjectionPlan:
    return GraphProjectionPlan(status=status, reason=reason)


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
    properties: dict[str, str | float | int | bool] = {}
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
    properties: dict[str, str | float | int | bool] = {"amount": amount, "currency": currency}
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


def map_observation(observation: ObservationV1) -> GraphProjectionPlan:
    """Return one stable plan or an explicit safe outcome for an observation."""
    if observation.observation_type == "cdr_call_record":
        return _cdr_plan(observation)
    if observation.observation_type == "financial_transaction_record":
        return _finance_plan(observation)
    return _document_plan(observation)
