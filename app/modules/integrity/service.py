"""The Phase 6 integrity facade: the only interface later branches need.

Callers record events and build/verify checkpoints through `IntegrityService`
without knowing anything about Merkle construction, signing, or the
underlying repository/table shapes. See
`docs/architecture/phase-6-integrity.md` for the full design and the
handoff contract for Aditya/Jasraj/Gaurav/Sarthak/Shreshtha.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import Settings
from app.modules.integrity.hashing import build_merkle_root, leaf_hash
from app.modules.integrity.manifest_sink import ManifestSink
from app.modules.integrity.modality_provenance import ModalityObservationIntegrityProvenanceV1
from app.modules.integrity.models import (
    MERKLE_TREE_FORMAT_VERSION,
    CheckpointBuildReceipt,
    IntegrityEventRecord,
    IntegrityEventSubmission,
    SigningKeyPublicRecord,
    VerificationBundle,
    VerificationLeaf,
    VerificationResult,
)
from app.modules.integrity.repository import (
    IntegrityRepository,
    IntegrityValidationError,
    SigningKeyConflictError,
)
from app.modules.integrity.signing import load_signing_key
from app.modules.integrity.signing import verify as verify_signature
from app.modules.integrity.structured_provenance import (
    StructuredObservationIntegrityProvenanceV1,
)

__all__ = [
    "IntegrityService",
    "IntegrityValidationError",
    "SigningKeyConflictError",
]

logger = structlog.get_logger(__name__)


class IntegrityService:
    def __init__(self, repository: IntegrityRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    async def record_integrity_event(
        self, submission: IntegrityEventSubmission, *, now: datetime | None = None
    ) -> IntegrityEventRecord:
        """Record one safe, case-scoped integrity event. Idempotent on exact retry."""
        return await self._repository.record_event(submission, now=now)

    async def record_structured_observation_provenance(
        self,
        projection: StructuredObservationIntegrityProvenanceV1,
        *,
        source_created_at: datetime,
        now: datetime | None = None,
    ) -> IntegrityEventRecord:
        """Persist the safe projection, then add its one additive observation leaf.

        A primary observation has already committed when this seam is called.
        If leaf recording fails, the immutable projection remains available to
        the bounded reconciliation service; no generic lifecycle leaf is
        emitted here.
        """
        await self._repository.record_structured_provenance(
            projection, source_created_at=source_created_at, now=now
        )
        return await self.record_integrity_event(
            projection.to_integrity_submission(source_created_at=source_created_at), now=now
        )

    async def record_modality_observation_provenance(
        self,
        projection: ModalityObservationIntegrityProvenanceV1,
        *,
        source_created_at: datetime,
        now: datetime | None = None,
    ) -> IntegrityEventRecord:
        """Persist a safe visual/communication projection, then add its sole leaf."""
        await self._repository.record_modality_provenance(
            projection, source_created_at=source_created_at, now=now
        )
        return await self.record_integrity_event(
            projection.to_integrity_submission(source_created_at=source_created_at), now=now
        )

    async def build_checkpoint(
        self,
        *,
        case_id: UUID,
        start_sequence: int,
        end_sequence: int,
        now: datetime | None = None,
    ) -> CheckpointBuildReceipt:
        """Seal a contiguous, case-scoped event range into a signed Merkle checkpoint.

        Idempotent for an exact repeat of the same range. Rejects a range
        with gaps (not yet-contiguous events) and, via the database's
        exclusion constraint, any range that overlaps a previously sealed
        checkpoint for this case.
        """
        if end_sequence < start_sequence:
            raise IntegrityValidationError("end_sequence must be >= start_sequence")
        events = await self._repository.list_events_in_range(case_id, start_sequence, end_sequence)
        expected_sequence_numbers = list(range(start_sequence, end_sequence + 1))
        if [event.sequence_number for event in events] != expected_sequence_numbers:
            raise IntegrityValidationError(
                "checkpoint range does not match a contiguous persisted event sequence"
            )
        root_hash = build_merkle_root([leaf_hash(event) for event in events])
        signing_key = load_signing_key(self._settings)
        signed_root = signing_key.sign(root_hash)
        receipt = await self._repository.create_checkpoint(
            case_id=case_id,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            leaf_count=len(events),
            root_hash=root_hash,
            tree_format_version=MERKLE_TREE_FORMAT_VERSION,
            signed=signed_root,
            now=now or datetime.now(UTC),
        )
        if not receipt.replayed:
            # Gap-Closure WP-6/re-close (G8): the real emission point behind
            # the event catalog's `checkpoint.sealed`/`checkpoint.signed`
            # names -- a checkpoint and its signature are always created
            # together (one repository call writes both rows), so one log
            # line represents both catalog names. Never logged on an
            # idempotent replay of an already-sealed range.
            logger.info(
                "checkpoint.sealed",
                case_id=str(case_id),
                checkpoint_id=str(receipt.checkpoint.checkpoint_id),
                leaf_count=receipt.checkpoint.leaf_count,
                key_id=receipt.signature.key_id,
            )
        return receipt

    async def build_pending_checkpoints(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[CheckpointBuildReceipt]:
        """Gap-Closure WP-5 (G4): seal every case with pending events, one
        checkpoint per case, up to `limit` cases. A single case's build
        failure (e.g. a signing-key problem) is not caught here -- it
        propagates and stops the sweep, exactly like `build_checkpoint`
        already does for one explicit call; the caller (`cli.py
        checkpoint-once`/`checkpoint-loop`) decides how to handle it."""
        pending = await self._repository.list_pending_checkpoint_ranges(limit=limit)
        return [
            await self.build_checkpoint(
                case_id=item.case_id,
                start_sequence=item.start_sequence,
                end_sequence=item.end_sequence,
                now=now,
            )
            for item in pending
        ]

    async def verify_checkpoint(self, checkpoint_id: UUID, *, case_id: UUID) -> VerificationResult:
        """Independently recompute and check a checkpoint's root and signature.

        Never trusts any previously computed value -- every check recomputes
        from the current, case-scoped `integrity_events` rows.
        """
        checkpoint = await self._repository.get_checkpoint(checkpoint_id, case_id=case_id)
        if checkpoint is None:
            return VerificationResult(
                checkpoint_id=checkpoint_id,
                case_id=case_id,
                leaf_count_matches=False,
                root_matches=False,
                signature_valid=False,
                ok=False,
                reason="checkpoint not found for this case",
            )
        signature = await self._repository.get_signature(checkpoint_id)
        events = await self._repository.list_events_in_range(
            case_id, checkpoint.start_sequence, checkpoint.end_sequence
        )
        expected_count = checkpoint.end_sequence - checkpoint.start_sequence + 1
        leaf_count_matches = len(events) == expected_count and len(events) == checkpoint.leaf_count

        root_matches = False
        if leaf_count_matches:
            recomputed_root = build_merkle_root([leaf_hash(event) for event in events])
            root_matches = recomputed_root == checkpoint.root_hash

        signature_valid = signature is not None and verify_signature(
            root_hash_hex=checkpoint.root_hash,
            signature_b64=signature.signature,
            public_key_b64=signature.public_key_b64,
        )

        ok = leaf_count_matches and root_matches and signature_valid
        reason = None
        if not ok:
            if not leaf_count_matches:
                reason = "leaf count does not match the checkpoint's sealed range (missing leaf)"
            elif not root_matches:
                reason = "recomputed Merkle root does not match the sealed checkpoint root"
            elif not signature_valid:
                reason = "checkpoint signature is missing or does not verify"
        return VerificationResult(
            checkpoint_id=checkpoint_id,
            case_id=case_id,
            leaf_count_matches=leaf_count_matches,
            root_matches=root_matches,
            signature_valid=signature_valid,
            ok=ok,
            reason=reason,
        )

    async def register_current_signing_key(self) -> tuple[SigningKeyPublicRecord, bool]:
        """Gap-Closure WP-5 (G4): register the currently configured key's
        public identity into the durable `signing_keys_public` registry.
        Idempotent for a repeat of the same key; raises
        `SigningKeyConflictError` (never overwrites) if `INTEGRITY_SIGNING_
        KEY_ID` was reused for a genuinely different key -- the operator
        must pick a new `key_id` for every real rotation."""
        signing_key = load_signing_key(self._settings)
        material = signing_key.public_key_material()
        return await self._repository.register_signing_key(material)

    async def list_signing_keys(self) -> list[SigningKeyPublicRecord]:
        return await self._repository.list_signing_keys()

    async def export_verification_bundle(
        self, checkpoint_id: UUID, *, case_id: UUID
    ) -> VerificationBundle:
        """Build a portable bundle of only public verification material.

        Contains no raw evidence content, no private key, and no more than
        the hashed metadata already safe to store in `integrity_events`.
        """
        checkpoint = await self._repository.get_checkpoint(checkpoint_id, case_id=case_id)
        if checkpoint is None:
            raise IntegrityValidationError("checkpoint not found for this case")
        signature = await self._repository.get_signature(checkpoint_id)
        if signature is None:
            raise IntegrityValidationError("checkpoint has no recorded signature")
        events = await self._repository.list_events_in_range(
            case_id, checkpoint.start_sequence, checkpoint.end_sequence
        )
        leaves = tuple(
            VerificationLeaf(
                integrity_event_id=event.integrity_event_id,
                sequence_number=event.sequence_number,
                event_kind=event.event_kind,
                subject_type=event.subject_type,
                subject_id=event.subject_id,
                canonical_payload_sha256=event.canonical_payload_sha256,
                payload_schema_version=event.payload_schema_version,
            )
            for event in events
        )
        return VerificationBundle(
            case_id=case_id, checkpoint=checkpoint, signature=signature, leaves=leaves
        )

    async def archive_verification_bundle(
        self, checkpoint_id: UUID, *, case_id: UUID, sink: ManifestSink
    ) -> str:
        """Gap-Closure WP-5 (G4): export, then durably write through `sink`.

        Returns the sink's opaque locator. Raises `ManifestAlreadyExistsError`
        (via the sink) if this checkpoint was already archived there --
        never silently overwrites.
        """
        bundle = await self.export_verification_bundle(checkpoint_id, case_id=case_id)
        payload = bundle.model_dump_json().encode("utf-8")
        return await sink.write_manifest(
            case_id=case_id, checkpoint_id=checkpoint_id, payload=payload
        )
