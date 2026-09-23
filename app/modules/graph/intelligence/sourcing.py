"""Adapts canonical persisted `ObservationV1` rows into Phase 5 intelligence inputs.

This is the missing seam identified by the Phase 5A reconciliation audit:
`retrieval.py`/`scoring.py`/`correlation.py`/`analytics.py` are all real,
deterministic, and unit-tested -- but every one of them operates on
caller-supplied Python objects (`ObservationDescriptor`, `GraphEdgeSnapshot`)
with no adapter anywhere that builds those objects from real, persisted
Phase 4 canonical observations. This module is that adapter, and nothing
else -- it never scores, never retrieves, never writes to Neo4j or
PostgreSQL beyond a read-only `worker_observations` query.

## Mapping scope (documented, bounded, not silently exhaustive)

Only observation types with a genuinely stable, source-backed identity or
temporal signal are mapped. Matching is keyed on `entity_type_hint` when an
observation's one extracted entity carries one (Jasraj's regex/record
mentions all set one, and several mention types share one hint vocabulary --
e.g. `financial_identifier_mention` covers `upi_id`/`account_number`/
`transaction_reference`), else on the observation's own type (Sarthak's flat
`phone_number`/`email_address`/`username_or_handle` observations set no
hint). Anything not listed below is skipped explicitly (returns `None`),
never guessed at:

- Sarthak's `communication_processing`: `chat_message` (sender name as an
  alias, its transliteration candidates, platform, event time),
  `phone_number`/`email_address` (exact identifiers), `username_or_handle`
  (platform handle).
- Jasraj's `structured_processing` FIR/document mentions (by
  `entity_type_hint`): `phone_number`, `email_address`,
  `vehicle_registration`, `upi_id`/`account_number` (both treated as the
  `account` identifier kind), `imei`/`imsi` (both `device_id`).
- Jasraj's CDR/finance combined-record observations: `cdr_call_record`
  (caller number only -- see `descriptor_from_observation`'s docstring for
  why the callee number isn't independently blockable from this shape),
  `financial_transaction_record` (sender account only, same reasoning).
  Both also contribute their record's own `vehicle_context` field (Gap-
  Closure follow-up) as a `vehicle_registration` identifier -- widening
  which modality supplies that *existing* identifier kind (already
  trusted from FIR/document OCR above), not a new one.
- `json_scalar_value` (Gap-Closure follow-up; `generic_json_v1`'s one-
  observation-per-scalar-leaf fallback, e.g. Fulcrum's `structured/
  sightings.json`): a `vehicle_id`/`vehicle_context`-named leaf (matched
  on `source_locator.json_path`'s exact trailing segment, never a
  substring) is mapped the same way, as `vehicle_registration`. Every
  other leaf name (`person_id`, `location_id`, `event_id`, `timestamp`,
  ...) stays unmapped -- see "Deliberately NOT mapped" below.

Deliberately NOT mapped, with reasons:

- `person_id`/`location_id`-shaped fields, however they arrive (structured
  field or OCR'd text) -- categorically different from a vehicle
  registration plate: no external registry makes a person's or a
  location's identity a directly-labeled, independently-verifiable field
  in real evidence. That's exactly what entity resolution exists to
  *infer* from contact-method/vehicle signals, never to accept as given;
  accepting it directly would be circular for real (non-synthetic) data.
  `_IDENTIFIER_TYPES` has no `person`/`location` kind at all, deliberately.

- `diarization_speaker_turn` (`speaker_label`) -- a source-local label,
  meaningless outside the one evidence file it came from; treating it as an
  alias would let a diarization label silently become identity-linking
  signal, which `communication_processing`'s own contract forbids.
- `transcript_segment`, `audio_metadata`, `url`, `transaction_reference`
  (a receipt number, not a stable party identifier), `cell_tower_id` (a
  location, not a person identifier), `amount_mention`,
  `object_detection`/`text_region_detection`/`anonymous_track_segment` (media
  sightings) -- no identifier, alias, or handle a person could be
  meaningfully blocked or aliased on; a raw OCR text mention is source text,
  not a parsed identifier claim, and is excluded from candidate retrieval
  input for the same reason raw content never appears in graph-facing
  properties.
- `meeting_candidate` (media) -- see `build_motif_edges`'s docstring for why
  it participates in the temporal motif via evidence citation rather than
  through `descriptor_from_observation`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.contracts.observation import ObservationV1
from app.core.ids import deterministic_uuid
from app.modules.evidence_lifecycle.repository import worker_observations_table
from app.modules.graph.integration_models import CandidateLinkRecord
from app.modules.graph.intelligence.models import GraphEdgeSnapshot, ObservationDescriptor
from app.modules.graph.intelligence.retrieval import normalise_identifier

#: Observation types this adapter can turn into retrieval-eligible descriptors.
#: Anything not listed here is a documented, deliberate skip (see module docstring).
_ALIAS_BEARING_TYPES = frozenset({"chat_message"})
_HANDLE_BEARING_TYPES = frozenset({"username_or_handle"})
#: Keyed by `_identifier_kind_key` (an entity_type_hint when the observation
#: carries one -- Jasraj's `fir_report.py`/`cdr.py`/`finance.py` regex/record
#: mentions all set one -- else the observation's own type, Sarthak's
#: `phone_number`/`email_address` flat observations set none). Deliberately
#: excludes `transaction_reference` (a receipt number, not a stable party
#: identifier) and `cell_tower_id` (a location, not a person identifier).
_EXACT_IDENTIFIER_ENTITY_TYPES: dict[str, str] = {
    "phone_number": "phone",
    "email_address": "email",
    "vehicle_registration": "vehicle_registration",
    "upi_id": "account",
    "account_number": "account",
    "account": "account",
    "imei": "device_id",
    "imsi": "device_id",
}


def _identifier_kind_key(observation: ObservationV1) -> str:
    """The key `_EXACT_IDENTIFIER_ENTITY_TYPES` is matched against.

    A mention-shaped observation (Jasraj's regex/record mentions) sets
    `entity_type_hint` on its one extracted entity -- several observation
    types share one `entity_type_hint` vocabulary (e.g. `financial_
    identifier_mention` covers `upi_id`/`account_number`/
    `transaction_reference`), so the hint, not the observation type, is the
    real identifier kind. A flat observation (Sarthak's `phone_number`/
    `email_address`) sets no hint at all, so its own observation type
    already *is* the kind.
    """
    if observation.extracted_entities:
        hint = observation.extracted_entities[0].entity_type_hint
        if hint is not None:
            return hint
    return observation.observation_type


def _event_time_of(observation: ObservationV1) -> tuple[datetime | None, datetime | None]:
    """A single instant, a bounded window, or a canonical CDR/finance `timestamp`
    attribute -- in that order. Never a guess: a record with none of these has no
    resolvable time for retrieval/analytics purposes, exactly as it has none for
    graph mapping (see `docs/qa/known-limitations.md`'s "Raw document dates
    remain non-temporal claims")."""
    if observation.event_time is not None:
        return observation.event_time, observation.event_time
    if observation.time_window is not None:
        return observation.time_window.start, observation.time_window.end
    raw = observation.attributes.get("timestamp")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None, None
        return parsed, parsed
    return None, None


def _flatten_transliteration_candidates(value: object) -> tuple[str, ...]:
    """`chat_message.attributes["sender_transliteration_candidates"]` is a list of
    `{"candidates": [...], ...}` dicts (see `communication_processing.social.
    common._candidate_attribute`) -- flatten every candidate string, never the
    original-script text twice over (that's already carried via `aliases`)."""
    if not isinstance(value, list):
        return ()
    flattened: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        candidates = entry.get("candidates")
        if isinstance(candidates, list):
            flattened.extend(item for item in candidates if isinstance(item, str) and item)
    return tuple(dict.fromkeys(flattened))


def _is_structured_validation_rejected(observation: ObservationV1) -> bool:
    """Producer-validation gate for Jasraj's structured (document/CDR/finance)
    family, shared by `descriptor_from_observation` and the motif-edge
    builder below (which reads CDR/finance attributes directly, not through
    that function).

    This producer module names its own validation attribute
    `source_signal_quality` (see `structured_processing.signal_validation.
    SignalValidationResult`) -- a different key than `communication_signal_
    validation`/`visual_signal_validation`, since each producer module
    independently established its own attribute name; the semantic this
    sourcing boundary enforces is identical across all three: a present,
    non-"accepted" outcome never reaches descriptor/correlation/motif input.
    Only `cdr.py`/`finance.py` attach this attribute today (always
    "accepted" currently, but never assumed to stay that way); `fir_report.
    py`'s regex-extracted document mentions attach none at all -- treated
    exactly like an observation with no communication validation attribute,
    never rejected merely for predating this additive metadata.
    """
    validation = observation.attributes.get("source_signal_quality")
    outcome = validation.get("outcome") if isinstance(validation, dict) else None
    return outcome is not None and outcome != "accepted"


def descriptor_from_observation(observation: ObservationV1) -> ObservationDescriptor | None:
    """Map one canonical observation into retrieval input, or `None` if this
    observation type carries no source-backed identity/alias/handle signal
    (see module docstring for the exact, documented mapping scope)."""
    identifiers: dict[str, str] = {}
    aliases: list[str] = []
    transliterations: list[str] = []
    platform: str | None = None
    handles: list[str] = []

    observation_type = observation.observation_type
    attributes = observation.attributes

    # Producer-side validation is authoritative when present. Legacy Phase 4
    # observations predate this attribute and keep their established mapping;
    # new incomplete/deferred/rejected communication signals never reach
    # retrieval merely because they contain an otherwise plausible value.
    validation = attributes.get("communication_signal_validation")
    if isinstance(validation, dict) and validation.get("correlation_ready") is not True:
        return None
    # Same gate for Jasraj's structured (document/CDR/finance) family -- see
    # `_is_structured_validation_rejected`'s own docstring.
    if _is_structured_validation_rejected(observation):
        return None

    kind_key = _identifier_kind_key(observation)
    if kind_key in _EXACT_IDENTIFIER_ENTITY_TYPES:
        kind = _EXACT_IDENTIFIER_ENTITY_TYPES[kind_key]
        if observation.extracted_entities:
            text = observation.extracted_entities[0].text
            if text.strip():
                identifiers[kind] = text
    elif observation_type == "cdr_call_record":
        # `ObservationDescriptor.identifiers` holds one value per stable-
        # identifier *kind*, and `retrieval.py._IDENTIFIER_TYPES` is a fixed,
        # closed set -- there is no "second phone" kind to hold caller and
        # callee in one descriptor safely. This single-descriptor function
        # therefore still returns the caller's number only, kept for every
        # existing caller of this exact signature; `descriptors_from_
        # observation` (below) is the Phase 5B two-party extension that
        # returns BOTH the caller and callee as independent, role-scoped
        # descriptors, closing the Phase 5A limitation this comment used to
        # document.
        caller = attributes.get("caller_number")
        if isinstance(caller, str) and caller.strip():
            identifiers["phone"] = caller
        vehicle_context = attributes.get("vehicle_context")
        if isinstance(vehicle_context, str) and vehicle_context.strip():
            identifiers["vehicle_registration"] = vehicle_context
    elif observation_type == "financial_transaction_record":
        # Same reasoning as `cdr_call_record` above: this single-descriptor
        # function returns only the sender side; `descriptors_from_
        # observation` returns both sender and receiver.
        sender = attributes.get("sender_account")
        if isinstance(sender, str) and sender.strip():
            identifiers["account"] = sender
        vehicle_context = attributes.get("vehicle_context")
        if isinstance(vehicle_context, str) and vehicle_context.strip():
            identifiers["vehicle_registration"] = vehicle_context
    elif observation_type == "json_scalar_value":
        # Gap-Closure follow-up: `generic_json_v1` (structured/sightings-
        # shaped JSON with no CDR/financial record-array match) produces
        # one observation per scalar leaf, with the field name recoverable
        # only from `source_locator.json_path`'s trailing segment (e.g.
        # `$.records[3].vehicle_id` -> `vehicle_id`) -- `attributes` itself
        # is just `{"value": ...}`. Exact trailing-segment match only,
        # never a substring/contains check, matching this codebase's
        # "never guess ambiguous input" rule applied everywhere else.
        field_name = (observation.source_locator.json_path or "").rsplit(".", 1)[-1]
        if field_name in {"vehicle_id", "vehicle_context"}:
            value = attributes.get("value")
            if isinstance(value, str) and value.strip():
                identifiers["vehicle_registration"] = value
    elif observation_type in _ALIAS_BEARING_TYPES:
        sender = attributes.get("sender")
        if isinstance(sender, str) and sender.strip():
            aliases.append(sender)
        transliterations.extend(
            _flatten_transliteration_candidates(attributes.get("sender_transliteration_candidates"))
        )
        platform_value = attributes.get("platform")
        if isinstance(platform_value, str) and platform_value.strip():
            platform = platform_value
    elif observation_type in _HANDLE_BEARING_TYPES:
        if observation.extracted_entities:
            text = observation.extracted_entities[0].text
            if text.strip():
                handles.append(text)
        platform_value = attributes.get("platform")
        if isinstance(platform_value, str) and platform_value.strip():
            platform = platform_value
    else:
        return None

    if not identifiers and not aliases and not handles:
        return None

    event_start, event_end = _event_time_of(observation)
    return ObservationDescriptor(
        case_id=observation.case_id,
        observation_id=observation.observation_id,
        descriptor_id=deterministic_uuid(
            "phase5_observation_descriptor",
            str(observation.case_id),
            str(observation.observation_id),
        ),
        evidence_id=observation.evidence_id,
        source_locator_reference=_locator_reference(observation),
        identifiers=identifiers,
        aliases=tuple(dict.fromkeys(aliases)),
        transliterations=tuple(dict.fromkeys(transliterations)),
        platform=platform,
        handles=tuple(dict.fromkeys(handles)),
        event_start=event_start,
        event_end=event_end,
    )


#: `(observation_type, attribute_key, identifier_kind, participant_role)` --
#: the CDR/finance two-party fields `descriptors_from_observation` maps.
#: Both entries of a pair MUST share the same identifier kind (both are
#: phone numbers, or both are accounts) since `retrieval.py`'s exact
#: blocking only ever compares like kinds.
_TWO_PARTY_FIELDS: tuple[tuple[str, str, str, str], tuple[str, str, str, str]] = (
    ("cdr_call_record", "caller_number", "phone", "caller"),
    ("cdr_call_record", "callee_number", "phone", "callee"),
)
_TWO_PARTY_FIELDS_FINANCE: tuple[tuple[str, str, str, str], tuple[str, str, str, str]] = (
    ("financial_transaction_record", "sender_account", "account", "sender"),
    ("financial_transaction_record", "receiver_account", "account", "receiver"),
)


def _party_descriptor(
    observation: ObservationV1,
    *,
    role: str,
    identifier_kind: str,
    raw_value: str,
    event_start: datetime | None,
    event_end: datetime | None,
    vehicle_registration: str | None = None,
) -> ObservationDescriptor:
    """One evidence-local, role-scoped descriptor for a two-party structured record.

    Never a second observation or an entity: `observation_id` is the same
    real, single combined CDR/finance record both parties came from.
    `descriptor_id` is what makes the two roles independently comparable
    in `retrieval.py` despite sharing one `observation_id` -- deterministic
    from `(case_id, observation_id, role, normalized value)`, per this
    module's own `_party_key` precedent for the motif adapter.

    `vehicle_registration` (Gap-Closure follow-up) is the record's own
    `vehicle_context` field, structurally present on the same CDR/finance
    record both parties came from -- merged into both parties' identifiers
    (not a role-scoped field itself), since both were, by the record's own
    authored semantics, associated with that vehicle. `_IDENTIFIER_TYPES`
    already trusts a vehicle-plate-shaped value regardless of whether it
    arrived via OCR'd document text or a structured field -- widening which
    modality supplies that existing identifier kind, not a new one.
    """
    normalized = normalise_identifier(identifier_kind, raw_value) or raw_value
    identifiers = {identifier_kind: raw_value}
    if vehicle_registration is not None:
        identifiers["vehicle_registration"] = vehicle_registration
    return ObservationDescriptor(
        case_id=observation.case_id,
        observation_id=observation.observation_id,
        descriptor_id=deterministic_uuid(
            "phase5_party_descriptor",
            str(observation.case_id),
            str(observation.observation_id),
            role,
            normalized,
        ),
        evidence_id=observation.evidence_id,
        source_locator_reference=_locator_reference(observation),
        participant_role=role,
        identifiers=identifiers,
        event_start=event_start,
        event_end=event_end,
    )


def descriptors_from_observation(observation: ObservationV1) -> tuple[ObservationDescriptor, ...]:
    """Every retrieval-eligible descriptor one canonical observation supplies.

    For most observation types this is exactly `descriptor_from_observation`'s
    single result, wrapped in a 0- or 1-tuple -- unchanged behavior, same
    producer-validation gates, same documented mapping scope. The one
    difference: a two-party structured record (`cdr_call_record`'s caller/
    callee, `financial_transaction_record`'s sender/receiver) returns BOTH
    parties as independent, role-scoped descriptors instead of only the
    primary one -- closing the Phase 5A limitation `descriptor_from_
    observation`'s own docstring documents.

    Social/chat and audio are deliberately unchanged here: a chat message
    only ever carries an explicit, source-supplied sender (no second
    "recipient" role the source data actually supplies), and a diarization
    speaker label is never promoted into a party descriptor at all (see
    the module docstring's "Deliberately NOT mapped" list) -- there is
    nothing to expand for either family.
    """
    two_party_fields = (
        _TWO_PARTY_FIELDS
        if observation.observation_type == "cdr_call_record"
        else _TWO_PARTY_FIELDS_FINANCE
        if observation.observation_type == "financial_transaction_record"
        else None
    )
    if two_party_fields is None:
        single = descriptor_from_observation(observation)
        return (single,) if single is not None else ()

    # The same producer-validation gates `descriptor_from_observation`
    # enforces apply identically here -- neither party of a rejected
    # structured record is correlation-ready.
    validation = observation.attributes.get("communication_signal_validation")
    if isinstance(validation, dict) and validation.get("correlation_ready") is not True:
        return ()
    if _is_structured_validation_rejected(observation):
        return ()

    # Gap-Closure follow-up: the record's own `vehicle_context` field,
    # shared by both parties (not role-scoped itself) -- merged into each
    # party descriptor below, not returned as a third, separate descriptor.
    vehicle_context = observation.attributes.get("vehicle_context")
    vehicle_registration = (
        vehicle_context if isinstance(vehicle_context, str) and vehicle_context.strip() else None
    )

    event_start, event_end = _event_time_of(observation)
    descriptors: list[ObservationDescriptor] = []
    for _observation_type, attribute_key, identifier_kind, role in two_party_fields:
        raw_value = observation.attributes.get(attribute_key)
        if isinstance(raw_value, str) and raw_value.strip():
            descriptors.append(
                _party_descriptor(
                    observation,
                    role=role,
                    identifier_kind=identifier_kind,
                    raw_value=raw_value,
                    event_start=event_start,
                    event_end=event_end,
                    vehicle_registration=vehicle_registration,
                )
            )
    return tuple(descriptors)


def _locator_reference(observation: ObservationV1) -> str:
    """A short, bounded, non-sensitive locator string -- never raw source content.

    Deterministic and stable for the same observation: derived from the
    fields `SourceLocator` itself already treats as non-secret positional
    metadata (page/span/message_id/json_path/frame/time), never from
    attribute values that could carry message/document text.
    """
    locator = observation.source_locator
    parts = [
        f"page={locator.page}" if locator.page is not None else None,
        f"span={locator.span_start}:{locator.span_end}" if locator.span_start is not None else None,
        f"row={locator.row}" if locator.row is not None else None,
        f"json_path={locator.json_path}" if locator.json_path else None,
        f"message_id={locator.message_id}" if locator.message_id else None,
        f"frame={locator.frame_number}" if locator.frame_number is not None else None,
        f"time={locator.time_start_ms}:{locator.time_end_ms}"
        if locator.time_start_ms is not None
        else None,
    ]
    reference = ",".join(part for part in parts if part is not None)
    return reference or f"observation={observation.observation_id}"


async def fetch_case_observations(engine: AsyncEngine, case_id: UUID) -> list[ObservationV1]:
    """Read every canonical observation persisted for one case.

    Read-only; never writes. Scoped by `case_id` at the SQL level -- the same
    case-isolation guarantee `integration_repository.py`'s own re-read of
    referenced observations already relies on.
    """
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(worker_observations_table.c.canonical_payload).where(
                    worker_observations_table.c.case_id == case_id
                )
            )
        ).mappings()
        return [ObservationV1.model_validate(row["canonical_payload"]) for row in rows]


