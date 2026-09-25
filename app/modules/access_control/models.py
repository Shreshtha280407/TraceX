"""Typed internal models, enums, and the role/action matrix for access control.

None of these are public `app/contracts/` contracts -- they exist only
inside `app/modules/access_control/`. API request/response shapes live here
too, kept internal to this module per the phase brief ("typed request/
response models internal to this module").
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.modules.access_control.password import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH


class AccessControlModel(BaseModel):
    """Base class for internal access-control models: immutable, no stray fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# `pydantic.EmailStr` needs the optional `email-validator` package, which is
# not on this phase's approved-dependency list (only PyJWT and
# pwdlib[argon2] may be added). This is a small, deliberately conservative
# hand-rolled check instead: reject obviously-malformed input, accept
# anything shaped like `local@domain.tld`. It is not a full RFC 5321
# validator -- that tradeoff is documented in
# `docs/decisions/ADR-003-authentication-and-case-scoped-access-control.md`.
_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_MAX_EMAIL_LENGTH = 320  # RFC 5321 practical upper bound


def normalize_email(raw_email: str) -> str:
    """Deterministic, documented email normalization: trim, then lowercase.

    Applied identically at registration and at every login lookup, so
    `users.email_normalized` uniqueness and lookups are consistent
    regardless of how a caller cased or padded the address. This treats the
    whole address (including the local part) as case-insensitive -- not
    strictly RFC 5321 (which allows a case-sensitive local part), but the
    pragmatic behavior every major mail provider follows in practice, and
    the only sane choice for a *unique* column.

    Raises `ValueError` (caught and re-raised as `ValidationError` by
    callers) for anything that doesn't look like an email address at all.
    """
    candidate = raw_email.strip().lower()
    if not candidate or len(candidate) > _MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.match(candidate):
        raise ValueError("invalid email address")
    return candidate


class SystemRole(StrEnum):
    """A user's deployment-wide (not case-scoped) capability.

    `None` on `UserRecord.system_role` is the overwhelmingly common case
    (an ordinary user with no capability beyond their per-case
    memberships). The only defined value today is `ADMIN`, which gates
    `POST /api/v1/admin/users` (see `dependencies.require_system_admin`) --
    it is deliberately not a case role and never appears in `ROLE_ACTIONS`.
    """

    ADMIN = "admin"


class CaseRole(StrEnum):
    """A user's role within one specific case membership."""

    CASE_OWNER = "case_owner"
    CASE_MANAGER = "case_manager"
    INVESTIGATOR = "investigator"
    ANALYST = "analyst"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class ClearanceLevel(StrEnum):
    """Ordered clearance levels. Order matters -- see `CLEARANCE_RANK` below."""

    RESTRICTED = "restricted"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


#: Higher rank = more clearance. A member's clearance must rank >= a
#: resource's classification for access to be granted (see `policy.py`).
CLEARANCE_RANK: dict[ClearanceLevel, int] = {
    ClearanceLevel.RESTRICTED: 0,
    ClearanceLevel.CONFIDENTIAL: 1,
    ClearanceLevel.SECRET: 2,
}


def clearance_satisfies(member_clearance: ClearanceLevel, required: ClearanceLevel) -> bool:
    """True only if `member_clearance` ranks at or above `required`."""
    return CLEARANCE_RANK[member_clearance] >= CLEARANCE_RANK[required]


class CaseAction(StrEnum):
    """An action a caller may attempt against a case-scoped resource."""

    CASE_READ = "case_read"
    CASE_MANAGE = "case_manage"
    MEMBER_MANAGE = "member_manage"
    EVIDENCE_READ = "evidence_read"
    EVIDENCE_WRITE = "evidence_write"
    GRAPH_READ = "graph_read"
    INTEGRITY_READ = "integrity_read"
    INTEGRITY_VERIFY = "integrity_verify"
    INTEGRITY_EXPORT = "integrity_export"
    REVIEW_DECIDE = "review_decide"
    HYPOTHESIS_PROPOSE = "hypothesis_propose"
    EXPORT_CASE_DATA = "export_case_data"
    #: Gap-Closure WP-4 (G3) additions.
    CASE_NOTE_WRITE = "case_note_write"
    #: Read every note, not only one's own -- see `notes_service.py`'s
    #: "author can always read their own note" override, checked in the
    #: service layer, not this role matrix.
    CASE_NOTE_READ_ALL = "case_note_read_all"


