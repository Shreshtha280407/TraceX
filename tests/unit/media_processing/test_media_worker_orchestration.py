"""`run_once`/`main` orchestration against a fake `WorkerApiClient`.

The fake below duck-types `client.WorkerApiClient`'s public surface
(`claim`/`submit_result`/`close`) -- `run_once` never constructs a client
itself, it only calls methods on whatever is injected, so no HTTP or
`httpx.MockTransport` is needed here (that wire-level proof lives in
`test_media_worker_client.py`). This file proves the claim -> resolve ->
SHA-256-verify -> `process_job` -> submit sequencing, the no-job exit, the
input-resolution-gap `DEFERRED` path, and the integrity-mismatch `FAILED`
path -- mirrors `tests/unit/communication_processing/
test_communication_worker_orchestration.py`'s pattern exactly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from app.contracts.evidence import SourceType
from app.contracts.worker import WorkerStatus
from app.modules.media_processing.analysis.fake_detector import FakeObjectDetector
from app.modules.media_processing.client import ClaimResult, SubmitResultAck
from app.modules.media_processing.errors import InputResolutionUnavailableError, WorkerApiError
from app.modules.media_processing.input_resolver import ResolvedMediaInput, StaticInputResolver
from app.modules.media_processing.ocr_adapter import FixtureOcrAdapter
from app.modules.media_processing.worker import (
    CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
    PROCESSOR_NAME_DETECTION,
    PROCESSOR_NAME_METADATA,
    PROCESSOR_VERSION,
    SUPPORTED_PROCESSORS,
    RunOnceOutcome,
    _effective_processors,
    run_once,
)
from tests.fixtures.media_processing.factory import make_evidence_and_job
from tests.fixtures.media_processing.synthetic import (
    ffmpeg_available,
    make_png_bytes,
    make_synthetic_mp4_bytes,
)


@dataclass
class _FakeClient:
    """Duck-types `WorkerApiClient`'s `claim`/`submit_result`/`close`."""

    claim_responses: list[ClaimResult]
    submit_ack: SubmitResultAck | None = None
    submit_exception: Exception | None = None
    submit_batch_exception: Exception | None = None
    submit_calls: list[tuple[UUID, str, object]] = field(default_factory=list)
    submit_batch_calls: list[tuple[UUID, str, object]] = field(default_factory=list)
    _claim_calls: int = 0

    def claim(self, *, processor_name: str, processor_version: str) -> ClaimResult:  # noqa: ARG002
        response = self.claim_responses[self._claim_calls]
        self._claim_calls += 1
        return response

    def submit_result(self, *, job_id: UUID, claim_token: str, result: object) -> SubmitResultAck:
        self.submit_calls.append((job_id, claim_token, result))
        if self.submit_exception is not None:
            raise self.submit_exception
        assert self.submit_ack is not None
        return self.submit_ack

    def submit_batch(self, *, job_id: UUID, claim_token: str, submission: object) -> None:
        self.submit_batch_calls.append((job_id, claim_token, submission))
        if self.submit_batch_exception is not None:
            raise self.submit_batch_exception

    def close(self) -> None:
        pass


@dataclass
class _RaisingResolver:
    def resolve(self, job: object, *, claim_token: str) -> ResolvedMediaInput:  # noqa: ARG002
        raise InputResolutionUnavailableError(
            "no input-access endpoint is available on the internal worker API yet"
        )


def _ack(job_id: UUID, *, status: str = "succeeded") -> SubmitResultAck:
    return SubmitResultAck(
        job_id=job_id, status=status, result_id=uuid4(), observation_count=0, observation_ids=()
    )


# --- supported processors ------------------------------------------------------


def test_supported_processors_tries_detection_then_metadata() -> None:
    """Real IMAGE/VIDEO uploads now route to `media_detection_v1` -- see
    `docs/architecture/phase-2-decisions.md`'s "Real local media inference
    closeout" -- so the live claim loop tries it first; `media_metadata_v1`
    remains tried second for a directly-constructed/legacy job."""
    assert SUPPORTED_PROCESSORS == (
        (PROCESSOR_NAME_DETECTION, PROCESSOR_VERSION),
        (PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),
    )


def test_effective_processors_falls_back_to_metadata_only_without_a_detector() -> None:
    """A worker instance with no detector model asset loaded must never claim
    (and then inevitably fail) a `media_detection_v1` job another,
    properly-configured instance could have handled."""
    assert _effective_processors(detector=None) == ((PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),)