def _party_key(case_id: UUID, kind: str, normalized_value: str) -> UUID:
    """A deterministic, internal-only correlation key for the temporal motif.

    `normalized_value` must already be the output of `normalise_identifier`
    (or an equivalent normalization) -- this function never normalizes
    itself, so two different raw spellings of the same identifier (e.g.
    "+91 98765 43210" and "9876543210") are the caller's responsibility to
    collapse first, exactly as `retrieval.py`'s own exact-blocking compares
    normalized values, never raw ones.

    Never an entity, never persisted, never exposed in an API response or a
    Neo4j property -- purely a same-run grouping key so
    `analytics.detect_communication_transfer_movement_motifs` can recognize
    "the same normalized identifier appears on both edges" without this
    adapter fabricating or merging any identity.
    """
    return deterministic_uuid("phase5_motif_party_key", str(case_id), kind, normalized_value)


def _phone_party_key(case_id: UUID, value: str) -> UUID | None:
    normalized = normalise_identifier("phone", value)
    return _party_key(case_id, "phone", normalized) if normalized is not None else None


def _account_party_key(case_id: UUID, value: str) -> UUID:
    """A transaction party's key -- "phone" kind when the account string
    itself is phone-shaped (e.g. a UPI/mobile-wallet account settled against
    a registered mobile number), else "account".

    This is not a guess or a cross-kind identity assertion: it applies the
    exact same phone-format rule `retrieval.normalise_identifier` already
    uses for genuine phone-number observations to a value that independently
    satisfies it, so a call and a transfer that legitimately share one
    mobile number (in any raw spelling) can be recognized as the same motif
    participant. A bank account number that doesn't look like a phone
    number is never reinterpreted -- it stays keyed as "account".
    """
    phone_key = _phone_party_key(case_id, value)
    if phone_key is not None:
        return phone_key
    normalized_account = normalise_identifier("account", value) or value.casefold()
    return _party_key(case_id, "account", normalized_account)