#: The role -> action matrix. Default deny: an action not listed for a role
#: is not permitted, full stop -- see `docs/architecture/access-control-v1.md`
#: for the human-readable table this encodes.
ROLE_ACTIONS: dict[CaseRole, frozenset[CaseAction]] = {
    CaseRole.CASE_OWNER: frozenset(CaseAction),
    CaseRole.CASE_MANAGER: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.CASE_MANAGE,
            CaseAction.MEMBER_MANAGE,
            CaseAction.EVIDENCE_READ,
            CaseAction.EVIDENCE_WRITE,
            CaseAction.GRAPH_READ,
            CaseAction.INTEGRITY_READ,
            CaseAction.INTEGRITY_VERIFY,
            CaseAction.INTEGRITY_EXPORT,
            CaseAction.REVIEW_DECIDE,
            CaseAction.HYPOTHESIS_PROPOSE,
            CaseAction.EXPORT_CASE_DATA,
            CaseAction.CASE_NOTE_WRITE,
            CaseAction.CASE_NOTE_READ_ALL,
        }
    ),
    CaseRole.INVESTIGATOR: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.EVIDENCE_WRITE,
            CaseAction.GRAPH_READ,
            CaseAction.INTEGRITY_READ,
            CaseAction.INTEGRITY_VERIFY,
            CaseAction.HYPOTHESIS_PROPOSE,
            CaseAction.CASE_NOTE_WRITE,
        }
    ),
    CaseRole.ANALYST: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.GRAPH_READ,
            CaseAction.INTEGRITY_READ,
            CaseAction.INTEGRITY_VERIFY,
        }
    ),
    CaseRole.REVIEWER: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.GRAPH_READ,
            CaseAction.INTEGRITY_READ,
            CaseAction.INTEGRITY_VERIFY,
            CaseAction.REVIEW_DECIDE,
            CaseAction.CASE_NOTE_WRITE,
            CaseAction.CASE_NOTE_READ_ALL,
        }
    ),
    CaseRole.VIEWER: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.GRAPH_READ,
        }
    ),
}


class CaseStatus(StrEnum):
    """Lifecycle status of the minimal `cases` access-control anchor row."""

    OPEN = "open"
    CLOSED = "closed"
    ARCHIVED = "archived"


class AuditOutcome(StrEnum):
    """Result recorded on a `security_audit_events` row."""

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class TokenType(StrEnum):
    """`typ` claim value on a token this module issues."""

    ACCESS_V1 = "access_v1"
    #: A short-lived, single-purpose token proving "password already
    #: verified for this user" -- carries no session, only a green light to
    #: attempt `POST /auth/mfa/login-verify`. See `docs/decisions/ADR-033-
    #: frontend-totp-mfa.md`.
    MFA_PENDING = "mfa_pending"


# --- Persistence-layer records (never returned directly from an API) -------


class UserRecord(AccessControlModel):
    """A full `users` row, including the password hash. Internal use only."""

    user_id: UUID
    email_normalized: str
    display_name: str
    password_hash: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    system_role: SystemRole | None = None
    #: True immediately after admin provisioning or an admin-triggered
    #: credential reset; cleared only by a successful `change_password`
    #: call. A UI-enforced ceremony, not a server-side hard gate on every
    #: other endpoint -- see ADR-033.
    must_change_password: bool = False
    #: The current (possibly not-yet-confirmed) base32 TOTP shared secret,
    #: or `None` before enrollment has ever started. Never returned from
    #: any API response -- see `PublicUser`, which deliberately omits it.
    totp_secret: str | None = None
    #: True only after the investigator has confirmed a real code against
    #: `totp_secret` (`confirm_mfa_enrollment`). While `False`, `login`
    #: never issues an MFA challenge -- there would be nothing to verify
    #: against yet.
    totp_enabled: bool = False


class CaseRecord(AccessControlModel):
    """A full `cases` row."""

    case_id: UUID
    case_reference: str
    classification: ClearanceLevel
    status: CaseStatus
    created_at: datetime


class CaseMembershipRecord(AccessControlModel):
    """A full `case_memberships` row."""

    membership_id: UUID
    case_id: UUID
    user_id: UUID
    role: CaseRole
    clearance: ClearanceLevel
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SessionRecord(AccessControlModel):
    """A full `auth_sessions` row. `refresh_token_hash` never leaves this module."""

    session_id: UUID
    user_id: UUID
    refresh_token_hash: str
    token_family_id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by_session_id: UUID | None
    last_used_at: datetime | None


class WorkerCredentialStatus(StrEnum):
    """Lifecycle state of a provisioned per-worker service credential."""

    ACTIVE = "active"
    REVOKED = "revoked"


