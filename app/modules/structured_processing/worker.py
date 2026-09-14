"""The structured-processing worker entry point: `WorkerJobV1` in, `WorkerResultV1` out.

`process_job` is the only function later-phase orchestration code needs to
call. It never touches PostgreSQL, Neo4j, Redis, or MinIO — bytes come in
through a `SourceResolver`, and everything it returns is either a
canonical `ObservationV1` or a safe, structured `WorkerError`.

Only `ProcessingError` is caught here and turned into a `FAILED` result;
any other exception is a programming bug and is allowed to propagate
rather than being silently repackaged as a plausible-looking failure.

`run_once`/`main` (bottom of this file) are the Phase 2 addition: a
one-shot CLI runner --

    uv run python -m app.modules.structured_processing.worker --once

-- that claims one compatible job through Nipun's internal worker API
(`client.py`), resolves its evidence via the injected
`input_resolver.WorkerInputResolver`, and submits a result. No daemon,
polling loop, or scheduler -- see `docs/architecture/
document-structured-processing.md`. Any `ProcessingError` raised while
processing is caught and turned into a `FAILED` result; a genuine bug
propagates uncaught out of `run_once`/`main` on purpose, exactly as this
module's own docstring already establishes -- the claimed job's lease
simply expires and becomes reclaimable rather than a fabricated result
being submitted for it.

Phase 3 (Jasraj) adds real document OCR/NER/relation extraction and real
CDR/finance micro-batch processing (`run_document_job_with_batches`/
`run_structured_batches_job`, below) -- `run_once` now submits these
processors' observations as one or more `ObservationBatchSubmissionV1`
micro-batches through Nipun's `/observations` endpoint (via `client.
submit_batch`), each with safe progress and transformation provenance,
*before* submitting its one terminal `WorkerResultV1` with `observations=
[]` (the observations were already delivered). `process_job` itself is
unchanged and still used directly for the fallback profiles
(`generic_tabular_v1`/`generic_json_v1`, owned by Nipun) and by every
existing test/integration caller that wants one synchronous, non-batched
result for a small input -- see `docs/architecture/phase-3-decisions.md`
for why both paths coexist.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from pydantic import JsonValue

from app.contracts.common import SourceLocator
from app.contracts.evidence import (
    EvidenceClassification,
    EvidenceProcessingStatus,
    EvidenceRecordV1,
)
from app.contracts.observation import ObservationV1
from app.contracts.observation_batch import TransformationProvenanceV1
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.core.config import Settings, get_settings
from app.modules.structured_processing.batching import (
    build_batch_submission,
    build_progress,
    build_transformation,
    deterministic_batch_id,
)
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.document.classifier import ContentKind, classify
from app.modules.structured_processing.document.docx import extract_docx
from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.ner import NerAdapter, NerMention
from app.modules.structured_processing.document.ner_fallback import (
    NER_FALLBACK_ADAPTER_NAME,
    NER_FALLBACK_ADAPTER_VERSION,
    DeterministicNerAdapter,
)
from app.modules.structured_processing.document.normalization import (
    NORMALIZATION_NAME,
    NORMALIZATION_VERSION,
    OffsetMap,
    normalization_config_hash,
    normalize_text,
)
from app.modules.structured_processing.document.ocr import (
    OCR_ENGINE_NAME,
    DocumentPageOcrEngine,
    OcrConfig,
    OcrPageResult,
    locate_bbox_for_span,
    ocr_config_hash,
)
from app.modules.structured_processing.document.ocr_routing import route_pdf
from app.modules.structured_processing.document.page_trust import page_trust_config_hash
from app.modules.structured_processing.document.pdf import extract_pdf, render_pdf_pages
from app.modules.structured_processing.document.relations import (
    RELATIONS_RULESET_VERSION,
    extract_relations,
    relations_config_hash,
)
from app.modules.structured_processing.document.txt import extract_txt
from app.modules.structured_processing.errors import (
    ErrorCode,
    InputResolutionUnavailableError,
    ProcessingError,
    WorkerApiError,
    WorkerAuthenticationError,
)
from app.modules.structured_processing.input_resolver import (
    LiveInputResolver,
    ResolvedInput,
    WorkerInputResolver,
)
from app.modules.structured_processing.limits import MAX_INPUT_BYTES, MAX_MENTION_TEXT_LENGTH
from app.modules.structured_processing.models import (
    ParserProfile,
    RawMention,
    RawRecord,
    SourceResolver,
    StaticBytesResolver,
    TextSegment,
)
from app.modules.structured_processing.provenance import (
    CONFIDENCE_STRUCTURED_COMPLETE,
    mention_to_observation,
    profile_config_hash,
)
from app.modules.structured_processing.structured.cdr import normalize_cdr_records
from app.modules.structured_processing.structured.chunked_processing import (
    assess_schema,
    iter_csv_record_chunks,
    iter_xlsx_record_chunks,
    normalize_chunk,
    peek_csv_header,
    peek_xlsx_header,
)
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

logger = structlog.get_logger(__name__)

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


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


# ---------------------------------------------------------------------------
# Phase 3 (Jasraj): document OCR/NER/relation extraction and CDR/finance
# chunked processing, both submitted through Nipun's `/observations`
# micro-batch endpoint rather than bundled into one terminal result. See
# this module's own docstring and docs/architecture/phase-3-decisions.md.
# ---------------------------------------------------------------------------


def _build_ner_adapter(settings: Settings) -> tuple[NerAdapter, str, str]:
    """The real local model if bootstrapped, else the deterministic fallback.

    Never raises and never crashes the job over a missing NER model asset
    -- the exact same graceful-degradation posture `media_processing.
    worker._build_analysis_components` already established for a missing
    detector/OCR model. See `document/ner_fallback.py`'s module docstring.
    """
    try:
        from app.modules.structured_processing.document.ner_spacy import (
            NER_MODEL_ADAPTER_NAME,
            NER_MODEL_VERSION,
            SpacyNerAdapter,
        )

        adapter = SpacyNerAdapter(settings.ner_model_path)
        return adapter, NER_MODEL_ADAPTER_NAME, NER_MODEL_VERSION
    except ProcessingError:
        return DeterministicNerAdapter(), NER_FALLBACK_ADAPTER_NAME, NER_FALLBACK_ADAPTER_VERSION


def _remap_mention(
    mention: RawMention,
    offset_map: OffsetMap,
    page: int | None,
    ocr_result: OcrPageResult | None,
) -> RawMention:
    """Map a mention's locator from normalized-text space back to source-text space.

    `offset_map.to_source` (see `document/normalization.py`) resolves the
    exact original span; if this page's text came from OCR, the matching
    bounding box (via `locate_bbox_for_span`) is attached too, so an OCR
    mention is always traceable to page *and* bounding box, not page alone.
    """
    start = mention.locator.span_start
    end = mention.locator.span_end
    if start is None or end is None:  # pragma: no cover - every FIR/NER mention sets a span
        return mention
    src_start, src_end = offset_map.to_source(start, end)
    bbox = locate_bbox_for_span(ocr_result, src_start, src_end) if ocr_result is not None else None
    new_locator = SourceLocator(
        page=page, span_start=src_start, span_end=src_end, bbox_xyxy_normalized=bbox
    )
    return replace(mention, locator=new_locator)


def _ner_mentions_to_raw(
    mentions: list[NerMention],
    offset_map: OffsetMap,
    page: int | None,
    ocr_result: OcrPageResult | None,
) -> list[RawMention]:
    results: list[RawMention] = []
    for ner_mention in mentions:
        start, end = offset_map.to_source(ner_mention.start, ner_mention.end)
        bbox = locate_bbox_for_span(ocr_result, start, end) if ocr_result is not None else None
        locator = SourceLocator(
            page=page, span_start=start, span_end=end, bbox_xyxy_normalized=bbox
        )
        results.append(
            RawMention(
                observation_type="ner_entity_mention",
                text=ner_mention.text,
                locator=locator,
                confidence=ner_mention.confidence,
                entity_type_hint=ner_mention.label.lower(),
                attributes={
                    "ner_label": ner_mention.label,
                    "adapter_name": ner_mention.adapter_name,
                    "adapter_version": ner_mention.adapter_version,
                },
            )
        )
    return results


@dataclass(frozen=True)
class _PageExtraction:
    mentions: list[RawMention]
    normalization_config_hash: str


def _extract_page_mentions(
    text: str, page: int | None, ner_adapter: NerAdapter, ocr_result: OcrPageResult | None
) -> _PageExtraction:
    """Regex + NER + rule-based relation extraction over one page/segment's text.

    Runs entirely against *normalized* text (see `document/normalization.py`)
    so a label split across a line-wrap or extra OCR spacing still matches,
    then remaps every mention's span back to the exact original source
    location before returning -- provenance is never lost to normalization.
    """
    normalized = normalize_text(text)
    regex_raw = extract_fir_mentions([TextSegment(page=page, text=normalized.normalized)])
    regex_mentions = [_remap_mention(m, normalized.offset_map, page, ocr_result) for m in regex_raw]
    ner_raw = ner_adapter.extract(normalized.normalized)
    ner_mentions = _ner_mentions_to_raw(list(ner_raw), normalized.offset_map, page, ocr_result)
    combined = regex_mentions + ner_mentions
    relation_mentions = extract_relations(combined)
    all_mentions = combined + relation_mentions
    if ocr_result is not None:
        all_mentions = [
            replace(
                mention,
                attributes={
                    **mention.attributes,
                    "ocr_extractor": {
                        "name": OCR_ENGINE_NAME,
                        "version": ocr_result.engine_version,
                        "model_version": ocr_result.tesseract_version,
                        "config_hash": ocr_result.config_hash,
                    },
                },
            )
            for mention in all_mentions
        ]
    return _PageExtraction(
        mentions=all_mentions,
        normalization_config_hash=normalization_config_hash(),
    )


def run_document_job_with_batches(
    *,
    client: WorkerApiClient,
    job: WorkerJobV1,
    claim_token: str,
    kind: ContentKind,
    profile: ParserProfile,
    data: bytes,
    clock: Clock = _default_clock,
) -> WorkerResultV1:
    """Process one document (PDF/DOCX/TXT), submitting one micro-batch per page/segment.

    A `TEXT_TRUSTED` PDF page never runs OCR. A `SCANNED_NO_TEXT`/
    `UNTRUSTWORTHY_TEXT_LAYER` page runs real local OCR (`document/ocr.py`)
    -- unless the OCR runtime itself is unavailable, in which case those
    specific pages are reported `DEFERRED` (mirroring the pre-Phase-3
    `document_requires_ocr` checkpoint) while every other page's batches
    are still submitted; a `CORRUPT` page is reported, safely, in that same
    checkpoint rather than silently vanishing. Never raises `ProcessingError`
    -- caught here and turned into a terminal `FAILED` result, exactly like
    `process_job`'s own contract.
    """
    settings = get_settings()
    now = clock()
    ner_adapter, ner_adapter_name, ner_adapter_version = _build_ner_adapter(settings)

    try:
        if kind is ContentKind.PDF:
            return _run_pdf_document_batches(
                client=client,
                job=job,
                claim_token=claim_token,
                profile=profile,
                data=data,
                ner_adapter=ner_adapter,
                ner_adapter_name=ner_adapter_name,
                ner_adapter_version=ner_adapter_version,
                settings=settings,
                now=now,
            )

        segments = extract_docx(data) if kind is ContentKind.DOCX else extract_txt(data)
        text = segments[0].text if segments else ""
        extraction = _extract_page_mentions(text, None, ner_adapter, None)
        observations = _observations_for(job, profile, extraction.mentions, now)
        batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=0)
        transformations = [
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=0,
                step_name=NORMALIZATION_NAME,
                step_version=NORMALIZATION_VERSION,
                config_hash=extraction.normalization_config_hash,
                started_at=now,
                completed_at=now,
            ),
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=1,
                step_name="regex_identifier_extraction",
                step_version=profile.version,
                config_hash=profile_config_hash(profile),
                started_at=now,
                completed_at=now,
                output_observation_ids=[o.observation_id for o in observations],
            ),
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=2,
                step_name="local_ner_extraction",
                step_version=ner_adapter_version,
                config_hash=ner_adapter_name,
                started_at=now,
                completed_at=now,
            ),
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=3,
                step_name="rule_based_event_extraction",
                step_version=RELATIONS_RULESET_VERSION,
                config_hash=relations_config_hash(),
                started_at=now,
                completed_at=now,
                output_observation_ids=[o.observation_id for o in observations],
            ),
        ]
        progress = build_progress(
            stage="parsing",
            units_total=1,
            units_completed=1,
            observations_emitted=len(observations),
            batch_sequence=0,
            occurred_at=now,
            message_code="DOCUMENT_PROCESSED",
        )
        submission = build_batch_submission(
            job=job,
            batch_sequence=0,
            submitted_at=now,
            observations=observations,
            transformations=transformations,
            progress=progress,
            is_final_batch=True,
        )
        client.submit_batch(job_id=job.job_id, claim_token=claim_token, submission=submission)
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.SUCCEEDED,
            observations=[],
            derived_artifacts=[],
            checkpoint=None,
            error=None,
            completed_at=now,
        )
    except ProcessingError as exc:
        return _fail(job, exc, now)


def _observations_for(
    job: WorkerJobV1, profile: ParserProfile, mentions: list[RawMention], now: datetime
) -> list[ObservationV1]:
    return [
        mention_to_observation(
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            profile=profile,
            mention=mention,
            created_at=now,
        )
        for mention in mentions
    ]


def _run_pdf_document_batches(
    *,
    client: WorkerApiClient,
    job: WorkerJobV1,
    claim_token: str,
    profile: ParserProfile,
    data: bytes,
    ner_adapter: NerAdapter,
    ner_adapter_name: str,
    ner_adapter_version: str,
    settings: Settings,
    now: datetime,
) -> WorkerResultV1:
    extraction = extract_pdf(data)

    ocr_engine: DocumentPageOcrEngine | None = None
    if extraction.ocr_required_pages:
        try:
            ocr_engine = DocumentPageOcrEngine(
                OcrConfig(
                    language=settings.document_ocr_language,
                    dpi=settings.document_ocr_dpi,
                    min_confidence=settings.document_ocr_min_confidence,
                )
            )
        except ProcessingError:
            ocr_engine = None  # degrade: those pages are reported deferred below, not a crash

    pages_text: dict[int, tuple[str, OcrPageResult | None]] = {
        segment.page: (segment.text, None)
        for segment in extraction.segments
        if segment.page is not None
    }
    deferred_pages: list[int] = []
    if ocr_engine is not None and extraction.ocr_required_pages:
        rendered = render_pdf_pages(
            data, extraction.ocr_required_pages, dpi=settings.document_ocr_dpi
        )
        for page_number in extraction.ocr_required_pages:
            image = rendered.get(page_number)
            if image is None:
                deferred_pages.append(page_number)
                continue
            recognized = ocr_engine.recognize_page(image, page_number=page_number)
            pages_text[page_number] = (recognized.joined_text, recognized)
    else:
        deferred_pages.extend(extraction.ocr_required_pages)

    for batch_sequence, page_number in enumerate(sorted(pages_text)):
        text, ocr_result = pages_text[page_number]
        assessment = extraction.page_assessments.get(page_number)
        extraction_step = (
            "pdf_page_ocr" if ocr_result is not None else "pdf_embedded_text_extraction"
        )

        page_extraction = _extract_page_mentions(text, page_number, ner_adapter, ocr_result)
        observations = _observations_for(job, profile, page_extraction.mentions, now)

        batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=batch_sequence)
        transformations: list[TransformationProvenanceV1] = [
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=0,
                step_name="pdf_text_layer_assessment",
                step_version="1.0.0",
                config_hash=page_trust_config_hash(),
                started_at=now,
                completed_at=now,
                safe_metadata={
                    "page": page_number,
                    "trust_level": assessment.level.value if assessment else "unknown",
                },
            )
        ]
        if ocr_result is not None:
            transformations.append(
                build_transformation(
                    job=job,
                    batch_id=batch_id,
                    ordinal=1,
                    step_name=extraction_step,
                    step_version="1.0.0",
                    config_hash=ocr_config_hash(ocr_engine.config),  # type: ignore[union-attr]
                    started_at=now,
                    completed_at=now,
                    safe_metadata={
                        "page": page_number,
                        "region_count": len(ocr_result.regions),
                        "average_confidence": ocr_result.average_confidence,
                        "tesseract_version": ocr_result.tesseract_version,
                        "dpi": settings.document_ocr_dpi,
                    },
                )
            )
        else:
            transformations.append(
                build_transformation(
                    job=job,
                    batch_id=batch_id,
                    ordinal=1,
                    step_name=extraction_step,
                    step_version="1.0.0",
                    config_hash="n/a",
                    started_at=now,
                    completed_at=now,
                    safe_metadata={"page": page_number},
                )
            )
        transformations.append(
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=2,
                step_name=NORMALIZATION_NAME,
                step_version=NORMALIZATION_VERSION,
                config_hash=page_extraction.normalization_config_hash,
                started_at=now,
                completed_at=now,
                safe_metadata={"page": page_number},
            )
        )
        transformations.append(
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=3,
                step_name="regex_identifier_extraction",
                step_version=profile.version,
                config_hash=profile_config_hash(profile),
                started_at=now,
                completed_at=now,
                output_observation_ids=[o.observation_id for o in observations],
                safe_metadata={"page": page_number},
            )
        )
        transformations.append(
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=4,
                step_name="local_ner_extraction",
                step_version=ner_adapter_version,
                config_hash=ner_adapter_name,
                started_at=now,
                completed_at=now,
                safe_metadata={"page": page_number},
            )
        )
        transformations.append(
            build_transformation(
                job=job,
                batch_id=batch_id,
                ordinal=5,
                step_name="rule_based_event_extraction",
                step_version=RELATIONS_RULESET_VERSION,
                config_hash=relations_config_hash(),
                started_at=now,
                completed_at=now,
                output_observation_ids=[o.observation_id for o in observations],
                safe_metadata={"page": page_number},
            )
        )

        progress = build_progress(
            stage="parsing",
            units_total=extraction.total_pages,
            units_completed=batch_sequence + 1,
            observations_emitted=len(observations),
            batch_sequence=batch_sequence,
            occurred_at=now,
            message_code="PAGE_PROCESSED",
        )
        submission = build_batch_submission(
            job=job,
            batch_sequence=batch_sequence,
            submitted_at=now,
            observations=observations,
            transformations=transformations,
            progress=progress,
        )
        client.submit_batch(job_id=job.job_id, claim_token=claim_token, submission=submission)

    all_deferred = sorted(set(deferred_pages) | set(extraction.corrupt_pages))
    if all_deferred:
        checkpoint = json.dumps(
            {
                "reason": ErrorCode.DOCUMENT_REQUIRES_OCR,
                "deferred_pages": sorted(deferred_pages),
                "corrupt_pages": sorted(extraction.corrupt_pages),
                "total_pages": extraction.total_pages,
            },
            sort_keys=True,
        )
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.DEFERRED,
            observations=[],
            derived_artifacts=[],
            checkpoint=checkpoint,
            error=None,
            completed_at=now,
        )

    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.SUCCEEDED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=None,
        completed_at=now,
    )


def _list_in_chunks(records: list[RawRecord], batch_size: int) -> Iterator[list[RawRecord]]:
    for i in range(0, len(records), batch_size):
        yield records[i : i + batch_size]


def run_structured_batches_job(
    *,
    client: WorkerApiClient,
    job: WorkerJobV1,
    claim_token: str,
    profile: ParserProfile,
    kind: ContentKind,
    data: bytes,
    clock: Clock = _default_clock,
) -> WorkerResultV1:
    """Process one CDR/finance source in bounded, vectorized micro-batches.

    Schema is assessed once, against the header alone, before any row is
    read (`ambiguous_schema` aborts the whole job with zero batches
    submitted). Each chunk is normalized independently and malformed rows
    within it are reported, never aborting the chunk (see
    `structured.chunked_processing`'s documented partial-success policy) --
    the terminal result's `checkpoint` names the total malformed-row count
    whenever it's non-zero, and the job is only `FAILED` outright if *every*
    row failed. Never raises `ProcessingError` -- caught here and turned
    into a terminal `FAILED` result.
    """
    settings = get_settings()
    now = clock()
    batch_size = settings.structured_batch_size
    step_prefix = "cdr" if profile.name == CDR_GENERIC_V1.name else "finance"

    try:
        if kind is ContentKind.CSV:
            header = peek_csv_header(data)
            assess_schema(profile, header)
            chunks = iter_csv_record_chunks(data, batch_size=batch_size)
        elif kind is ContentKind.XLSX:
            header = peek_xlsx_header(data)
            assess_schema(profile, header)
            chunks = iter_xlsx_record_chunks(data, batch_size=batch_size)
        elif kind is ContentKind.JSON:
            records = parse_json_records(data)
            header = list(records[0].values.keys()) if records else []
            assess_schema(profile, header)
            chunks = _list_in_chunks(records, batch_size)
        else:  # pragma: no cover - defensive; classify() already rejected anything else upstream
            raise ProcessingError(
                ErrorCode.UNSUPPORTED_PARSER_PROFILE,
                f"content kind '{kind}' has no chunked-batch profile",
            )

        total_valid = 0
        total_malformed = 0
        renew_since_last = 0
        for batch_sequence, chunk in enumerate(chunks):
            chunk_result = normalize_chunk(profile, chunk)
            observations = _observations_for(job, profile, chunk_result.mentions, now)
            batch_id = deterministic_batch_id(job_id=job.job_id, batch_sequence=batch_sequence)

            transformations: list[TransformationProvenanceV1] = []
            ordinal = 0
            if batch_sequence == 0:
                schema_metadata: dict[str, JsonValue] = {
                    "column_count": len(header),
                    "columns_sample": ", ".join(sorted(header)[:20])[:400],
                }
                transformations.append(
                    build_transformation(
                        job=job,
                        batch_id=batch_id,
                        ordinal=ordinal,
                        step_name=f"{step_prefix}_schema_normalization",
                        step_version=profile.version,
                        config_hash=profile_config_hash(profile),
                        started_at=now,
                        completed_at=now,
                        safe_metadata=schema_metadata,
                    )
                )
                ordinal += 1
            transformations.append(
                build_transformation(
                    job=job,
                    batch_id=batch_id,
                    ordinal=ordinal,
                    step_name=f"{step_prefix}_batch_processing",
                    step_version=profile.version,
                    config_hash=profile_config_hash(profile),
                    started_at=now,
                    completed_at=now,
                    output_observation_ids=[o.observation_id for o in observations],
                    safe_metadata={
                        "batch_sequence": batch_sequence,
                        "valid_row_count": chunk_result.valid_row_count,
                        "malformed_row_count": len(chunk_result.malformed_rows),
                    },
                )
            )

            total_valid += chunk_result.valid_row_count
            total_malformed += len(chunk_result.malformed_rows)
            progress = build_progress(
                stage="normalizing",
                units_completed=total_valid + total_malformed,
                observations_emitted=len(observations),
                batch_sequence=batch_sequence,
                occurred_at=now,
                message_code="BATCH_NORMALIZED",
            )
            submission = build_batch_submission(
                job=job,
                batch_sequence=batch_sequence,
                submitted_at=now,
                observations=observations,
                transformations=transformations,
                progress=progress,
            )
            client.submit_batch(job_id=job.job_id, claim_token=claim_token, submission=submission)

            renew_since_last += 1
            if renew_since_last >= 5:
                # Best-effort heartbeat only; a failure here is not fatal to the job.
                with contextlib.suppress(WorkerApiError):
                    client.renew_lease(job.job_id, claim_token=claim_token)
                renew_since_last = 0

        if total_valid == 0 and total_malformed > 0:
            return WorkerResultV1(
                job_id=job.job_id,
                case_id=job.case_id,
                evidence_id=job.evidence_id,
                status=WorkerStatus.FAILED,
                observations=[],
                derived_artifacts=[],
                checkpoint=None,
                error=WorkerError(
                    code=ErrorCode.PARTIAL_ROW_FAILURES,
                    message=f"all {total_malformed} row(s) failed row-level normalization",
                    retryable=False,
                ),
                completed_at=now,
            )

        checkpoint = None
        if total_malformed > 0:
            checkpoint = json.dumps(
                {"valid_row_count": total_valid, "malformed_row_count": total_malformed},
                sort_keys=True,
            )
        return WorkerResultV1(
            job_id=job.job_id,
            case_id=job.case_id,
            evidence_id=job.evidence_id,
            status=WorkerStatus.SUCCEEDED,
            observations=[],
            derived_artifacts=[],
            checkpoint=checkpoint,
            error=None,
            completed_at=now,
        )
    except ProcessingError as exc:
        return _fail(job, exc, now)


#: Every profile this worker's `process_job` dispatch table supports,
#: matching `structured/profiles.py` exactly (name, version). Nipun's
#: current routing (`evidence_lifecycle/routing.py`) only ever creates
#: `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1`
#: jobs from a real evidence upload -- `generic_tabular_v1`/
#: `generic_json_v1` are supported here for completeness (and any future
#: routing change or direct job construction) but are never claimable
#: through the live upload path today. See
#: docs/architecture/structured-processing-worker.md.
SUPPORTED_PROCESSORS: tuple[tuple[str, str], ...] = (
    (FIR_REPORT_TEXT_V1.name, FIR_REPORT_TEXT_V1.version),
    (CDR_GENERIC_V1.name, CDR_GENERIC_V1.version),
    (FINANCIAL_TRANSACTION_GENERIC_V1.name, FINANCIAL_TRANSACTION_GENERIC_V1.version),
    (GENERIC_TABULAR_V1.name, GENERIC_TABULAR_V1.version),
    (GENERIC_JSON_V1.name, GENERIC_JSON_V1.version),
)

#: A safe, non-secret checkpoint recorded on a `DEFERRED` result when the
#: claimed job's evidence couldn't be resolved because the input-access
#: endpoint doesn't exist yet -- mirrors `document_requires_ocr`'s role for
#: a scanned PDF: an honest, expected "not yet possible" outcome, not a
#: fabricated failure or a crash.
CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE = "input_resolution_unavailable"


@dataclass(frozen=True)
class RunOnceOutcome:
    """What one `run_once` call actually did -- for the CLI's exit behavior and for tests."""

    claimed: bool
    job_id: UUID | None
    result_status: str | None
    deferred_reason: str | None = None


def _dispatch_job(
    *,
    client: WorkerApiClient,
    job: WorkerJobV1,
    claim_token: str,
    evidence: EvidenceRecordV1,
    data: bytes,
    clock: Clock,
) -> WorkerResultV1:
    """Route a claimed job to the micro-batch orchestration for its profile, if it has one.

    `fir_report_text_v1`/`cdr_generic_v1`/`financial_transaction_generic_v1`
    (Jasraj's Phase 3 profiles) submit their observations as one or more
    batches through `client.submit_batch` and return a terminal result with
    `observations=[]`. Every other profile (`generic_tabular_v1`/
    `generic_json_v1`, Nipun's fallbacks) is unchanged: `process_job` runs
    synchronously and its full result -- observations included -- is
    submitted directly via `/result`, exactly as every phase before this
    one already did.
    """
    kind = classify(evidence.content_type, evidence.original_filename)
    profile = get_profile(job.processor_name)

    if profile.name == FIR_REPORT_TEXT_V1.name:
        return run_document_job_with_batches(
            client=client,
            job=job,
            claim_token=claim_token,
            kind=kind,
            profile=profile,
            data=data,
            clock=clock,
        )
    if profile.name in (CDR_GENERIC_V1.name, FINANCIAL_TRANSACTION_GENERIC_V1.name):
        return run_structured_batches_job(
            client=client,
            job=job,
            claim_token=claim_token,
            profile=profile,
            kind=kind,
            data=data,
            clock=clock,
        )
    return process_job(job, evidence, StaticBytesResolver(payload=data))


def run_once(
    *,
    client: WorkerApiClient,
    input_resolver: WorkerInputResolver,
    clock: Clock = _default_clock,
    processors: Sequence[tuple[str, str]] = SUPPORTED_PROCESSORS,
) -> RunOnceOutcome:
    """Claim at most one job, process it, submit its result, and return what happened.

    Tries each supported `(processor_name, processor_version)` in turn
    until one yields a job, or returns `claimed=False` once all are
    exhausted -- a normal, successful "no work" outcome, never an error.
    Never submits `queued`/`running` as a result: `process_job` only ever
    returns a terminal `WorkerResultV1`, and the input-resolution-gap path
    below submits `DEFERRED` (also terminal), never fabricates `SUCCEEDED`.
    """
    run_id = str(uuid4())
    structlog.contextvars.bind_contextvars(run_id=run_id)
    try:
        logger.info("worker.run_once.started")
        claim_result = None
        for processor_name, processor_version in processors:
            claim_result = client.claim(
                processor_name=processor_name, processor_version=processor_version
            )
            if claim_result.job is not None:
                break

        if claim_result is None or claim_result.job is None:
            logger.info("worker.run_once.no_job_available")
            return RunOnceOutcome(claimed=False, job_id=None, result_status=None)

        job = claim_result.job
        claim_token = claim_result.claim_token
        if claim_token is None:  # pragma: no cover - defensive: API always pairs job+token
            raise WorkerApiError("internal worker API returned a job without a claim token")

        structlog.contextvars.bind_contextvars(job_id=str(job.job_id))
        logger.info("worker.run_once.claimed", processor_name=job.processor_name)

        try:
            resolved = input_resolver.resolve(job, claim_token=claim_token)
        except InputResolutionUnavailableError as exc:
            logger.warning("worker.run_once.input_unavailable")
            deferred = _deferred_result_for_missing_input(job, clock())
            client.submit_result(job_id=job.job_id, claim_token=claim_token, result=deferred)
            return RunOnceOutcome(
                claimed=True,
                job_id=job.job_id,
                result_status=deferred.status.value,
                deferred_reason=str(exc),
            )

        if resolved.expected_sha256 is not None:
            actual_sha256 = hashlib.sha256(resolved.data).hexdigest()
            if actual_sha256 != resolved.expected_sha256:
                logger.error("worker.run_once.integrity_mismatch")
                mismatch = _failed_result_for_integrity_mismatch(job, clock())
                ack = client.submit_result(
                    job_id=job.job_id, claim_token=claim_token, result=mismatch
                )
                return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)

        evidence = _shim_evidence_record(job, resolved)
        result = _dispatch_job(
            client=client,
            job=job,
            claim_token=claim_token,
            evidence=evidence,
            data=resolved.data,
            clock=clock,
        )
        ack = client.submit_result(job_id=job.job_id, claim_token=claim_token, result=result)
        logger.info(
            "worker.run_once.submitted",
            status=ack.status,
            observation_count=ack.observation_count,
        )
        return RunOnceOutcome(claimed=True, job_id=job.job_id, result_status=ack.status)
    finally:
        structlog.contextvars.unbind_contextvars("run_id", "job_id")


