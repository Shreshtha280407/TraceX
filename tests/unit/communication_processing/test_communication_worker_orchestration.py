"""`run_once`/`main` orchestration against a fake `WorkerApiClient`.

The fake below duck-types `client.WorkerApiClient`'s public surface
(`claim`/`submit_result`/`close`) -- `run_once` never constructs a client
itself, it only calls methods on whatever is injected, so no HTTP or
`httpx.MockTransport` is needed here (that wire-level proof lives in
`test_worker_client.py`). This file proves the claim -> resolve -> build
typed input payload -> parse -> submit sequencing across every supported
profile, the multi-processor claim loop, the no-job exit, the
input-resolution-gap `DEFERRED` path, the integrity-mismatch and
malformed-interchange-payload `FAILED` paths, and that a submit failure
propagates without leaking the claim token.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.contracts.evidence import SourceType
from app.contracts.observation_batch import (
    BatchAcceptanceStatus,
    ObservationBatchReceiptV1,
    ObservationBatchSubmissionV1,
)
from app.contracts.worker import WorkerStatus
from app.modules.communication_processing.audio.asr_adapter import UnavailableAsrAdapter
from app.modules.communication_processing.client import ClaimResult, RenewAck, SubmitResultAck
from app.modules.communication_processing.errors import (
    InputResolutionUnavailableError,
    WorkerApiError,
)
from app.modules.communication_processing.input_resolver import ResolvedInput, StaticInputResolver
from app.modules.communication_processing.models import (
    DiarizationSegmentInput,
    TranscriptSegmentInput,
)
from app.modules.communication_processing.phase4 import DEEP_AUDIO_PROFILE, RAPID_AUDIO_PROFILE
from app.modules.communication_processing.worker import (
    AUDIO_METADATA_V1,
    CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE,
    DIARIZATION_IMPORT_V1,
    GENERIC_SOCIAL_JSON_V1,
    SUPPORTED_PROCESSORS,
    TRANSCRIPT_IMPORT_V1,
    WHATSAPP_EXPORT_V1,
    RunOnceOutcome,
    main,
    run_once,
)
from tests.fixtures.communication_processing.asr_fixture_adapter import AsrFixtureAdapter
from tests.fixtures.communication_processing.builders import (
    build_generic_json_export,
    build_wav_bytes,
    build_whatsapp_export,
)
from tests.fixtures.communication_processing.diarization_fixture_adapter import (
    DiarizationFixtureAdapter,
)
from tests.fixtures.communication_processing.factory import make_job


@dataclass
class _FakeClient:
    """Duck-types `WorkerApiClient`'s `claim`/`submit_result`/`submit_batch`/`renew_lease`."""

    claim_responses: list[ClaimResult]
    submit_ack: SubmitResultAck | None = None
    submit_exception: Exception | None = None
    submit_calls: list[tuple[UUID, str, object]] = field(default_factory=list)
    batch_calls: list[ObservationBatchSubmissionV1] = field(default_factory=list)
    renew_calls: int = 0
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

    def submit_batch(
        self, *, job_id: UUID, claim_token: str, submission: ObservationBatchSubmissionV1
    ) -> ObservationBatchReceiptV1:
        self.batch_calls.append(submission)
        return ObservationBatchReceiptV1(
            job_id=job_id,
            batch_id=submission.batch_id,
            status=BatchAcceptanceStatus.ACCEPTED,
            accepted_observation_count=len(submission.observations),
            progress=submission.progress,
            request_id=None,
        )

    def renew_lease(self, job_id: UUID, *, claim_token: str) -> RenewAck:  # noqa: ARG002
        self.renew_calls += 1
        return RenewAck(job_id=job_id, lease_expires_at=datetime.now(UTC))

    def close(self) -> None:
        pass


@dataclass
class _RaisingResolver:
    def resolve(self, job: object, *, claim_token: str) -> ResolvedInput:  # noqa: ARG002
        raise InputResolutionUnavailableError(
            "no input-access endpoint is available on the internal worker API yet"
        )