class WorkerCredentialRecord(AccessControlModel):
    """A full `worker_credentials` row -- never the plaintext token.

    `credential_digest` is `HMAC-SHA256(pepper, token)` (or a plain SHA-256
    digest when no pepper is configured -- see `worker_credentials.py`);
    the raw token exists only transiently, in the process memory of the CLI
    that generated it and the worker process it was handed to. Rotation
    never mutates `worker_id` -- only `credential_digest`/`rotated_at`
    change, so a job already bound to this `worker_id`
    (`worker_jobs.claimed_by_worker_id`) stays bound across a rotation.
    """

    worker_id: UUID
    display_name: str
    status: WorkerCredentialStatus
    allowed_processor_names: tuple[str, ...]
    credential_digest: str
    created_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    last_seen_at: datetime | None = None


class WorkerLivenessStatus(StrEnum):
    """Gap-Closure WP-6 (G16): a worker credential's *observed* liveness,
    derived from `last_seen_at` -- never a claim about whether the worker
    process itself is currently running, only "when this credential was
    last used to successfully authenticate a request"."""

    ACTIVE = "active"
    STALE = "stale"
    NEVER_SEEN = "never_seen"


def worker_liveness_status(
    credential: WorkerCredentialRecord, *, now: datetime, stale_seconds: int
) -> WorkerLivenessStatus:
    if credential.last_seen_at is None:
        return WorkerLivenessStatus.NEVER_SEEN
    age_seconds = (now - credential.last_seen_at).total_seconds()
    return (
        WorkerLivenessStatus.ACTIVE if age_seconds <= stale_seconds else WorkerLivenessStatus.STALE
    )


class WorkerLivenessView(AccessControlModel):
    """Safe, admin-only read shape: never the credential digest."""

    worker_id: UUID
    display_name: str
    status: WorkerCredentialStatus
    allowed_processor_names: tuple[str, ...]
    last_seen_at: datetime | None
    liveness: WorkerLivenessStatus


class WorkerLivenessListResponse(AccessControlModel):
    items: tuple[WorkerLivenessView, ...]


class SecurityAuditEventRecord(AccessControlModel):
    """A full `security_audit_events` row -- safe security telemetry only.

    Never carries a password, token, evidence/CDR/financial value, full IP
    address, or stack trace -- see `audit.py` and
    `docs/architecture/security-boundaries-v1.md`.
    """

    event_id: UUID
    occurred_at: datetime
    event_type: str
    outcome: AuditOutcome
    request_id: str | None
    user_id_nullable: UUID | None
    case_id_nullable: UUID | None
    ip_hash_or_safe_network_marker: str | None
    metadata_safe_json: dict[str, JsonValue]


# --- Safe, API-facing shapes -------------------------------------------------


class PublicUser(AccessControlModel):
    """The only user representation ever returned from the API. Never `totp_secret`."""

    user_id: UUID
    email_normalized: str
    display_name: str
    is_active: bool
    created_at: datetime
    system_role: SystemRole | None = None
    must_change_password: bool = False
    totp_enabled: bool = False


class PublicUserListResponse(AccessControlModel):
    """`GET /api/v1/admin/users`'s response shape. Admin-only account listing."""

    items: tuple[PublicUser, ...]


class CaseMembershipView(AccessControlModel):
    """One case membership, as shown to the member themselves via `/me`."""

    case_id: UUID
    role: CaseRole
    clearance: ClearanceLevel
    is_active: bool


class AdminProvisionUserRequest(AccessControlModel):
    """Admin-only user provisioning. No public self-registration exists (G5).

    `password` bounds enforce the real MVP policy (see `password.py`). A
    request-validation failure on any field of this model -- including this
    one -- is rendered by `app.core.errors.request_validation_exception_handler`,
    which never echoes the submitted value, so enforcing the policy here
    (rather than only deeper in `service.py`) cannot leak the password.
    `system_role` defaults to `None` (an ordinary user); only an existing
    admin can set it to `ADMIN`, minting another admin.
    """

    email: str
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=200)
    system_role: SystemRole | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class CaseCreateRequest(AccessControlModel):
    """`POST /api/v1/cases` body. The creator becomes the case's `CASE_OWNER`."""

    case_reference: str = Field(min_length=1, max_length=200)
    classification: ClearanceLevel


class CaseView(AccessControlModel):
    """A safe, API-facing case shape -- never the raw `CaseRecord` row directly."""

    case_id: UUID
    case_reference: str
    classification: ClearanceLevel
    status: CaseStatus
    created_at: datetime


class CaseStatusView(AccessControlModel):
    case_id: UUID
    status: CaseStatus