def test_effective_processors_uses_full_set_with_a_detector() -> None:
    assert _effective_processors(detector=FakeObjectDetector()) == SUPPORTED_PROCESSORS


# --- happy path ----------------------------------------------------------------


def test_run_once_happy_path_image() -> None:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    payload = make_png_bytes()
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png",
            original_filename="photo.png",
            data=payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.claimed is True
    assert outcome.result_status == "succeeded"
    submitted_job_id, submitted_token, submitted_result = client.submit_calls[0]
    assert submitted_job_id == job.job_id
    assert submitted_token == "tok-abc"
    assert submitted_result.status is WorkerStatus.SUCCEEDED  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined]
    assert len(client.submit_batch_calls) == 1


def test_run_once_submits_ocr_and_media_observations_in_batches_before_empty_terminal() -> None:
    """Exercise the real ``run_once`` OCR closure without live infrastructure."""
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    payload = make_png_bytes(width=200, height=100)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png",
            original_filename="photo.png",
            data=payload,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    )

    outcome = run_once(
        client=client,  # type: ignore[arg-type]
        input_resolver=resolver,
        detector=FakeObjectDetector(),
        ocr_adapter=FixtureOcrAdapter(),  # real orchestration; fixture OCR is explicit
    )

    assert outcome.result_status == "succeeded"
    assert len(client.submit_batch_calls) == 2
    ocr_batch = client.submit_batch_calls[0][2]
    media_batch = client.submit_batch_calls[1][2]
    assert ocr_batch.batch_sequence == 0  # type: ignore[attr-defined]
    assert ocr_batch.is_final_batch is False  # type: ignore[attr-defined]
    assert [o.observation_type for o in ocr_batch.observations] == ["ocr_text_mention"]  # type: ignore[attr-defined]
    assert media_batch.batch_sequence == 1  # type: ignore[attr-defined]
    assert media_batch.is_final_batch is True  # type: ignore[attr-defined]
    assert {o.observation_type for o in media_batch.observations} == {  # type: ignore[attr-defined]
        "media_metadata",
        "object_detection",
    }
    assert client.submit_calls[0][2].observations == []  # type: ignore[attr-defined]


def test_run_once_batch_submission_failure_sends_safe_failed_terminal_result() -> None:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.IMAGE,
    )
    payload = make_png_bytes(width=200, height=100)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="failed"),
        submit_batch_exception=WorkerApiError("batch submission failed: HTTP 503"),
    )
    outcome = run_once(
        client=client,  # type: ignore[arg-type]
        input_resolver=StaticInputResolver(
            ResolvedMediaInput(
                content_type="image/png",
                original_filename="photo.png",
                data=payload,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
        ),
        detector=FakeObjectDetector(),
        ocr_adapter=FixtureOcrAdapter(),
    )

    assert outcome.result_status == "failed"
    terminal = client.submit_calls[0][2]
    assert terminal.status is WorkerStatus.FAILED  # type: ignore[attr-defined]
    assert terminal.error.code == "media_batch_submission_failed"  # type: ignore[attr-defined]
    assert terminal.observations == []  # type: ignore[attr-defined]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe unavailable")
def test_run_once_video_ocr_batches_have_global_zero_based_sequence_and_final_marker() -> None:
    """Run the video branch of the production callback without live services."""
    _metadata, job = make_evidence_and_job(
        content_type="video/mp4",
        filename="clip.mp4",
        processor_name=PROCESSOR_NAME_DETECTION,
        source_type=SourceType.VIDEO,
    )
    # The default sampler selects at least two frames from this clip.  That
    # makes this a regression proof for the old third-batch 422: the media
    # aggregate must not report a new ``1 / 1`` progress event after frame
    # OCR has already reported two completed units.
    payload = make_synthetic_mp4_bytes(width=96, height=64, duration_seconds=3.0, fps=5.0)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )

    outcome = run_once(
        client=client,  # type: ignore[arg-type]
        input_resolver=StaticInputResolver(
            ResolvedMediaInput(
                content_type="video/mp4",
                original_filename="clip.mp4",
                data=payload,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
        ),
        detector=FakeObjectDetector(),
        ocr_adapter=FixtureOcrAdapter(),
    )

    assert outcome.result_status == "succeeded"
    batches = [call[2] for call in client.submit_batch_calls]
    assert len(batches) >= 3  # one or more frame OCR batches, media, completion
    assert [batch.batch_sequence for batch in batches] == list(range(len(batches)))
    assert batches[-1].is_final_batch is True
    assert batches[-1].progress.stage == "video_frame_ocr"  # type: ignore[union-attr]
    frame_batches = [
        batch for batch in batches if batch.progress and batch.progress.stage == "video_frame_ocr"
    ]
    assert frame_batches
    for batch in frame_batches[:-1]:
        for observation in batch.observations:
            locator = observation.source_locator
            assert locator.frame_number is not None
            assert locator.time_start_ms is not None
            assert locator.time_end_ms is not None
            assert locator.time_start_ms <= locator.time_end_ms
            assert locator.bbox_xyxy_normalized is not None
    progress_events = [batch.progress for batch in batches if batch.progress is not None]
    assert [event.units_completed for event in progress_events] == sorted(
        event.units_completed for event in progress_events
    )
    media_batches = [batch for batch in batches if batch.progress is None]
    assert len(media_batches) == 1
    assert client.submit_calls[0][2].observations == []  # type: ignore[attr-defined]


def test_run_once_skips_sha_verification_when_resolver_supplies_none() -> None:
    """A resolver that can't supply `expected_sha256` (e.g. a bare static double
    built without one) never treats that absence as a mismatch."""
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png", original_filename="photo.png", data=make_png_bytes()
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"


# --- no job available -----------------------------------------------------------


def test_run_once_no_job_available() -> None:
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in SUPPORTED_PROCESSORS
        ]
    )
    outcome = run_once(
        client=client,
        input_resolver=StaticInputResolver(
            ResolvedMediaInput(content_type="image/png", original_filename="x.png", data=b"")
        ),
    )
    assert outcome == RunOnceOutcome(claimed=False, job_id=None, result_status=None)
    assert client.submit_calls == []


