"""Versioned, non-sensitive synthetic benchmark for the Phase 5 rules baseline.

See `docs/decisions/ADR-006-phase-5-rules-baseline.md`'s "measurement and
final freeze" addendum and `docs/architecture/phase-5-integration.md`'s
rules-baseline section. Every observation here is invented -- no private
police data, no Operation Nightfall data, nothing derived from a real
case. This module only builds inputs and states ground truth; the actual
metrics computation lives in `tests/unit/graph/test_phase5_rules_benchmark.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.common import Extractor, SourceLocator
from app.contracts.observation import ExtractedEntityMention, ObservationV1
from app.modules.graph.intelligence.models import ObservationDescriptor

BENCHMARK_VERSION = "phase5_rules_benchmark_v1"

_EXTRACTOR = Extractor(
    name="benchmark-fixture", version="1.0.0", config_hash="h", model_version="n/a"
)
_T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _observation(
    *,
    case_id: UUID,
    observation_type: str,
    attributes: dict[str, object] | None = None,
    extracted_entities: list[ExtractedEntityMention] | None = None,
    locator: SourceLocator | None = None,
    event_time: datetime | None = None,
) -> ObservationV1:
    return ObservationV1(
        observation_id=uuid4(),
        case_id=case_id,
        evidence_id=uuid4(),
        observation_type=observation_type,
        extracted_entities=extracted_entities or [],
        event_time=event_time,
        attributes=attributes or {},
        extraction_confidence=0.9,
        source_locator=locator or SourceLocator(page=1, span_start=0, span_end=5),
        extractor=_EXTRACTOR,
        created_at=datetime.now(UTC),
    )


@dataclass(frozen=True)
class BenchmarkCase:
    """One case's synthetic observations plus its labeled ground truth.

    `positive_pairs` names every unordered `(observation_id, observation_id)`
    pair a correctly-behaving rules baseline must surface as *some*
    candidate (regardless of score) -- true signal genuinely present in
    the fixture. `suppressed_pairs` names pairs that must NEVER surface as
    a candidate even though they might look related at a glance (same
    two-party record, or a case-isolation boundary) -- a real regression
    if the engine ever produces one. Everything else is an implicit true
    negative: any pair not listed in either set must not produce a
    candidate, or Precision@K below would already have caught it.
    """

    case_id: UUID
    observations: tuple[ObservationV1, ...]
    positive_pairs: frozenset[frozenset[UUID]]
    contradiction_pairs: frozenset[frozenset[UUID]]
    label: str


def build_benchmark() -> BenchmarkCase:
    """One self-contained case exercising every required benchmark scenario.

    - Two-party CDR cross-blocking (O1's callee == O2's caller, different records).
    - Two-party finance cross-blocking (O3's receiver == O4's sender, different records).
    - Cross-modal exact match (a CDR caller number == a FIR document phone mention).
    - Weak alias/transliteration signal (two chat messages, one transliterated).
    - Clean negatives (O8/O9: no relationship to anything else in the set).
    - Same-event suppression (O1's own caller/callee must never pair with each
      other; likewise O3's own sender/receiver).
    """
    case_id = uuid4()

    cdr_first = _observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919876500001",
            "callee_number": "+919876500002",
            "timestamp": _T0.isoformat(),
        },
    )
    cdr_second = _observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919876500002",
            "callee_number": "+919876500003",
            "timestamp": _T0.isoformat(),
        },
    )
    finance_first = _observation(
        case_id=case_id,
        observation_type="financial_transaction_record",
        attributes={
            "sender_account": "ACC-SEND-001",
            "receiver_account": "ACC-MID-002",
            "timestamp": _T0.isoformat(),
        },
    )
    finance_second = _observation(
        case_id=case_id,
        observation_type="financial_transaction_record",
        attributes={
            "sender_account": "ACC-MID-002",
            "receiver_account": "ACC-END-003",
            "timestamp": _T0.isoformat(),
        },
    )
    document_phone = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="+919876500001", entity_type_hint="phone_number")
        ],
    )
    chat_original = _observation(
        case_id=case_id,
        observation_type="chat_message",
        attributes={
            "platform": "whatsapp",
            "sender": "Rahul",
            "sender_transliteration_candidates": [
                {"original_text": "राहुल", "candidates": ["raahula"]}
            ],
        },
    )
    chat_transliterated = _observation(
        case_id=case_id, observation_type="chat_message", attributes={"sender": "raahula"}
    )
    negative_cdr = _observation(
        case_id=case_id,
        observation_type="cdr_call_record",
        attributes={
            "caller_number": "+919000000091",
            "callee_number": "+919000000092",
            "timestamp": _T0.isoformat(),
        },
    )
    negative_document = _observation(
        case_id=case_id,
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text="+919000000099", entity_type_hint="phone_number")
        ],
    )

    observations = (
        cdr_first,
        cdr_second,
        finance_first,
        finance_second,
        document_phone,
        chat_original,
        chat_transliterated,
        negative_cdr,
        negative_document,
    )
    positive_pairs = frozenset(
        {
            frozenset({cdr_first.observation_id, cdr_second.observation_id}),
            frozenset({cdr_first.observation_id, document_phone.observation_id}),
            frozenset({finance_first.observation_id, finance_second.observation_id}),
            frozenset({chat_original.observation_id, chat_transliterated.observation_id}),
        }
    )
    return BenchmarkCase(
        case_id=case_id,
        observations=observations,
        positive_pairs=positive_pairs,
        contradiction_pairs=frozenset(),
        label=BENCHMARK_VERSION,
    )


def build_contradiction_probe() -> tuple[ObservationDescriptor, ObservationDescriptor, UUID, UUID]:
    """A direct-construction probe for match+contradiction co-occurrence.

    No current producer emits one descriptor carrying both an alias and an
    identifier (`sourcing.py`'s mapping keeps document/CDR/finance
    identifiers and chat/social aliases in disjoint observation types --
    see its module docstring). To exercise the scoring engine's own
    contradiction handling (matches on one signal, conflicts on another)
    this probe constructs `ObservationDescriptor`s directly rather than
    through a real producer mapping -- a synthetic engine-behavior check,
    not a claim about what any producer's output looks like today.
    """
    case_id = uuid4()
    left_observation_id = uuid4()
    right_observation_id = uuid4()
    left = ObservationDescriptor(
        case_id=case_id,
        observation_id=left_observation_id,
        evidence_id=uuid4(),
        source_locator_reference="benchmark=contradiction-left",
        identifiers={"phone": "+919876500010"},
        aliases=("Amit Verma",),
    )
    right = ObservationDescriptor(
        case_id=case_id,
        observation_id=right_observation_id,
        evidence_id=uuid4(),
        source_locator_reference="benchmark=contradiction-right",
        identifiers={"phone": "+919876500099"},  # deliberately conflicting
        aliases=("Amit Verma",),  # deliberately matching
    )
    return left, right, left_observation_id, right_observation_id


def build_cross_case_attempt() -> tuple[ObservationV1, ObservationV1]:
    """Two observations in *different* cases that would collide if case
    scoping were ever bypassed -- must never be compared together."""
    shared_number = "+919876500077"
    first = _observation(
        case_id=uuid4(),
        observation_type="cdr_call_record",
        attributes={
            "caller_number": shared_number,
            "callee_number": "+919000000001",
            "timestamp": _T0.isoformat(),
        },
    )
    second = _observation(
        case_id=uuid4(),
        observation_type="phone_number_mention",
        extracted_entities=[
            ExtractedEntityMention(text=shared_number, entity_type_hint="phone_number")
        ],
    )
    return first, second


__all__ = [
    "BENCHMARK_VERSION",
    "BenchmarkCase",
    "build_benchmark",
    "build_contradiction_probe",
    "build_cross_case_attempt",
]