def _deferred_result_for_missing_input(job: WorkerJobV1, completed_at: datetime) -> WorkerResultV1:
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.DEFERRED,
        observations=[],
        derived_artifacts=[],
        checkpoint=CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
        error=None,
        completed_at=completed_at,
    )


def _failed_result_for_integrity_mismatch(
    job: WorkerJobV1, completed_at: datetime
) -> WorkerResultV1:
    """A genuine data-integrity problem (not "not yet possible") -- `FAILED`, not `DEFERRED`.

    `retryable=True`: a fresh claim/re-stream could plausibly succeed if
    the mismatch was caused by a one-off transport issue rather than a
    persistently corrupt stored object.
    """
    return WorkerResultV1(
        job_id=job.job_id,
        case_id=job.case_id,
        evidence_id=job.evidence_id,
        status=WorkerStatus.FAILED,
        observations=[],
        derived_artifacts=[],
        checkpoint=None,
        error=WorkerError(
            code="evidence_integrity_mismatch",
            message="resolved evidence bytes do not match the expected SHA-256",
            retryable=True,
        ),
        completed_at=completed_at,
    )


def _shim_evidence_record(job: WorkerJobV1, resolved: ResolvedInput) -> EvidenceRecordV1:
    """Reconstruct the minimal `EvidenceRecordV1` that `process_job` actually reads.

    `process_job` reads only `.content_type`/`.original_filename` from this
    object (see its own docstring above) -- every other field here is
    either a real, already-known value (`case_id`/`evidence_id` from the
    job; `object_uri` = `job.input_object_uri`; `sha256` computed from the
    bytes actually resolved; `uploaded_at` = `job.requested_at`) or an
    unused structural placeholder Pydantic requires but `process_job` never
    reads (`classification`, `uploaded_by`, `processing_status`,
    `parser_profile`) -- called out explicitly here so a future reader
    never mistakes a placeholder for real evidence metadata a worker
    legitimately has no authenticated way to obtain (see
    `input_resolver.py`'s module docstring).
    """
    return EvidenceRecordV1(
        evidence_id=job.evidence_id,
        case_id=job.case_id,
        source_type=job.source_type,
        original_filename=resolved.original_filename,
        content_type=resolved.content_type,
        object_uri=job.input_object_uri,
        sha256=hashlib.sha256(resolved.data).hexdigest(),
        classification=EvidenceClassification.UNCLASSIFIED,  # placeholder -- unused by process_job
        uploaded_by="unknown",  # placeholder -- unused by process_job
        uploaded_at=job.requested_at,
        parser_profile=None,
        processing_status=EvidenceProcessingStatus.QUEUED,  # placeholder -- unused by process_job
    )