def _ack(job_id: UUID, *, status: str = "succeeded") -> SubmitResultAck:
    return SubmitResultAck(
        job_id=job_id, status=status, result_id=uuid4(), observation_count=0, observation_ids=()
    )


# --- happy path, one per input-payload shape ---------------------------------


def test_run_once_happy_path_audio_metadata() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-abc", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="audio/wav", original_filename="a.wav", data=build_wav_bytes())
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.claimed is True
    assert outcome.result_status == "succeeded"
    submitted_job_id, submitted_token, submitted_result = client.submit_calls[0]
    assert submitted_job_id == job.job_id
    assert submitted_token == "tok-abc"
    assert submitted_result.status is WorkerStatus.SUCCEEDED  # type: ignore[attr-defined]


def test_run_once_invokes_explicit_local_asr_path_and_keeps_transcript_out_of_attributes() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-audio", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="audio/wav",
            original_filename="signal.wav",
            data=build_wav_bytes(amplitude=2_000),
        )
    )
    outcome = run_once(
        client=client,
        input_resolver=resolver,
        audio_profile=RAPID_AUDIO_PROFILE,
        asr_adapter=AsrFixtureAdapter(
            segments=(
                TranscriptSegmentInput(0, 500, "synthetic transcript", "en", 0.8, "segment-1"),
            )
        ),
        diarization_adapter=DiarizationFixtureAdapter(segments=()),
    )

    assert outcome.result_status == "succeeded"
    observations = client.batch_calls[0].observations
    transcript = next(
        item for item in observations if item.observation_type == "transcript_segment"
    )
    assert transcript.source_locator.time_start_ms == 0
    assert transcript.source_locator.time_end_ms == 500
    assert transcript.extractor.config_hash == "fixture"
    assert "text" not in transcript.attributes
    assert transcript.attributes["transcript_text_length"] == len("synthetic transcript")
    assert transcript.attributes["manifest_id"]
    assert transcript.attributes["chunk_id"]


def test_run_once_missing_local_asr_defers_without_fabricating_a_transcript() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-audio", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="deferred"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="audio/wav", original_filename="signal.wav", data=build_wav_bytes()
        )
    )
    outcome = run_once(
        client=client,
        input_resolver=resolver,
        audio_profile=RAPID_AUDIO_PROFILE,
        asr_adapter=UnavailableAsrAdapter(),
        diarization_adapter=DiarizationFixtureAdapter(segments=()),
    )

    assert outcome.result_status == "deferred"
    assert [item.observation_type for item in client.batch_calls[0].observations] == [
        "audio_metadata"
    ]
    assert client.submit_calls[0][2].checkpoint == "local_asr_unavailable"  # type: ignore[attr-defined]


def test_run_once_deep_profile_adds_source_local_diarization_turns() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-deep", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="audio/wav",
            original_filename="signal.wav",
            data=build_wav_bytes(duration_seconds=21, amplitude=2_000),
        )
    )
    outcome = run_once(
        client=client,
        input_resolver=resolver,
        audio_profile=DEEP_AUDIO_PROFILE,
        asr_adapter=AsrFixtureAdapter(
            segments=(TranscriptSegmentInput(0, 500, "synthetic", "en", 0.8, "segment-1"),)
        ),
        diarization_adapter=DiarizationFixtureAdapter(
            segments=(DiarizationSegmentInput(0, 1_000, "speaker_1", 0.7, "turn-1"),)
        ),
    )

    assert outcome.result_status == "succeeded"
    turn = next(
        item
        for item in client.batch_calls[0].observations
        if item.observation_type == "diarization_speaker_turn"
    )
    assert turn.source_locator.time_start_ms == 0
    assert turn.source_locator.time_end_ms == 1_000
    assert turn.extracted_entities[0].entity_type_hint == "speaker_label_local"
    assert turn.attributes["speaker_identity_status"] == "source_local_unresolved"


