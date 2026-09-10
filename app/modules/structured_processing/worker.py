"""The structured-processing worker entry point: `WorkerJobV1` in, `WorkerResultV1` out.

`process_job` is the only function later-phase orchestration code needs to
call. It never touches PostgreSQL, Neo4j, Redis, or MinIO — bytes come in
through a `SourceResolver`, and everything it returns is either a
canonical `ObservationV1` or a safe, structured `WorkerError`.

Only `ProcessingError` is caught here and turned into a `FAILED` result;
any other exception is a programming bug and is allowed to propagate
rather than being silently repackaged as a plausible-looking failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.contracts.evidence import EvidenceRecordV1
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.modules.structured_processing.document.classifier import ContentKind, classify
from app.modules.structured_processing.document.docx import extract_docx
from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.ocr_routing import route_pdf
from app.modules.structured_processing.document.pdf import extract_pdf
from app.modules.structured_processing.document.txt import extract_txt
from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.limits import MAX_INPUT_BYTES, MAX_MENTION_TEXT_LENGTH
from app.modules.structured_processing.models import (
    ParserProfile,
    RawMention,
    RawRecord,
    SourceResolver,
)
from app.modules.structured_processing.provenance import (
    CONFIDENCE_STRUCTURED_COMPLETE,
    mention_to_observation,
)
from app.modules.structured_processing.structured.cdr import normalize_cdr_records
from app.modules.structured_processing.structured.csv_parser import parse_csv
from app.modules.structured_processing.structured.finance import normalize_financial_records
from app.modules.structured_processing.structured.json_parser import (
    parse_json_records,
    traverse_json_scalars,
)
from app.modules.structured_processing.structured.profiles import (
    CDR_GENERIC_V1,
    FINANCIAL_TRANSACTION_GENERIC_V1,
    FIR_REPORT_TEXT_V1,
    GENERIC_JSON_V1,
    GENERIC_TABULAR_V1,
    get_profile,
)
from app.modules.structured_processing.structured.xlsx_parser import parse_xlsx


def process_job(
    job: WorkerJobV1, evidence: EvidenceRecordV1, resolver: SourceResolver
) -> WorkerResultV1:
    """Process one evidence source and return a canonical `WorkerResultV1`.

    `evidence.content_type` is classified against the Phase 1 supported
    format set first (so a fundamentally unsupported format is reported as
    such); `job.processor_name` then selects the parser profile
    explicitly, cross-checked against that profile's accepted content
    types before any parsing happens.
    """
    completed_at = datetime.now(UTC)
    try:
        kind = classify(evidence.content_type, evidence.original_filename)
        profile = get_profile(job.processor_name)
        if evidence.content_type not in profile.accepted_content_types:
            raise ProcessingError(
                ErrorCode.UNSUPPORTED_PARSER_PROFILE,
                f"profile '{profile.name}' does not accept content type '{evidence.content_type}'",
            )

        data = resolver.read_bytes(job.input_object_uri)
        if len(data) > MAX_INPUT_BYTES:
            raise ProcessingError(
                ErrorCode.INPUT_LIMIT_EXCEEDED, f"input exceeds the {MAX_INPUT_BYTES}-byte limit"
            )

        if kind is ContentKind.PDF:
            return _process_pdf(job, profile, data, completed_at)

        if kind is ContentKind.DOCX or kind is ContentKind.TXT:
            segments = extract_docx(data) if kind is ContentKind.DOCX else extract_txt(data)
            mentions = extract_fir_mentions(segments)
            return _succeed(job, profile, mentions, completed_at)

        if kind is ContentKind.JSON and profile.name == GENERIC_JSON_V1.name:
            mentions = _generic_json_mentions(data)
            return _succeed(job, profile, mentions, completed_at)

        records = _load_records(kind, data)
        mentions = _normalize_records(profile, records)
        return _succeed(job, profile, mentions, completed_at)

    except ProcessingError as exc:
        return _fail(job, exc, completed_at)


def _load_records(kind: ContentKind, data: bytes) -> list[RawRecord]:
    if kind is ContentKind.CSV:
        return parse_csv(data)
    if kind is ContentKind.XLSX:
        return parse_xlsx(data)
    if kind is ContentKind.JSON:
        return parse_json_records(data)
    raise ProcessingError(
        ErrorCode.UNSUPPORTED_PARSER_PROFILE, f"content kind '{kind}' has no record-based profile"
    )


def _normalize_records(profile: ParserProfile, records: list[RawRecord]) -> list[RawMention]:
    if profile.name == CDR_GENERIC_V1.name:
        return normalize_cdr_records(records)
    if profile.name == FINANCIAL_TRANSACTION_GENERIC_V1.name:
        return normalize_financial_records(records)
    if profile.name == GENERIC_TABULAR_V1.name:
        return _generic_tabular_mentions(records)
    raise ProcessingError(
        ErrorCode.UNSUPPORTED_PARSER_PROFILE,
        f"profile '{profile.name}' has no record-normalization logic",
    )


def _generic_tabular_mentions(records: list[RawRecord]) -> list[RawMention]:
    mentions: list[RawMention] = []
    for record in records:
        attrs: dict[str, JsonValue] = dict(record.values)
        mentions.append(
            RawMention(
                observation_type="tabular_record",
                text=f"tabular_record row {record.index}",
                locator=record.locator_for(None),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes=attrs,
            )
        )
    return mentions


def _generic_json_mentions(data: bytes) -> list[RawMention]:
    mentions: list[RawMention] = []
    for path, value in traverse_json_scalars(data):
        mentions.append(
            RawMention(
                observation_type="json_scalar_value",
                text=str(value)[:MAX_MENTION_TEXT_LENGTH],
                locator=SourceLocator(json_path=path),
                confidence=CONFIDENCE_STRUCTURED_COMPLETE,
                attributes={"value": value},
            )
        )
    return mentions


def _process_pdf(
    job: WorkerJobV1, profile: ParserProfile, data: bytes, completed_at: datetime
) -> WorkerResultV1:
    result = extract_pdf(data)
    routing = route_pdf(result)
    mentions = extract_fir_mentions(result.segments)
    observations = [
        mention_to_observation(
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            profile=profile,
            mention=mention,
            created_at=completed_at,
        )
        for mention in mentions
    ]
    status = WorkerStatus.DEFERRED if routing.requires_ocr else WorkerStatus.SUCCEEDED
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=status,
        observations=observations,
        derived_artifacts=[],
        checkpoint=routing.checkpoint,
        error=None,
        completed_at=completed_at,
    )


def _succeed(
    job: WorkerJobV1, profile: ParserProfile, mentions: list[RawMention], completed_at: datetime
) -> WorkerResultV1:
    observations = [
        mention_to_observation(
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            profile=profile,
            mention=mention,
            created_at=completed_at,
        )
        for mention in mentions
    ]
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=observations,
        derived_artifacts=[],
        checkpoint=None,
        error=None,
        completed_at=completed_at,
    )


def _fail(job: WorkerJobV1, exc: ProcessingError, completed_at: datetime) -> WorkerResultV1:
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=WorkerError(code=exc.code, message=exc.message, retryable=exc.retryable),
        completed_at=completed_at,
    )


__all__ = [
    "CDR_GENERIC_V1",
    "FINANCIAL_TRANSACTION_GENERIC_V1",
    "FIR_REPORT_TEXT_V1",
    "GENERIC_JSON_V1",
    "GENERIC_TABULAR_V1",
    "process_job",
]