def _configure_logging() -> None:
    """Structured JSON logging to stdout, mirroring `app.main`'s configuration.

    Deliberately duplicated rather than imported from `app.main`: importing
    it would construct the full FastAPI application (every router, every
    other module's module-level dependency wiring) just to log one line
    from a one-shot CLI script.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _build_client(settings: Settings) -> WorkerApiClient:
    token = settings.worker_token
    if token is None:
        raise WorkerAuthenticationError(
            "WORKER_TOKEN is not configured for this worker process; see "
            "docs/architecture/worker-identity-and-security.md"
        )
    return WorkerApiClient(
        base_url=settings.worker_api_base_url, shared_secret=token.get_secret_value()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: `uv run python -m app.modules.structured_processing.worker --once`."""
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.structured_processing.worker",
        description=(
            "Claim and process at most one compatible structured-processing job, then exit. "
            "No daemon or polling mode exists in this phase."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run exactly one claim-process-submit cycle, then exit.",
    )
    parser.parse_args(argv)

    _configure_logging()
    settings = get_settings()
    try:
        client = _build_client(settings)
    except WorkerAuthenticationError as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1

    try:
        outcome = run_once(client=client, input_resolver=LiveInputResolver(client))
    except (WorkerAuthenticationError, WorkerApiError, InputResolutionUnavailableError) as exc:
        logger.error("worker.cli.failed", reason=str(exc))
        return 1
    finally:
        client.close()

    logger.info(
        "worker.cli.done",
        job_id=str(outcome.job_id) if outcome.job_id else None,
        status=outcome.result_status or "no_job_available",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CDR_GENERIC_V1",
    "FINANCIAL_TRANSACTION_GENERIC_V1",
    "FIR_REPORT_TEXT_V1",
    "GENERIC_JSON_V1",
    "GENERIC_TABULAR_V1",
    "SUPPORTED_PROCESSORS",
    "RunOnceOutcome",
    "main",
    "process_job",
    "run_once",
]