def test_run_once_happy_path_transcript_import_json_interchange() -> None:
    """The new Phase 2 JSON interchange deserialization path."""
    job = make_job(
        processor_name=TRANSCRIPT_IMPORT_V1.name, source_type=SourceType.AUDIO_TRANSCRIPT
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    payload = json.dumps(
        {
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 500,
                    "text": "hello",
                    "language_hint": "en",
                    "confidence": 0.9,
                    "source_segment_id": "seg-1",
                }
            ]
        }
    ).encode()
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="application/json", original_filename="transcript.json", data=payload
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.status is WorkerStatus.SUCCEEDED  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined] -- delivered via batch
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0].observations[0].observation_type == "transcript_segment"


def test_run_once_happy_path_diarization_import_json_interchange() -> None:
    job = make_job(
        processor_name=DIARIZATION_IMPORT_V1.name, source_type=SourceType.AUDIO_DIARIZATION
    )
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-y", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    payload = json.dumps(
        {
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 800,
                    "speaker_label": "SPEAKER_00",
                    "confidence": 0.8,
                    "source_segment_id": "seg-1",
                }
            ]
        }
    ).encode()
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="application/json", original_filename="diarization.json", data=payload
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.observations == []  # type: ignore[attr-defined] -- delivered via batch
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0].observations[0].observation_type == "diarization_speaker_turn"


def test_run_once_happy_path_social_export() -> None:
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-z", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    export = build_whatsapp_export(["01/01/26, 10:00 - Alice: hello there"])
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/plain", original_filename="chat.txt", data=export)
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"
    assert len(client.submit_calls) == 1  # Scenario 31: exactly one terminal result
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.observations == []  # type: ignore[attr-defined] -- delivered via batch
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0].observations[0].observation_type == "chat_message"


def test_run_once_social_export_also_emits_mentioned_identifiers() -> None:
    """Task #81: phone/email/url/handle mentions ride the same batch, with
    distinct observation_ids from the parent `chat_message` despite the two
    kinds of observation sharing the exact same message locator."""
    job = make_job(processor_name=WHATSAPP_EXPORT_V1.name, source_type=SourceType.WHATSAPP_CHAT)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-z2", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    export = build_whatsapp_export(
        ["01/01/26, 10:00 - Alice: call me at 9876543210 or alice@example.com"]
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="text/plain", original_filename="chat.txt", data=export)
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"
    observations = client.batch_calls[0].observations
    types = [o.observation_type for o in observations]
    assert types == ["chat_message", "phone_number", "email_address"]
    assert len({o.observation_id for o in observations}) == len(observations)


# --- multi-processor claim loop / no-job -------------------------------------


def test_run_once_tries_each_processor_until_one_has_work() -> None:
    job = make_job(processor_name=GENERIC_SOCIAL_JSON_V1.name, source_type=SourceType.CHAT)
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in range(len(SUPPORTED_PROCESSORS) - 1)
        ]
        + [ClaimResult(job=job, claim_token="tok-last", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    export = build_generic_json_export(records=[{"sender": "alice", "text": "hi"}])
    resolver = StaticInputResolver(
        ResolvedInput(content_type="application/json", original_filename="export.json", data=export)
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert client._claim_calls == len(SUPPORTED_PROCESSORS)
    assert outcome.claimed is True
    assert outcome.result_status == "succeeded"


def test_run_once_returns_claimed_false_when_no_processor_has_work() -> None:
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in SUPPORTED_PROCESSORS
        ]
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="audio/wav", original_filename="x.wav", data=b"")
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome == RunOnceOutcome(claimed=False, job_id=None, result_status=None)


# --- input-resolution gap -----------------------------------------------------


def test_run_once_submits_deferred_when_input_resolution_unavailable() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="deferred"),
    )

    outcome = run_once(client=client, input_resolver=_RaisingResolver())

    assert outcome.result_status == "deferred"
    assert outcome.deferred_reason is not None
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.status is WorkerStatus.DEFERRED  # type: ignore[attr-defined]
    assert submitted_result.checkpoint == CHECKPOINT_INPUT_RESOLUTION_UNAVAILABLE  # type: ignore[attr-defined]