class CaseMemberAddRequest(AccessControlModel):
    """`POST /api/v1/cases/{case_id}/members` body."""

    user_id: UUID
    role: CaseRole
    clearance: ClearanceLevel


class CaseMemberView(AccessControlModel):
    user_id: UUID
    role: CaseRole
    clearance: ClearanceLevel
    is_active: bool


class CaseAuditEventListResponse(AccessControlModel):
    """Gap-Closure WP-4 (G7): `GET /cases/{id}/audit`'s response shape.
    Wraps `SecurityAuditEventRecord` directly -- already safe by that
    model's own contract (never a password/token/evidence value)."""

    items: tuple[SecurityAuditEventRecord, ...]


class LoginRequest(AccessControlModel):
    """`password` here only enforces a DoS-guard upper bound, not the registration policy.

    A too-short login attempt must fail exactly like a wrong password --
    the same generic denial (see `AuthenticationError`) -- not a distinct
    422, which would let a caller learn about password-shape requirements
    without a valid account.
    """

    email: str
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class RefreshRequest(AccessControlModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(AccessControlModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPairResponse(AccessControlModel):
    """`POST /auth/login`'s response.

    Two disjoint shapes in one model (additive, not a breaking rename --
    every pre-MFA caller of this endpoint keeps working against the
    `mfa_required=False` shape unchanged):

    - `mfa_required=False` (the only shape this endpoint ever returned
      before MFA existed): `access_token`/`refresh_token`/`expires_in` are
      set, `mfa_token` is `None`. A real session already exists.
    - `mfa_required=True`: password was correct and the account has TOTP
      enabled, but no session exists yet. `mfa_token` is set;
      `access_token`/`refresh_token`/`expires_in` are `None`. The caller
      must present a code to `POST /auth/mfa/login-verify` with this
      `mfa_token` to actually obtain a session.
    """

    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None
    mfa_required: bool = False
    mfa_token: str | None = None


class MeResponse(AccessControlModel):
    user: PublicUser
    case_memberships: tuple[CaseMembershipView, ...]


class ChangePasswordRequest(AccessControlModel):
    """`POST /api/v1/auth/change-password` body. Requires the current password.

    `current_password` is bounded the same generous way `LoginRequest.password`
    is (a DoS guard only) -- it is verified against the stored hash, not
    subject to the registration policy. `new_password` enforces the real
    policy, exactly like `AdminProvisionUserRequest.password`.
    """

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class MfaEnrollResponse(AccessControlModel):
    """`POST /api/v1/auth/mfa/enroll`'s response -- shown to the investigator once.

    `secret` is included alongside `provisioning_uri` for an authenticator
    app that cannot scan a QR code (manual entry) -- the same information
    already embedded in the URI, never a second secret.
    """

    secret: str
    provisioning_uri: str


class MfaVerifyRequest(AccessControlModel):
    """`POST /api/v1/auth/mfa/verify` body -- confirms enrollment with one real code."""

    code: str = Field(min_length=6, max_length=6)


class MfaLoginVerifyRequest(AccessControlModel):
    """`POST /api/v1/auth/mfa/login-verify` body: the challenge from `login` plus a live code."""

    mfa_token: str = Field(min_length=1, max_length=2048)
    code: str = Field(min_length=6, max_length=6)


class AdminResetCredentialsResponse(AccessControlModel):
    """`POST /api/v1/admin/users/{user_id}/reset-credentials`'s response.

    Shown to the admin once, exactly like `AdminProvisionUserRequest`'s
    initial password -- the investigator must re-enroll MFA from scratch
    (the old `totp_secret` is cleared), and `must_change_password` is set
    again on the affected account.
    """

    temporary_password: str


# --- Token/session working types --------------------------------------------


class AccessTokenClaims(AccessControlModel):
    """Decoded, validated access-token claims."""

    sub: UUID
    sid: UUID
    iat: datetime
    exp: datetime
    iss: str
    aud: str
    typ: TokenType


class MfaChallengeClaims(AccessControlModel):
    """Decoded, validated MFA-challenge-token claims. No `sid`: no session exists yet."""

    sub: UUID
    iat: datetime
    exp: datetime
    iss: str
    aud: str
    typ: TokenType


class AuthenticatedPrincipal(AccessControlModel):
    """The result of successfully validating an access token against a live session."""

    user_id: UUID
    session_id: UUID


class AuthorizedCasePrincipal(AccessControlModel):
    """Typed allow decision for a principal, requested case, and required action."""

    principal: AuthenticatedPrincipal
    case_id: UUID
    action: CaseAction
    membership: CaseMembershipRecord
    allowed: Literal[True] = True
