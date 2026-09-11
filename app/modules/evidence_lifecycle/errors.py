"""Safe, typed errors for the evidence-lifecycle module.

Mirrors `app.modules.access_control.errors`: every message here is a short,
safe, human-written string -- never a driver/library exception's text, a
credential, an object key, or raw evidence content.
"""

from __future__ import annotations


class EvidenceLifecycleError(Exception):
    """Base class for all evidence-lifecycle-module errors."""


class MissingFilenameError(EvidenceLifecycleError):
    """The upload carried no (or an empty/oversized) filename."""


class UnsupportedSourceTypeError(EvidenceLifecycleError):
    """No processor is registered for the declared `source_type` in this phase."""


class UnsupportedContentTypeError(EvidenceLifecycleError):
    """`content_type` is not an accepted type for the declared `source_type`."""


class EmptyUploadError(EvidenceLifecycleError):
    """The uploaded file contained zero bytes."""


class PayloadTooLargeError(EvidenceLifecycleError):
    """The uploaded file exceeded `Settings.max_evidence_bytes`."""


class IdempotencyConflictError(EvidenceLifecycleError):
    """The same `Idempotency-Key` was reused for a different-content upload."""


class EvidenceNotFoundError(EvidenceLifecycleError):
    """No evidence record with this id exists in this case."""


class JobNotFoundError(EvidenceLifecycleError):
    """No job record with this id exists in this case."""


class StorageError(EvidenceLifecycleError):
    """Object storage write/read/delete failed.

    Never constructed with the underlying driver exception's text -- some
    S3/MinIO client errors can embed endpoint or request details.
    """


class InvalidClaimTokenError(EvidenceLifecycleError):
    """A claim token is missing, malformed, doesn't match, expired, or names an unknown job.

    Also covers a worker-identity mismatch (a valid claim token presented
    by a worker other than the one currently bound to the job via
    `WorkerJobRecord.claimed_by_worker_id`) -- deliberately the same
    generic exception as every other reason, uniformly: "job not found,"
    "wrong job," "expired lease," "token doesn't match," and "wrong worker
    identity" are all indistinguishable to the caller (see
    `docs/architecture/worker-job-lifecycle.md`'s "Worker identity"
    section), the same default-deny philosophy `access_control.errors
    .AuthenticationError` already applies to login.

    `reason` is a safe, internal-only classifier (never included in the
    HTTP response body/detail) -- callers that audit a denial (e.g.
    `internal_api.py`) read it to record a precise, safe reason code
    without changing what the client is told.
    """

    def __init__(self, message: str, *, reason: str = "invalid_claim_token") -> None:
        super().__init__(message)
        self.reason = reason


class ResultValidationError(EvidenceLifecycleError):
    """A submitted `WorkerResultV1` doesn't belong to the claimed job, or isn't terminal."""


class ResultConflictError(EvidenceLifecycleError):
    """A different result payload was already accepted for this (already-terminal) job."""