def _call_or_transfer_edge(observation: ObservationV1) -> GraphEdgeSnapshot | None:
    if _is_structured_validation_rejected(observation):
        return None
    if observation.observation_type == "cdr_call_record":
        caller = observation.attributes.get("caller_number")
        callee = observation.attributes.get("callee_number")
        if not isinstance(caller, str) or not caller.strip():
            return None
        left = _phone_party_key(observation.case_id, caller)
        if left is None:
            return None
        event_start, event_end = _event_time_of(observation)
        if event_start is None or event_end is None:
            return None
        right = (
            _phone_party_key(observation.case_id, callee)
            if isinstance(callee, str) and callee.strip()
            else None
        ) or left
        return GraphEdgeSnapshot(
            case_id=observation.case_id,
            left_id=left,
            right_id=right,
            event_id=observation.observation_id,
            evidence_observation_ids=(observation.observation_id,),
            event_kind="cdr_call",
            event_start=event_start,
            event_end=event_end,
        )
    if observation.observation_type == "financial_transaction_record":
        sender = observation.attributes.get("sender_account")
        receiver = observation.attributes.get("receiver_account")
        if not isinstance(sender, str) or not sender.strip():
            return None
        event_start, event_end = _event_time_of(observation)
        if event_start is None or event_end is None:
            return None
        left = _account_party_key(observation.case_id, sender)
        right = (
            _account_party_key(observation.case_id, receiver)
            if isinstance(receiver, str) and receiver.strip()
            else left
        )
        return GraphEdgeSnapshot(
            case_id=observation.case_id,
            left_id=left,
            right_id=right,
            event_id=observation.observation_id,
            evidence_observation_ids=(observation.observation_id,),
            event_kind="financial_transaction",
            event_start=event_start,
            event_end=event_end,
        )
    return None


