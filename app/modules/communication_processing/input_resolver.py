"""Worker input-access boundary: bytes + metadata for a claimed job.

`worker.run_once` needs the claimed job's raw evidence bytes plus
`content_type`/`original_filename` (to decide, together with
`job.processor_name`, which typed `InputPayload` variant to build --
`AudioMetadataInput`/`TranscriptImportInput`/`DiarizationImportInput`/
`SocialExportInput`, see `models.py`) -- but `WorkerJobV1` (the only thing
`/claim` returns) carries neither.

Mirrors `app.modules.structured_processing.input_resolver` exactly (same
`WorkerInputResolver` Protocol, same `StaticInputResolver`/`LiveInputResolver`
split) rather than importing it directly: this module owns its own copy so
it stays independently buildable, matching the existing repository-wide
convention every sibling processing module already follows (no cross-module
import except shared `app.contracts`).

`LiveInputResolver` delegates to `client.WorkerApiClient.fetch_input`,
which calls `GET /api/v1/internal/worker-jobs/{job_id}/input` (Nipun's
claim-token-bound worker evidence-delivery endpoint -- see
`docs/architecture/evidence-lifecycle.md`'s "Worker evidence delivery")
and raises `InputResolutionUnavailableError` if that endpoint isn't
reachable, rather than falling back to a direct object-storage read.
`StaticInputResolver` is the in-memory test double every unit test uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.contracts.worker import WorkerJobV1


@dataclass(frozen=True)
class ResolvedInput:
    """Everything `worker.run_once` needs from a claimed job's evidence, beyond `WorkerJobV1`.

    `content_type`/`original_filename`/`data` are what `worker.py` uses to
    build the correct typed `InputPayload` for `process_job`.
    `expected_sha256` is additional: the evidence's recorded hash, when the
    resolver can supply one, so `run_once` can verify the bytes it actually
    received before ever parsing them (`None` for a resolver that can't
    provide it, e.g. a bare `StaticInputResolver` built without one --
    verification is then simply skipped, never treated as a mismatch).
    """

    content_type: str
    original_filename: str
    data: bytes
    expected_sha256: str | None = None


@runtime_checkable
class WorkerInputResolver(Protocol):
    """Resolves a claimed `WorkerJobV1` to its evidence content_type/filename/bytes.

    Implementations must never require -- or accept -- direct PostgreSQL,
    Neo4j, Redis, or MinIO credentials; the only inputs are the claimed job
    and the worker's already-established API credentials (the claim token),
    exactly like every other call this worker makes.
    """

    def resolve(self, job: WorkerJobV1, *, claim_token: str) -> ResolvedInput: ...


@dataclass(frozen=True)
class StaticInputResolver:
    """An in-memory `WorkerInputResolver` for tests -- no HTTP, no credentials.

    Mirrors `structured_processing.models.StaticBytesResolver`'s role: tests
    construct the resolved input directly and never need a real API call.
    """

    resolved: ResolvedInput

    def resolve(self, job: WorkerJobV1, *, claim_token: str) -> ResolvedInput:  # noqa: ARG002
        return self.resolved


@runtime_checkable
class _InputFetcher(Protocol):
    """The narrow slice of `client.WorkerApiClient` `LiveInputResolver` depends on.

    A Protocol, not a direct import of `WorkerApiClient`, so this module
    and `client.py` don't need to import each other.
    """

    def fetch_input(self, job_id: UUID, *, claim_token: str) -> ResolvedInput: ...


@dataclass(frozen=True)
class LiveInputResolver:
    """Delegates to a `WorkerApiClient`'s `fetch_input` -- the real, HTTP-backed resolver.

    Raises `errors.InputResolutionUnavailableError` (propagated from
    `client`) if the endpoint isn't reachable. Never falls back to a direct
    object-storage read.
    """

    client: _InputFetcher

    def resolve(self, job: WorkerJobV1, *, claim_token: str) -> ResolvedInput:
        return self.client.fetch_input(job.job_id, claim_token=claim_token)
