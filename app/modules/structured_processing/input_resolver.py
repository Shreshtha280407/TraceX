"""Worker input-access boundary: bytes + metadata for a claimed job.

`process_job` (`worker.py`) needs `EvidenceRecordV1.content_type`/
`.original_filename` (for `document.classifier.classify`) plus the raw
evidence bytes -- but `WorkerJobV1` (the only thing `/claim` returns)
carries neither: it has `input_object_uri` (an internal object-storage
*key*, not a dialable URL or a credential) and `source_type` (too coarse --
e.g. `document` covers PDF/DOCX/TXT, exactly the ambiguity `classify()`
exists to resolve), but no `content_type`/`original_filename` at all.

Inspected before writing this file: `app/modules/evidence_lifecycle/`
(Nipun's Phase 2/2.1) exposes no authenticated way for a claimed worker to
retrieve either the bytes or that metadata -- `internal_api.py` has only
`/claim` and `/{job_id}/result`; `MinioSourceResolver` is an *internal*,
credentialed Python object, never an HTTP endpoint; the case-scoped
`GET /api/v1/cases/{case_id}/evidence/{evidence_id}` endpoint exists but
requires human case-membership auth, which a worker does not have. See
`docs/architecture/structured-processing-worker.md`'s "Input-access
boundary" section for the precise, documented gap and the minimal endpoint
shape proposed to close it -- not implemented here, and not implemented by
altering Nipun's module unilaterally, per this task's explicit instruction.

`WorkerInputResolver` is the seam that isolates the rest of this module
from that gap: `worker.py`'s orchestration code depends on the Protocol
only, never on how bytes/metadata actually arrive. `StaticInputResolver`
is a test double (in-memory, used by every unit test); `LiveInputResolver`
delegates to `client.WorkerApiClient.fetch_input`, which calls the proposed
endpoint and raises `InputResolutionUnavailableError` -- loudly and safely
-- if that endpoint doesn't exist yet, rather than falling back to a MinIO
credential or fabricating a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.contracts.worker import WorkerJobV1


@dataclass(frozen=True)
class ResolvedInput:
    """Everything `process_job` needs from a claimed job's evidence, beyond `WorkerJobV1` itself.

    Deliberately minimal: `process_job` only ever reads
    `EvidenceRecordV1.content_type`/`.original_filename` -- this is exactly
    those two fields plus the raw bytes, not a full `EvidenceRecordV1`
    (which this module has no authenticated way to fully reconstruct --
    e.g. `uploaded_by`/`classification` are never given to a worker at all;
    see `worker.py::_shim_evidence_record`).
    """

    content_type: str
    original_filename: str
    data: bytes


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

    The simplest possible testable boundary, mirroring
    `structured_processing.models.StaticBytesResolver`'s role for
    `SourceResolver`: tests construct the resolved input directly and never
    need a real API call. This is exactly what makes the rest of this
    module "testable with an injected in-memory input resolver even if the
    live input-stream capability is not currently available."
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
    `client`) if the proposed endpoint isn't available yet. Never falls
    back to a direct object-storage read.
    """

    client: _InputFetcher

    def resolve(self, job: WorkerJobV1, *, claim_token: str) -> ResolvedInput:
        return self.client.fetch_input(job.job_id, claim_token=claim_token)