def build_motif_edges(observations: list[ObservationV1]) -> tuple[GraphEdgeSnapshot, ...]:
    """Build `GraphEdgeSnapshot`s for `analytics.detect_communication_transfer_movement_motifs`.

    Call and transfer edges are built directly from CDR/finance canonical
    observations, keyed on their own normalized phone/account identifiers
    (see `_party_key` -- an internal grouping key, never an identity).

    `meeting_candidate` media observations are deliberately handled
    differently, not via `descriptor_from_observation`'s generic mapping:
    a media sighting carries no phone/account/vehicle identifier comparable
    to a CDR/finance party, so fabricating one would misrepresent what the
    evidence actually supports. Instead, a `meeting_candidate` becomes a
    "movement" edge *only* when its own `contributing_observation_ids`
    (already an explicit, reviewed evidence citation --
    `graph.mapping.py`'s existing `meeting_candidate` handling) names a
    call/transfer observation this same function already turned into an
    edge -- reusing that edge's own party key, since the meeting candidate
    itself is the evidence that ties the two together. A meeting candidate
    citing no known call/transfer observation is skipped, not guessed at:
    this is a documented Phase 5A limitation (see
    docs/qa/known-limitations.md), not a defect -- real cross-modal chains
    need a shared identifier the media pipeline does not yet extract.
    """
    edges: list[GraphEdgeSnapshot] = []
    edge_by_observation_id: dict[UUID, GraphEdgeSnapshot] = {}
    for observation in observations:
        edge = _call_or_transfer_edge(observation)
        if edge is not None:
            edges.append(edge)
            edge_by_observation_id[observation.observation_id] = edge

    for observation in observations:
        if observation.observation_type != "meeting_candidate":
            continue
        contributing = observation.attributes.get("contributing_observation_ids")
        if not isinstance(contributing, list):
            continue
        event_start, event_end = _event_time_of(observation)
        if event_start is None or event_end is None:
            continue
        linked_edge = next(
            (
                edge_by_observation_id[UUID(value)]
                for value in contributing
                if isinstance(value, str) and UUID(value) in edge_by_observation_id
            ),
            None,
        )
        if linked_edge is None:
            continue
        edges.append(
            GraphEdgeSnapshot(
                case_id=observation.case_id,
                left_id=linked_edge.left_id,
                right_id=linked_edge.right_id,
                event_id=observation.observation_id,
                evidence_observation_ids=(observation.observation_id,),
                event_kind="meeting",
                event_start=event_start,
                event_end=event_end,
            )
        )
    return tuple(edges)