# --- integrity mismatch -------------------------------------------------------


def test_run_once_submits_failed_when_resolved_bytes_fail_sha256_check() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="failed"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="audio/wav",
            original_filename="a.wav",
            data=b"not the bytes that were hashed",
            expected_sha256="0" * 64,
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "failed"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.error.code == "evidence_integrity_mismatch"  # type: ignore[attr-defined]


def test_run_once_proceeds_to_dispatch_when_sha256_matches() -> None:
    """Scenario 1 (SHA-verified leg): a genuinely matching SHA-256 lets the
    resolved bytes reach dispatch/extraction, never rejected as a mismatch."""
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-sha-ok", lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    wav_bytes = build_wav_bytes(duration_seconds=0.1)
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="audio/wav",
            original_filename="a.wav",
            data=wav_bytes,
            expected_sha256=hashlib.sha256(wav_bytes).hexdigest(),
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "succeeded"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.error is None  # type: ignore[attr-defined]
    assert client.batch_calls[0].observations[0].observation_type == "audio_metadata"


# --- malformed interchange payload --------------------------------------------


def test_run_once_submits_failed_for_malformed_transcript_interchange_json() -> None:
    """The payload-build step (unique to this worker: process_job never sees raw bytes)."""
    job = make_job(processor_name=TRANSCRIPT_IMPORT_V1.name, source_type=SourceType.AUDIO)
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token="tok-x", lease_expires_at=None)],
        submit_ack=_ack(job.job_id, status="failed"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(
            content_type="application/json", original_filename="bad.json", data=b"not json"
        )
    )

    outcome = run_once(client=client, input_resolver=resolver)

    assert outcome.result_status == "failed"
    submitted_result = client.submit_calls[0][2]
    assert submitted_result.error.code == "malformed_json_payload"  # type: ignore[attr-defined]
    assert submitted_result.observations == []  # type: ignore[attr-defined]


# --- submit failure / secret safety -------------------------------------------


def test_run_once_submit_failure_propagates_without_leaking_claim_token() -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    secret_token = "super-secret-claim-token"  # noqa: S105
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token=secret_token, lease_expires_at=None)],
        submit_exception=WorkerApiError("result submission rejected"),
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="audio/wav", original_filename="a.wav", data=build_wav_bytes())
    )

    with pytest.raises(WorkerApiError) as exc_info:
        run_once(client=client, input_resolver=resolver)

    assert secret_token not in str(exc_info.value)


def test_run_once_never_logs_the_claim_token(caplog: pytest.LogCaptureFixture) -> None:
    job = make_job(processor_name=AUDIO_METADATA_V1.name, source_type=SourceType.AUDIO)
    secret_token = "super-secret-claim-token-for-logging-check"  # noqa: S105
    client = _FakeClient(
        claim_responses=[ClaimResult(job=job, claim_token=secret_token, lease_expires_at=None)],
        submit_ack=_ack(job.job_id),
    )
    resolver = StaticInputResolver(
        ResolvedInput(content_type="audio/wav", original_filename="a.wav", data=build_wav_bytes())
    )

    with caplog.at_level("DEBUG"):
        run_once(client=client, input_resolver=resolver)

    assert secret_token not in caplog.text


# --- main() -------------------------------------------------------------


def test_main_returns_1_when_worker_token_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TOKEN", "")
    assert main(["--once"]) == 1


def test_main_returns_0_and_exits_cleanly_when_no_job_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        claim_responses=[
            ClaimResult(job=None, claim_token=None, lease_expires_at=None)
            for _ in SUPPORTED_PROCESSORS
        ]
    )
    monkeypatch.setattr(
        "app.modules.communication_processing.worker._build_client",
        lambda settings: client,  # noqa: ARG005
    )

    assert main(["--once"]) == 0
    assert client._claim_calls == len(SUPPORTED_PROCESSORS)
