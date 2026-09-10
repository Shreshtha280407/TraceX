"""Safe, typed errors for the access-control module.

Every error here carries a short, safe, human-written message only -- never
a password, token, secret, or driver/library exception text. Mirrors the
same rule already applied to `/readyz` in `app/dependencies/services.py`
and to `app/modules/graph/errors.py`.
"""

from __future__ import annotations


class AccessControlError(Exception):
    """Base class for all access-control-module errors."""


class ValidationError(AccessControlError):
    """A caller-supplied value (email, password, pagination, ...) is invalid.

    Never constructed with the offending secret value in its message.
    """


class AuthenticationError(AccessControlError):
    """Login credentials were not accepted.

    Raised for every one of "unknown email", "disabled user", and "wrong
    password" -- the caller must not be able to distinguish these from the
    exception alone (see `docs/decisions/ADR-003-...md`).
    """


class InvalidTokenError(AccessControlError):
    """An access token failed signature, issuer, audience, expiry, or type checks.

    Never constructed with the raw token or the underlying JWT library's
    exception text.
    """


class SessionRevokedError(AccessControlError):
    """A refresh token's session has already been revoked or rotated away."""


class RefreshReuseDetectedError(AccessControlError):
    """A refresh token was reused after rotation -- its whole token family was just revoked."""


class RateLimitExceededError(AccessControlError):
    """Too many attempts for a rate-limited operation (or the limiter fail-closed)."""


class PolicyDeniedError(AccessControlError):
    """A case-scoped authorization check failed.

    Deliberately generic: this exception (and the HTTP response built from
    it) never states *which* check failed -- missing case, inactive
    membership, insufficient role, or insufficient clearance are all
    indistinguishable from the outside, by design (default-deny).
    """


class RetryNotAllowedError(AccessControlError):
    """A caller asked `retry.py` to retry an operation that must never be retried."""
