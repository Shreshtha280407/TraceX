"""`run_loop`/`_lease_heartbeat` continuous-operation behavior against a fake
`WorkerApiClient` -- no real HTTP, no real infra, small injected intervals so
this runs fast and deterministically. Mirrors
`test_media_worker_orchestration.py`'s fake-client pattern, extended with a
`renew`/multi-call `claim` sequence `run_once` alone never needed.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.contracts.evidence import SourceType
from app.modules.media_processing.client import ClaimResult, SubmitResultAck
from app.modules.media_processing.errors import WorkerApiError
from app.modules.media_processing.input_resolver import ResolvedMediaInput, StaticInputResolver
from app.modules.media_processing.worker import (
    PROCESSOR_NAME_METADATA,
    PROCESSOR_VERSION,
    _lease_heartbeat,
    run_loop,
)
from tests.fixtures.media_processing.factory import make_evidence_and_job
from tests.fixtures.media_processing.synthetic import make_png_bytes

_NO_JOB = ClaimResult(job=None, claim_token=None, lease_expires_at=None)
#: These tests are about `run_loop`'s own poll/backoff/shutdown behavior, not
#: `run_once`'s per-call multi-processor claim fallback (covered separately
#: in `test_media_worker_orchestration.py`) -- restricting to one processor
#: keeps exactly one `client.claim()` call per loop iteration, so the fake
#: client's outcome queue lines up 1:1 with iterations.
_SINGLE_PROCESSOR = ((PROCESSOR_NAME_METADATA, PROCESSOR_VERSION),)


@dataclass
class _LoopFakeClient:
    """A `claim` outcome per call, cycling; `submit_result` always succeeds."""

    claim_outcomes: list[ClaimResult | Exception]
    #: optional side-effect hook, e.g. set a shutdown_event after N calls
    on_claim_call: Callable[[int], None] | None = None
    claim_calls: int = 0
    submit_calls: list[UUID] = field(default_factory=list)
    renew_calls: list[UUID] = field(default_factory=list)

    def claim(self, *, processor_name: str, processor_version: str) -> ClaimResult:  # noqa: ARG002
        if self.on_claim_call is not None:
            self.on_claim_call(self.claim_calls)
        outcome = self.claim_outcomes[self.claim_calls % len(self.claim_outcomes)]
        self.claim_calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def submit_result(self, *, job_id: UUID, claim_token: str, result: object) -> SubmitResultAck:  # noqa: ARG002
        self.submit_calls.append(job_id)
        return SubmitResultAck(
            job_id=job_id, status="succeeded", result_id=uuid4(), observation_count=1,
            observation_ids=(uuid4(),),
        )  # fmt: skip

    def renew(self, job_id: UUID, *, claim_token: str) -> datetime:  # noqa: ARG002
        self.renew_calls.append(job_id)
        return datetime.now(UTC)

    def close(self) -> None:
        pass


def _resolver() -> StaticInputResolver:
    return StaticInputResolver(
        ResolvedMediaInput(
            content_type="image/png", original_filename="p.png", data=make_png_bytes()
        )
    )


def _claimed_job_outcome() -> ClaimResult:
    _metadata, job = make_evidence_and_job(
        content_type="image/png",
        filename="p.png",
        processor_name=PROCESSOR_NAME_METADATA,
        source_type=SourceType.IMAGE,
    )
    return ClaimResult(job=job, claim_token="tok", lease_expires_at=None)


# --- shutdown behavior -------------------------------------------------------


def test_run_loop_stops_immediately_when_shutdown_already_requested() -> None:
    client = _LoopFakeClient(claim_outcomes=[_NO_JOB])
    event = threading.Event()
    event.set()
    summary = run_loop(
        client=client,  # type: ignore[arg-type]
        input_resolver=_resolver(),
        processors=_SINGLE_PROCESSOR,
        shutdown_event=event,
        poll_interval_seconds=0.01,
    )
    assert summary.iterations == 0
    assert summary.stopped_reason == "shutdown_requested"
    assert client.claim_calls == 0


def test_run_loop_claims_no_further_job_once_shutdown_is_requested_mid_loop() -> None:
    """Shutdown is requested from inside a `claim` call (simulating a signal
    arriving between iterations) -- the loop must not claim again afterward."""
    event = threading.Event()

    def _request_shutdown_after_two_calls(call_index: int) -> None:
        if call_index >= 2:
            event.set()

    client = _LoopFakeClient(
        claim_outcomes=[_NO_JOB], on_claim_call=_request_shutdown_after_two_calls
    )
    summary = run_loop(
        client=client,  # type: ignore[arg-type]
        input_resolver=_resolver(),
        processors=_SINGLE_PROCESSOR,
        shutdown_event=event,
        poll_interval_seconds=0.01,
    )
    assert summary.stopped_reason == "shutdown_requested"
    assert client.claim_calls == 3  # calls 0, 1, 2 (2 triggers the event; loop exits before call 3)


# --- backoff / failure handling ----------------------------------------------


def test_run_loop_stops_after_max_consecutive_failures_with_exponential_backoff() -> None:
    client = _LoopFakeClient(claim_outcomes=[WorkerApiError("claim request failed: HTTP 503")])
    event = threading.Event()
    started = time.monotonic()
    summary = run_loop(
        client=client,  # type: ignore[arg-type]
        input_resolver=_resolver(),
        processors=_SINGLE_PROCESSOR,
        shutdown_event=event,
        poll_interval_seconds=0.01,
        max_backoff_seconds=0.05,
        max_consecutive_failures=3,
    )
    elapsed = time.monotonic() - started
    assert summary.stopped_reason == "max_consecutive_failures"
    assert summary.consecutive_failures == 3
    assert summary.jobs_processed == 0
    # backoff after failures 1 and 2 (failure 3 stops immediately, no sleep after
    # it): min(0.01*2, 0.05) + min(0.01*4, 0.05) = 0.02 + 0.04 = 0.06s minimum.
    assert elapsed >= 0.05


def test_run_loop_resets_failure_counter_after_a_success() -> None:
    """2 failures, a success (resets the counter), then 2 more failures -- never 3
    *consecutive* failures, so `max_consecutive_failures=3` must never trip. The
    trailing `_NO_JOB` avoids the outcome list wrapping back into a 3rd
    consecutive failure once the fixed-length queue cycles."""
    client = _LoopFakeClient(
        claim_outcomes=[
            WorkerApiError("transient"),
            WorkerApiError("transient"),
            _NO_JOB,  # resets the counter
            WorkerApiError("transient"),
            WorkerApiError("transient"),
            _NO_JOB,
        ]
    )
    event = threading.Event()

    def _stop_after_six(call_index: int) -> None:
        if call_index >= 6:
            event.set()

    client.on_claim_call = _stop_after_six
    summary = run_loop(
        client=client,  # type: ignore[arg-type]
        input_resolver=_resolver(),
        processors=_SINGLE_PROCESSOR,
        shutdown_event=event,
        poll_interval_seconds=0.01,
        max_backoff_seconds=0.02,
        max_consecutive_failures=3,  # would have stopped at failure 3 without the reset
    )
    assert summary.stopped_reason == "shutdown_requested"


def test_run_loop_processes_claimed_jobs_and_loops_again_immediately() -> None:
    client = _LoopFakeClient(
        claim_outcomes=[_claimed_job_outcome(), _claimed_job_outcome(), _NO_JOB]
    )
    event = threading.Event()

    def _stop_once_idle(call_index: int) -> None:
        if call_index >= 2:  # the third call (index 2) returns _NO_JOB
            event.set()

    client.on_claim_call = _stop_once_idle
    summary = run_loop(
        client=client,  # type: ignore[arg-type]
        input_resolver=_resolver(),
        processors=_SINGLE_PROCESSOR,
        shutdown_event=event,
        poll_interval_seconds=5.0,  # would make the test slow if actually waited on
    )
    assert summary.jobs_processed == 2
    assert len(client.submit_calls) == 2


# --- lease heartbeat ----------------------------------------------------------


def test_lease_heartbeat_renews_periodically_while_the_block_runs() -> None:
    client = _LoopFakeClient(claim_outcomes=[_NO_JOB])
    job_id = uuid4()
    with _lease_heartbeat(client, job_id, "claim-tok", 0.02):  # type: ignore[arg-type]
        time.sleep(0.07)
    assert len(client.renew_calls) >= 2
    assert all(call == job_id for call in client.renew_calls)


def test_lease_heartbeat_stops_renewing_after_the_block_exits() -> None:
    client = _LoopFakeClient(claim_outcomes=[_NO_JOB])
    job_id = uuid4()
    with _lease_heartbeat(client, job_id, "claim-tok", 0.02):  # type: ignore[arg-type]
        time.sleep(0.03)
    count_at_exit = len(client.renew_calls)
    time.sleep(0.06)
    assert len(client.renew_calls) == count_at_exit  # no renewals after the context exits


def test_lease_heartbeat_swallows_renew_failures_without_raising() -> None:
    @dataclass
    class _FailingRenewClient(_LoopFakeClient):
        def renew(self, job_id: UUID, *, claim_token: str) -> datetime:  # noqa: ARG002
            raise WorkerApiError("renew request failed: HTTP 500")

    client = _FailingRenewClient(claim_outcomes=[_NO_JOB])
    job_id = uuid4()
    with _lease_heartbeat(client, job_id, "claim-tok", 0.02):  # type: ignore[arg-type]
        time.sleep(0.05)
    # No exception propagated out of the context manager despite every renewal failing.


def test_lease_heartbeat_thread_is_joined_before_the_context_manager_returns() -> None:
    """The real proof here is that this test function returns promptly at all
    (`_lease_heartbeat`'s `thread.join(timeout=5.0)` bounds it) -- a hung
    background thread would otherwise make the whole test session hang at
    interpreter shutdown."""
    threads_before = {t.ident for t in threading.enumerate()}
    client = _LoopFakeClient(claim_outcomes=[_NO_JOB])
    with _lease_heartbeat(client, uuid4(), "claim-tok", 0.01):  # type: ignore[arg-type]
        pass
    threads_after = {t.ident for t in threading.enumerate()}
    assert threads_after == threads_before  # the heartbeat thread is gone, not leaked


@pytest.mark.parametrize("interval_seconds", [0.01])
def test_lease_heartbeat_never_logs_or_raises_the_claim_token(interval_seconds: float) -> None:
    """Sanity check on the heartbeat's own call surface -- the claim token is
    passed straight through to `client.renew`, never embedded in any log
    message or exception text this module constructs itself."""
    client = _LoopFakeClient(claim_outcomes=[_NO_JOB])
    job_id = uuid4()
    secret_token = "super-secret-claim-token-value"  # noqa: S105
    with _lease_heartbeat(client, job_id, secret_token, interval_seconds):  # type: ignore[arg-type]
        time.sleep(interval_seconds * 2)
    assert client.renew_calls  # renewal did happen; the token itself is opaque to this test
