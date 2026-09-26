"""Gap-Closure WP-6 (G8): the canonical catalog of every `event_type` value
`record_audit_event`/`record_audit_event_safely` are ever called with.

Before this WP, `event_type` was an untyped free-text string (see
`audit.py`) -- every call site chose its own literal with no central
registry, no way for an operator or auditor to answer "what are all the
possible security-audit event types this system can emit," and no
protection against a typo silently creating a new, undocumented event
type. `AuditEventType` is that registry.

Deliberately **not** a change to `record_audit_event`'s signature (still
plain `str`, unchanged, zero call-site risk across ~29 existing call sites
in modules outside this one) -- `tests/unit/access_control/
test_audit_event_catalog.py` is a static test that greps every literal
`event_type` keyword argument in `app/` and asserts it is a member of
this enum, and that every member here is actually used somewhere. That
test is
this catalog's real enforcement mechanism: it fails on drift in either
direction (an uncatalogued literal, or a catalogued-but-dead entry).

Excludes `EventV1.event_type` values (`cdr_call`, `financial_transaction`,
`meeting_candidate`, `message`, `sighting`, `speech_segment`, defined in
`app/modules/graph/mapping.py`) -- a different concept entirely (a
domain/real-world event kind projected into the graph, not a security
audit event).
"""

from __future__ import annotations

from enum import StrEnum


class AuditEventType(StrEnum):
    """Every `event_type` value used across the codebase as of Gap-Closure
    WP-6. See each module's own call site for exactly when it fires --
    this enum is the enumeration, not a re-description of the trigger
    conditions (which would drift independently of the code itself)."""

    # --- app/modules/access_control/api.py, dependencies.py -----------------
    ADMIN_ACCESS_DENIED = "admin.access_denied"
    ADMIN_BOOTSTRAP_CREATE_ADMIN = "admin.bootstrap_create_admin"
    ADMIN_PROVISION_USER = "admin.provision_user"
    AUTH_LOGIN_FAILURE = "auth.login.failure"
    AUTH_LOGIN_RATE_LIMITED = "auth.login.rate_limited"
    AUTH_LOGIN_SUCCESS = "auth.login.success"
    AUTH_LOGOUT = "auth.logout"
    AUTH_REFRESH_DENIED = "auth.refresh.denied"
    AUTH_REFRESH_RATE_LIMITED = "auth.refresh.rate_limited"
    AUTH_REFRESH_REUSE_DETECTED = "auth.refresh.reuse_detected"
    AUTH_REFRESH_SUCCESS = "auth.refresh.success"
    AUTH_MFA_LOGIN_CHALLENGE = "auth.mfa.login_challenge"
    AUTH_MFA_LOGIN_FAILURE = "auth.mfa.login_failure"
    AUTH_MFA_LOGIN_RATE_LIMITED = "auth.mfa.login_rate_limited"
    AUTH_MFA_ENROLLED = "auth.mfa.enrolled"
    AUTH_MFA_ENROLL_FAILURE = "auth.mfa.enroll_failure"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"
    AUTH_PASSWORD_CHANGE_FAILURE = "auth.password_change_failure"
    ADMIN_RESET_CREDENTIALS = "admin.reset_credentials"
    CASE_CREATE = "case.create"
    CASE_MEMBER_ADD = "case.member_add"
    CASE_MEMBER_UPDATE = "case.member_update"
    CASE_MEMBER_DEACTIVATE = "case.member_deactivate"
    FIRST_ADMIN_SETUP = "admin.first_setup"
    CASE_ACCESS_DENIED = "case_access_denied"
    CASE_ACCESS_GRANTED = "case_access_granted"
    WORKER_AUTHENTICATION_DENIED = "worker_authentication_denied"
    WORKER_CREDENTIAL_REVOKED = "worker_credential_revoked"
    WORKER_CREDENTIAL_ROTATED = "worker_credential_rotated"
    WORKER_JOB_ACCESS_DENIED = "worker_job_access_denied"
    WORKER_JOB_LEASE_RENEWED = "worker_job_lease_renewed"
    WORKER_JOB_RECLAIMED = "worker_job_reclaimed"
    WORKER_JOB_RETRY_EXHAUSTED = "worker_job_retry_exhausted"
    WORKER_PROCESSOR_SCOPE_DENIED = "worker_processor_scope_denied"

    # --- app/modules/evidence_lifecycle/*.py --------------------------------
    EVIDENCE_REPROCESS = "evidence.reprocess"
    EVIDENCE_UPLOAD = "evidence.upload"
    WORKER_MEDIA_CHUNK_ACCEPTED = "worker.media_chunk.accepted"
    WORKER_MEDIA_MANIFEST_CREATED = "worker.media_manifest.created"
    WORKER_OBSERVATION_BATCH_ACCEPTED = "worker.observation_batch.accepted"

    # --- app/modules/integrity/api.py ---------------------------------------
    INTEGRITY_OPERATION = "integrity_operation"


__all__ = ["AuditEventType"]
