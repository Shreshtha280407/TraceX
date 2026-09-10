"""Synthetic user/case/membership record builders for access-control tests.

Each `make_*` function returns a fully-valid instance with sensible
defaults; pass keyword overrides to construct edge cases without repeating
every other required field. Mirrors the pattern in
`tests/fixtures/factories.py`. Every value here is synthetic -- no real
credentials, emails, or names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.modules.access_control.models import (
    CaseMembershipRecord,
    CaseRecord,
    CaseRole,
    CaseStatus,
    ClearanceLevel,
    UserRecord,
)
from app.modules.access_control.password import hash_password

FIXED_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: A pre-hashed password for fixtures that don't care about the plaintext
#: (avoids paying Argon2id's deliberate cost once per fixture instance
#: across a large test suite -- tests that DO care call `hash_password`
#: themselves with a specific plaintext).
DEFAULT_PASSWORD = "correct-horse-battery-staple"
_DEFAULT_PASSWORD_HASH = hash_password(DEFAULT_PASSWORD)


def make_user_record(**overrides: Any) -> UserRecord:
    data: dict[str, Any] = {
        "user_id": uuid4(),
        "email_normalized": f"analyst-{uuid4().hex[:8]}@example.test",
        "display_name": "Test Analyst",
        "password_hash": _DEFAULT_PASSWORD_HASH,
        "is_active": True,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    data.update(overrides)
    return UserRecord(**data)


def make_case_record(**overrides: Any) -> CaseRecord:
    data: dict[str, Any] = {
        "case_id": uuid4(),
        "case_reference": f"CASE-{uuid4().hex[:8]}",
        "classification": ClearanceLevel.CONFIDENTIAL,
        "status": CaseStatus.OPEN,
        "created_at": FIXED_TIME,
    }
    data.update(overrides)
    return CaseRecord(**data)


def make_membership_record(**overrides: Any) -> CaseMembershipRecord:
    data: dict[str, Any] = {
        "membership_id": uuid4(),
        "case_id": uuid4(),
        "user_id": uuid4(),
        "role": CaseRole.INVESTIGATOR,
        "clearance": ClearanceLevel.CONFIDENTIAL,
        "is_active": True,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    }
    data.update(overrides)
    return CaseMembershipRecord(**data)