# --- input-resolution-gap defers, never fails or crashes ------------------------


def test_run_once_defers_when_input_endpoint_unreachable() -> None:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="deferred"),
    )

    outcome = run_once(client=client, input_resolver=_RaisingResolver())

    assert outcome.claimed is True
    assert outcome.result_status == "deferred"
    assert outcome.deferred_reason is not None
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.checkpoint == CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE  # type: ignore[attr-defined]
    assert submitted_result.status is WorkerStatus.DEFERRED  # type: ignore[attr-defined]


# --- SHA-256 mismatch fails safely, before any decode ---------------------------


def test_run_once_fails_safely_on_sha256_mismatch_before_decode() -> None:
    """A tampered/incomplete transfer never reaches `process_job` at all."""
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="failed"),
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png",
            original_filename="photo.png",
            data=b"not the bytes that were hashed",
            expected_sha256="0" * 64,
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "failed"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.error.code == "evidence_integrity_mismatch"  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined]


# --- multi-processor claim loop --------------------------------------------------


def test_run_once_tries_each_processor_until_one_has_work() -> None:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    processors = ((PROCESSOR_NAME_METADATA, PROCESSOR_VERSION), ("media_detection_v1", "1.0.0"))
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None),
            ClaimResult(job=job, claim_token="tok-last", lease_expires_at=None),
        ],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png", original_filename="photo.png", data=make_png_bytes()
        )
    )

    outcome = run_once(client=client, input_resolver=resolver, processors=processors)

    assert outcome.claimed is True
    assert outcome.result_status == "succeeded"


# --- idempotent resubmission -----------------------------------------------------


def test_repeated_result_submission_is_idempotent_at_the_client_layer() -> None:
    """`run_once` submits exactly once per claimed job; a second, separate
    `submit_result` call with the same job/claim/result is still safe to make
    (the server-side idempotent-replay guarantee this client relies on is proven
    live in `test_media_worker_live.py`)."""
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="photo.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    ack = _ack(job.job_id)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=ack,
    )
    resolver = StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png", original_filename="photo.png", data=make_png_bytes()
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)
    assert len(client.submit_calls) == 1

    # A direct repeat call (simulating a retried submission) reuses the exact
    # same claim token/result and gets back the same ack -- never a second,
    # differently-identified result.
    _, claim_token, result = client.submit_calls[0]
    second_ack = client.submit_result(job_id=job.job_id, claim_token=claim_token, result=result)
    assert second_ack.result_id == ack.result_id
    assert outcome.result_status == "succeeded"
