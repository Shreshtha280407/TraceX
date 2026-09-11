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
`input_resolver.WorkerInputResolver`, calls the existing `process_job`
above completely unchanged, and submits the result. No daemon, polling
loop, or scheduler -- see `docs/architecture/structured-processing-worker.md`.
`process_job` itself only ever raises `ProcessingError` for an input
problem (caught, turned into a `FAILED` result below); a genuine bug
propagates uncaught out of `run_once`/`main` on purpose, exactly as this
module's own docstring already establishes -- the claimed job's lease
simply expires and becomes reclaimable rather than a fabricated result
being submitted for it.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from app.contracts.worker import WorkerError, WorkerJobV1, WorkerResultV1, WorkerStatus
from app.core.config import Settings, get_settings
from app.modules.structured_processing.client import WorkerApiClient
from app.modules.structured_processing.document.classifier import ContentKind, classify
from app.modules.structured_processing.document.docx import extract_docx
from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.ocr_routing import route_pdf
from app.modules.structured_processing.document.pdf import extract_pdf
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

logger = structlog.get_logger(__name__)


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

Clock = Callable[[], datetime]


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunOnceOutcome:
    """What one `run_once` call actually did -- for the CLI's exit behavior and for tests."""

    claimed: bool
    job_id: UUID | None
    result_status: str | None
    deferred_reason: str | None = None


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
        result = process_job(job, evidence, StaticBytesResolver(payload=resolved.data))
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
    secret = settings.worker_shared_secret
    if secret is None:
        raise WorkerAuthenticationError(
            "WORKER_SHARED_SECRET is not configured; the internal worker API fails closed"
        )
    return WorkerApiClient(
        base_url=settings.worker_api_base_url, shared_secret=secret.get_secret_value()
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
