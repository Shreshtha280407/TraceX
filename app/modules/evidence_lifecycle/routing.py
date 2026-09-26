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

The public Evidence Library uses :func:`detect_source` to make the initial
routing decision from inspected bytes.  A caller never gets to choose a
processor.  The older explicit-source endpoint shape remains accepted only
for backwards-compatible clients; it is checked against the detected result
by the service and can never override it.
"""

from __future__ import annotations

import json
import re
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
    SourceType.AUDIO: frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg"}),
    SourceType.CHAT: frozenset({"application/json"}),
    SourceType.IMAGE: frozenset({"image/jpeg", "image/png"}),
    SourceType.VIDEO: frozenset(
        {"video/mp4", "video/quicktime", "video/x-matroska", "video/x-msvideo"}
    ),
    # Phase 2.3: general CSV/XLSX/JSON evidence that isn't specifically CDR
    # or financial shaped. Deliberately disjoint content-type sets from
    # each other (never both CSV/XLSX *and* JSON under one source type) so
    # one declared source type still maps unambiguously to exactly one
    # processor -- see docs/architecture/phase-2-decisions.md.
    SourceType.STRUCTURED_TABULAR: frozenset(
        {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    SourceType.STRUCTURED_JSON: frozenset({"application/json"}),
    # Phase 2 routing fix: `communication_processing`'s five other profiles,
    # fully implemented since Phase 1 but previously unreachable via a real
    # upload -- see the `SourceType` docstring. `AUDIO`/`CHAT` above are
    # unchanged; these are new, disjoint source types, not new content
    # types layered onto the existing ones.
    SourceType.AUDIO_TRANSCRIPT: frozenset({"application/json"}),
    SourceType.AUDIO_DIARIZATION: frozenset({"application/json"}),
    SourceType.WHATSAPP_CHAT: frozenset({"text/plain"}),
    SourceType.TELEGRAM_CHAT: frozenset({"application/json"}),
    SourceType.INSTAGRAM_CHAT: frozenset({"application/json"}),
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
    # Phase 2 closeout: routed to media_processing's real local detection
    # pipeline (`media_detection_v1`), not the metadata-only fallback --
    # `_process` always emits the metadata observation first regardless of
    # which processor claimed the job, so this is strictly additive real
    # intelligence, never a loss of the prior metadata-only behavior. See
    # docs/architecture/phase-2-decisions.md's "Real local media inference
    # closeout" and docs/architecture/media-processing-worker.md.
    # `media_metadata_v1` remains fully supported by that worker for a
    # directly-constructed/legacy job; it is simply no longer what a real
    # upload is routed to.
    SourceType.VIDEO: ProcessorRoute("media_detection_v1", "1.0.0"),
    SourceType.IMAGE: ProcessorRoute("media_detection_v1", "1.0.0"),
    # Phase 2.3: reaches structured_processing's existing generic fallback
    # profiles (previously only constructible via a direct/test job, never
    # a real upload -- see docs/architecture/structured-processing-worker.md).
    SourceType.STRUCTURED_TABULAR: ProcessorRoute("generic_tabular_v1", "1.0.0"),
    SourceType.STRUCTURED_JSON: ProcessorRoute("generic_json_v1", "1.0.0"),
    # Phase 2 routing fix: reaches communication_processing's existing
    # (Phase 1, unit-tested, previously upload-unreachable) profiles -- see
    # docs/architecture/audio-social-and-communication-processing-v1.md and
    # docs/architecture/phase-2-decisions.md.
    SourceType.AUDIO_TRANSCRIPT: ProcessorRoute("transcript_import_v1", "1.0.0"),
    SourceType.AUDIO_DIARIZATION: ProcessorRoute("diarization_import_v1", "1.0.0"),
    SourceType.WHATSAPP_CHAT: ProcessorRoute("whatsapp_export_v1", "1.0.0"),
    SourceType.TELEGRAM_CHAT: ProcessorRoute("telegram_export_v1", "1.0.0"),
    SourceType.INSTAGRAM_CHAT: ProcessorRoute("instagram_export_v1", "1.0.0"),
}


def accepted_content_types(source_type: SourceType) -> frozenset[str]:
    return SOURCE_TYPE_CONTENT_TYPES.get(source_type, frozenset())


def route_for(source_type: SourceType) -> ProcessorRoute | None:
    return ROUTING.get(source_type)


@dataclass(frozen=True)
class DetectedEvidenceType:
    """A conservative byte-inspection result used before an object is stored.

    This deliberately recognises only formats the worker registry can really
    consume.  Unknown bytes are rejected before object storage and before a
    durable job is created; a filename suffix or browser supplied MIME type
    never makes an otherwise unsupported file acceptable.
    """

    source_type: SourceType
    content_type: str


def detect_source(
    *, sample: bytes, filename: str, zip_members: set[str] | None = None
) -> DetectedEvidenceType | None:
    """Detect a supported evidence family from a bounded inspected prefix.

    Text/CSV/JSON detection is intentionally deterministic and schema based.
    Ambiguous tabular/JSON input uses the existing generic structured route,
    rather than pretending it is a CDR, financial record, or chat export.
    """
    lower_name = filename.lower()
    if sample.startswith(b"%PDF-"):
        return DetectedEvidenceType(SourceType.DOCUMENT, "application/pdf")
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedEvidenceType(SourceType.IMAGE, "image/png")
    if sample.startswith(b"\xff\xd8\xff"):
        return DetectedEvidenceType(SourceType.IMAGE, "image/jpeg")
    if sample.startswith(b"RIFF") and sample[8:12] == b"WAVE":
        return DetectedEvidenceType(SourceType.AUDIO, "audio/wav")
    if sample.startswith(b"RIFF") and sample[8:12] == b"AVI ":
        return DetectedEvidenceType(SourceType.VIDEO, "video/x-msvideo")
    if sample.startswith(b"ID3") or sample[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return DetectedEvidenceType(SourceType.AUDIO, "audio/mpeg")
    if len(sample) >= 12 and sample[4:8] == b"ftyp":
        return DetectedEvidenceType(SourceType.VIDEO, "video/mp4")
    if sample.startswith(b"PK\x03\x04"):
        members = zip_members or set()
        if any(name.startswith("word/") for name in members):
            return DetectedEvidenceType(
                SourceType.DOCUMENT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        if any(name.startswith("xl/") for name in members):
            return DetectedEvidenceType(
                SourceType.STRUCTURED_TABULAR,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        return None

    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        return None
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            # A larger JSON file can be valid but not fit in the prefix. It
            # remains a generic JSON document only when its opening shape is
            # recognisable; malformed JSON is rejected by the worker later.
            return DetectedEvidenceType(SourceType.STRUCTURED_JSON, "application/json")
        if isinstance(payload, dict):
            keys = {str(key).lower() for key in payload}
            records = payload.get("records")
            if "messages" in keys or "records" in keys and isinstance(records, list):
                return DetectedEvidenceType(SourceType.CHAT, "application/json")
            if {"caller_number", "callee_number"} <= keys or {"source_id", "target_id"} <= keys:
                return DetectedEvidenceType(SourceType.CDR, "application/json")
            if {"from_account", "to_account"} <= keys:
                return DetectedEvidenceType(SourceType.FINANCIAL, "application/json")
        return DetectedEvidenceType(SourceType.STRUCTURED_JSON, "application/json")

    header = next((line.strip().lower() for line in text.splitlines() if line.strip()), "")
    columns = {part.strip().strip('"') for part in header.split(",")}
    if {"source_id", "target_id"} <= columns or {"caller_number", "callee_number"} <= columns:
        return DetectedEvidenceType(SourceType.CDR, "text/csv")
    if {"from_account", "to_account"} <= columns or {"amount", "currency"} <= columns:
        return DetectedEvidenceType(SourceType.FINANCIAL, "text/csv")
    if len(columns) > 1 and all(re.match(r"^[\w .-]+$", column) for column in columns):
        return DetectedEvidenceType(SourceType.STRUCTURED_TABULAR, "text/csv")
    # A WhatsApp text export has a stable, content-derived line shape.  This
    # check intentionally precedes the generic plaintext-document fallback:
    # it does not rely on a caller's MIME field or filename to select a
    # communications processor.
    if re.search(
        r"(?m)^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4},\s+\d{1,2}:\d{2}\s+-\s+[^:]+:",
        text,
    ):
        return DetectedEvidenceType(SourceType.WHATSAPP_CHAT, "text/plain")
    # Plain UTF-8 text is a supported document input.  It is intentionally
    # not reclassified as a chat export merely because its name says so.
    if lower_name.endswith((".txt", ".md", ".log")) or len(stripped) > 0:
        return DetectedEvidenceType(SourceType.DOCUMENT, "text/plain")
    return None