def edges_from_candidate_links(
    links: list[CandidateLinkRecord],
) -> tuple[GraphEdgeSnapshot, ...]:
    """Build the case-scoped review-candidate graph PageRank/betweenness/WCC/Leiden run over.

    Nodes are observation IDs; edges are already-persisted, already-scored
    `correlation_candidate_links` rows -- never a raw, unreviewed observation
    pair, and never an `Entity`/person edge. This is deliberately the
    *candidate* graph, not a hypothetical identity graph: centrality/
    community results describe which candidate correlations are more
    structurally central within a case's own review queue, which is a
    review-priority signal, not a claim about a real-world network (see
    `docs/architecture/phase-5-graph-intelligence.md`).
    """
    if len({link.case_id for link in links}) > 1:
        raise ValueError("candidate-link edge snapshot spans multiple cases")
    return tuple(
        GraphEdgeSnapshot(
            case_id=link.case_id,
            left_id=link.left_observation_id,
            right_id=link.right_observation_id,
            event_id=link.candidate_link_id,
            evidence_observation_ids=(link.left_observation_id, link.right_observation_id),
            event_kind="candidate_link",
        )
        for link in links
    )


__all__ = [
    "build_motif_edges",
    "descriptor_from_observation",
    "descriptors_from_observation",
    "edges_from_candidate_links",
    "fetch_case_observations",
]
