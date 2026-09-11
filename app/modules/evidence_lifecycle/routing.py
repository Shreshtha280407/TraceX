"""Central source-type -> accepted content types and processor routing registry.

Deliberately small and explicit, not derived from the downstream processing
modules' own profile registries: this module must not import or take
ownership of Jasraj's (`structured_processing`), Sarthak's
(`communication_processing`), or Gaurav's (`media_processing`) parser
profiles. It only makes the coarse, unambiguous routing decision needed to
construct a valid `WorkerJobV1` at ingestion time -- content-specific
classification (e.g. choosing a FIR-report profile vs. a generic tabular
fallback) remains each processing module's own job. See
`docs/architecture/evidence-lifecycle.md`.

`source_type` is always declared explicitly by the uploading client (a
required form field), never inferred from `content_type` or file content --
consistent with this codebase's "never guess ambiguous input" rule applied
everywhere else (FIR extraction, CDR/financial normalization, audio
routing).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.evidence import SourceType


@dataclass(frozen=True)
class ProcessorRoute:
    processor_name: str
    processor_version: str


#: Content types accepted per declared `source_type`. `SourceType.OTHER` is
#: intentionally absent: it is the contract's escape hatch for an
#: unanticipated modality, but no processing module is registered to
#: consume it yet in this phase -- see `docs/qa/known-limitations.md`.
SOURCE_TYPE_CONTENT_TYPES: dict[SourceType, frozenset[str]] = {
    SourceType.DOCUMENT: frozenset(
        {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        }
    ),
    SourceType.CDR: frozenset(
        {
            "text/csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
        }
    ),
    SourceType.FINANCIAL: frozenset(
        {
            "text/csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
        }
    ),
    SourceType.AUDIO: frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
    SourceType.CHAT: frozenset({"application/json"}),
    SourceType.IMAGE: frozenset({"image/jpeg", "image/png"}),
    SourceType.VIDEO: frozenset({"video/mp4", "video/quicktime", "video/x-matroska"}),
    # Phase 2.3: general CSV/XLSX/JSON evidence that isn't specifically CDR
    # or financial shaped. Deliberately disjoint content-type sets from
    # each other (never both CSV/XLSX *and* JSON under one source type) so
    # one declared source type still maps unambiguously to exactly one
    # processor -- see docs/architecture/phase-2-decisions.md.
    SourceType.STRUCTURED_TABULAR: frozenset(
        {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    SourceType.STRUCTURED_JSON: frozenset({"application/json"}),
}

#: `source_type` -> the processor a durable `WorkerJobV1` is routed to. One
#: canonical processor per source type, matching the name/version each
#: downstream module's own worker already recognizes as its default/
#: generic profile. Fine-grained profile selection within a source type
#: (e.g. FIR-report vs. a fallback tabular profile) is deliberately NOT
#: decided here -- that classification happens inside the processing
#: module itself once it reads the bytes.
ROUTING: dict[SourceType, ProcessorRoute] = {
    SourceType.DOCUMENT: ProcessorRoute("fir_report_text_v1", "1.0.0"),
    SourceType.CDR: ProcessorRoute("cdr_generic_v1", "1.0.0"),
    SourceType.FINANCIAL: ProcessorRoute("financial_transaction_generic_v1", "1.0.0"),
    SourceType.AUDIO: ProcessorRoute("audio_metadata_v1", "1.0.0"),
    SourceType.CHAT: ProcessorRoute("generic_social_json_v1", "1.0.0"),
    SourceType.VIDEO: ProcessorRoute("media_metadata_v1", "1.0.0"),
    SourceType.IMAGE: ProcessorRoute("media_metadata_v1", "1.0.0"),
    # Phase 2.3: reaches structured_processing's existing generic fallback
    # profiles (previously only constructible via a direct/test job, never
    # a real upload -- see docs/architecture/structured-processing-worker.md).
    SourceType.STRUCTURED_TABULAR: ProcessorRoute("generic_tabular_v1", "1.0.0"),
    SourceType.STRUCTURED_JSON: ProcessorRoute("generic_json_v1", "1.0.0"),
}


def accepted_content_types(source_type: SourceType) -> frozenset[str]:
    return SOURCE_TYPE_CONTENT_TYPES.get(source_type, frozenset())


def route_for(source_type: SourceType) -> ProcessorRoute | None:
    return ROUTING.get(source_type)
