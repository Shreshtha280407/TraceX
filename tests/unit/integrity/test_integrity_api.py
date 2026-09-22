"""Protected integrity HTTP surface with synthetic, non-sensitive records."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.modules.access_control.dependencies import (
    get_access_control_repository,
    get_login_rate_limiter,
    get_refresh_rate_limiter,
)
from app.modules.access_control.models import CaseRole, ClearanceLevel
from app.modules.access_control.rate_limit import InMemoryRateLimiter
from app.modules.integrity.dependencies import get_integrity_service
from app.modules.integrity.models import (
    CheckpointSignatureRecord,
    IntegrityEventKind,
    MerkleCheckpointRecord,
    VerificationBundle,
    VerificationLeaf,
    VerificationResult,
)
from tests.fixtures.access_control.factories import (
    make_case_record,
    make_membership_record,
    make_user_record,
)
from tests.fixtures.access_control.fake_repository import FakeAccessControlRepository

_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_PASSWORD = "correct-horse-battery-staple"


class _FakeIntegrityRepository:
    def __init__(self) -> None:
        self.records: dict[UUID, MerkleCheckpointRecord] = {}
        self.signatures: dict[UUID, CheckpointSignatureRecord] = {}

    async def list_checkpoints(self, case_id: UUID, *, limit: int, offset: int):
        return [item for item in self.records.values() if item.case_id == case_id][
            offset : offset + limit
        ]

    async def get_checkpoint(self, checkpoint_id: UUID, *, case_id: UUID):
        item = self.records.get(checkpoint_id)
        return item if item is not None and item.case_id == case_id else None

    async def get_signature(self, checkpoint_id: UUID):
        return self.signatures.get(checkpoint_id)


class _FakeIntegrityService:
    def __init__(self, repository: _FakeIntegrityRepository) -> None:
        self._repository = repository

    async def verify_checkpoint(self, checkpoint_id: UUID, *, case_id: UUID) -> VerificationResult:
        return VerificationResult(
            checkpoint_id=checkpoint_id,
            case_id=case_id,
            leaf_count_matches=True,
            root_matches=True,
            signature_valid=True,
            ok=True,
        )

    async def export_verification_bundle(self, checkpoint_id: UUID, *, case_id: UUID):
        checkpoint = await self._repository.get_checkpoint(checkpoint_id, case_id=case_id)
        if checkpoint is None:
            from app.modules.integrity.service import IntegrityValidationError

            raise IntegrityValidationError("checkpoint not found for this case")
        signature = await self._repository.get_signature(checkpoint_id)
        assert signature is not None
        return VerificationBundle(
            case_id=case_id,
            checkpoint=checkpoint,
            signature=signature,
            leaves=(
                VerificationLeaf(
                    integrity_event_id=uuid4(),
                    sequence_number=1,
                    event_kind=IntegrityEventKind.EVIDENCE_REGISTERED,
                    subject_type="evidence",
                    subject_id="synthetic-evidence",
                    canonical_payload_sha256="a" * 64,
                    payload_schema_version="v1",
                ),
            ),
        )


def _add_checkpoint(repository: _FakeIntegrityRepository, case_id: UUID) -> UUID:
    checkpoint_id = uuid4()
    repository.records[checkpoint_id] = MerkleCheckpointRecord(
        checkpoint_id=checkpoint_id,
        case_id=case_id,
        start_sequence=1,
        end_sequence=1,
        leaf_count=1,
        root_hash="b" * 64,
        tree_format_version="tracex-sha256-domain-separated-v1",
        created_at=_NOW,
    )
    repository.signatures[checkpoint_id] = CheckpointSignatureRecord(
        signature_id=uuid4(),
        checkpoint_id=checkpoint_id,
        case_id=case_id,
        key_id="synthetic-public-key",
        algorithm="ed25519",
        signature_encoding="base64",
        signature="synthetic-signature",
        public_key_b64="synthetic-public-key-material",
        public_key_fingerprint="c" * 64,
        signed_root_hash="b" * 64,
        signed_at=_NOW,
    )
    return checkpoint_id


@pytest.fixture
def ac_repository() -> FakeAccessControlRepository:
    return FakeAccessControlRepository()


@pytest.fixture
def integrity_repository() -> _FakeIntegrityRepository:
    return _FakeIntegrityRepository()


@pytest.fixture
def _overrides(
    ac_repository: FakeAccessControlRepository, integrity_repository: _FakeIntegrityRepository
) -> Iterator[None]:
    app.dependency_overrides[get_access_control_repository] = lambda: ac_repository
    app.dependency_overrides[get_login_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_refresh_rate_limiter] = lambda: InMemoryRateLimiter()
    app.dependency_overrides[get_integrity_service] = lambda: _FakeIntegrityService(
        integrity_repository
    )
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(_overrides: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    ) as value:
        yield value


async def _member(
    client: AsyncClient,
    repository: FakeAccessControlRepository,
    role: CaseRole = CaseRole.INVESTIGATOR,
) -> tuple[str, UUID]:
    email = f"integrity-{uuid4().hex[:8]}@example.test"
    user = make_user_record(email_normalized=email)
    await repository.create_user(user)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": _PASSWORD},
    )
    case = make_case_record(classification=ClearanceLevel.CONFIDENTIAL)
    await repository.create_case(case)
    await repository.create_membership(
        make_membership_record(
            case_id=case.case_id,
            user_id=user.user_id,
            role=role,
            clearance=ClearanceLevel.CONFIDENTIAL,
        )
    )
    return login.json()["access_token"], case.case_id


async def test_unauthenticated_integrity_access_is_401(client: AsyncClient) -> None:
    assert (await client.get(f"/api/v1/cases/{uuid4()}/integrity/checkpoints")).status_code == 401


async def test_member_lists_verifies_and_exports_safe_own_checkpoint(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    integrity_repository: _FakeIntegrityRepository,
) -> None:
    token, case_id = await _member(client, ac_repository)
    checkpoint_id = _add_checkpoint(integrity_repository, case_id)
    headers = {"Authorization": f"Bearer {token}"}
    listed = await client.get(f"/api/v1/cases/{case_id}/integrity/checkpoints", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["checkpoint_id"] == str(checkpoint_id)
    verified = await client.post(
        f"/api/v1/cases/{case_id}/integrity/checkpoints/{checkpoint_id}/verify", headers=headers
    )
    assert verified.status_code == 200 and verified.json()["ok"] is True
    exported = await client.get(
        f"/api/v1/cases/{case_id}/integrity/checkpoints/{checkpoint_id}/export", headers=headers
    )
    assert exported.status_code == 403  # investigators cannot export
    owner_token, owner_case = await _member(client, ac_repository, CaseRole.CASE_OWNER)
    owner_checkpoint = _add_checkpoint(integrity_repository, owner_case)
    exported = await client.get(
        f"/api/v1/cases/{owner_case}/integrity/checkpoints/{owner_checkpoint}/export",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert exported.status_code == 200
    serialized = exported.text.lower()
    for forbidden in ("private_key", "token", "secret", "object_uri", "transcript", "ocr_text"):
        assert forbidden not in serialized


async def test_cross_case_and_nonmember_requests_are_safely_denied(
    client: AsyncClient,
    ac_repository: FakeAccessControlRepository,
    integrity_repository: _FakeIntegrityRepository,
) -> None:
    token_a, _case_a = await _member(client, ac_repository)
    _token_b, case_b = await _member(client, ac_repository)
    checkpoint_b = _add_checkpoint(integrity_repository, case_b)
    headers = {"Authorization": f"Bearer {token_a}"}
    denial_bodies = []
    for method, suffix in (("get", ""), ("post", "/verify"), ("get", "/export")):
        response = await getattr(client, method)(
            f"/api/v1/cases/{case_b}/integrity/checkpoints/{checkpoint_b}{suffix}", headers=headers
        )
        assert response.status_code == 403
        denial_bodies.append(response.json())
    assert [body["error"]["code"] for body in denial_bodies] == ["forbidden"] * 3
    assert [body["error"]["message"] for body in denial_bodies] == ["access denied"] * 3
    assert any(event.event_type == "case_access_denied" for event in ac_repository.audit_events)


async def test_viewer_is_denied_integrity_action(
    client: AsyncClient, ac_repository: FakeAccessControlRepository
) -> None:
    token, case_id = await _member(client, ac_repository, CaseRole.VIEWER)
    response = await client.get(
        f"/api/v1/cases/{case_id}/integrity/checkpoints",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
