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
    EVIDENCE_READ = "evidence_read"
    EVIDENCE_WRITE = "evidence_write"
    GRAPH_READ = "graph_read"
    REVIEW_DECIDE = "review_decide"
    EXPORT_CASE_DATA = "export_case_data"


#: The role -> action matrix. Default deny: an action not listed for a role
#: is not permitted, full stop -- see `docs/architecture/access-control-v1.md`
#: for the human-readable table this encodes.
ROLE_ACTIONS: dict[CaseRole, frozenset[CaseAction]] = {
    CaseRole.CASE_OWNER: frozenset(CaseAction),
    CaseRole.CASE_MANAGER: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.CASE_MANAGE,
            CaseAction.EVIDENCE_READ,
            CaseAction.EVIDENCE_WRITE,
            CaseAction.GRAPH_READ,
            CaseAction.REVIEW_DECIDE,
            CaseAction.EXPORT_CASE_DATA,
        }
    ),
    CaseRole.INVESTIGATOR: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.EVIDENCE_WRITE,
            CaseAction.GRAPH_READ,
        }
    ),
    CaseRole.ANALYST: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.GRAPH_READ,
        }
    ),
    CaseRole.REVIEWER: frozenset(
        {
            CaseAction.CASE_READ,
            CaseAction.EVIDENCE_READ,
            CaseAction.GRAPH_READ,
            CaseAction.REVIEW_DECIDE,
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
    """`typ` claim value on an access token. A version bump if the claim shape ever changes."""

    ACCESS_V1 = "access_v1"


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
    """The only user representation ever returned from the API."""

    user_id: UUID
    email_normalized: str
    display_name: str
    is_active: bool
    created_at: datetime


class CaseMembershipView(AccessControlModel):
    """One case membership, as shown to the member themselves via `/me`."""

    case_id: UUID
    role: CaseRole
    clearance: ClearanceLevel
    is_active: bool


class RegisterRequest(AccessControlModel):
    """`password` bounds enforce the real MVP policy (see `password.py`).

    A request-validation failure on any field of this model -- including
    this one -- is rendered by `app.core.errors.request_validation_exception_handler`,
    which never echoes the submitted value, so enforcing the policy here
    (rather than only deeper in `service.py`) cannot leak the password.
    """

    email: str
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


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
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(AccessControlModel):
    user: PublicUser
    case_memberships: tuple[CaseMembershipView, ...]


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


class AuthenticatedPrincipal(AccessControlModel):
    """The result of successfully validating an access token against a live session."""

    user_id: UUID
    session_id: UUID


class AuthorizedCasePrincipal(AccessControlModel):
    """The result of a successful case-scoped authorization check."""

    principal: AuthenticatedPrincipal
    case_id: UUID
    membership: CaseMembershipRecord
