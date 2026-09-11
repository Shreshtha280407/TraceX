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
