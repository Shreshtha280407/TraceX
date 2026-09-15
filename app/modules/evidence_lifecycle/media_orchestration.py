"""Phase 4 typed media manifest/publication records; no media decoding lives here."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.common import SourceLocator, TraceXModel
from app.contracts.observation_batch import ObservationBatchSubmissionV1
from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid

MANIFEST_VERSION = "media_chunk_manifest.v1"


class ChunkBoundary(TraceXModel):
    byte_start: int | None = Field(default=None, ge=0)
    byte_end: int | None = Field(default=None, ge=0)
    time_start_ms: int | None = Field(default=None, ge=0)
    time_end_ms: int | None = Field(default=None, ge=0)
    frame_start: int | None = Field(default=None, ge=0)
    frame_end: int | None = Field(default=None, ge=0)
    source_locator: SourceLocator | None = None

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> ChunkBoundary:
        for start, end in (
            (self.byte_start, self.byte_end),
            (self.time_start_ms, self.time_end_ms),
            (self.frame_start, self.frame_end),
        ):
            if start is not None and end is not None and end < start:
                raise ValueError("chunk boundary end must not precede start")
        if not any(value is not None for value in self.model_dump().values()):
            raise ValueError("chunk needs a supplied boundary or source locator")
        return self


class ChunkSpec(TraceXModel):
    index: int = Field(ge=0)
    boundary: ChunkBoundary


class ChunkManifest(TraceXModel):
    manifest_id: UUID
    version: str = MANIFEST_VERSION
    case_id: UUID
    evidence_id: UUID
    job_id: UUID
    source_type: str
    processor_name: str
    processor_version: str
    input_object_uri: str = Field(min_length=1, max_length=512)
    configuration_hash: str = Field(min_length=1, max_length=256)
    manifest_hash: str
    chunks: tuple[ChunkSpec, ...] = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def indexes_are_contiguous(self) -> ChunkManifest:
        if [item.index for item in self.chunks] != list(range(len(self.chunks))):
            raise ValueError("chunk indexes must be contiguous from zero")
        return self

    @model_validator(mode="after")
    def input_uri_is_safe(self) -> ChunkManifest:
        _validate_safe_object_uri(self.input_object_uri)
        return self


def build_manifest(**values: object) -> ChunkManifest:
    # Timestamps describe persistence, not the immutable work definition.
    # Leaving them out makes a restart/retry derive the same identity.
    normalized = {
        "version": MANIFEST_VERSION,
        **{key: value for key, value in values.items() if key != "created_at"},
    }
    manifest_hash = canonical_sha256(normalized)
    return ChunkManifest.model_validate(
        {
            **values,
            "manifest_id": deterministic_uuid("phase4_media_manifest", manifest_hash),
            "manifest_hash": manifest_hash,
        }
    )


def chunk_identity(manifest: ChunkManifest, index: int) -> UUID:
    return deterministic_uuid("phase4_media_chunk", str(manifest.manifest_id), str(index))


def find_chunk_for_interval(manifest: ChunkManifest, *, start_ms: int, end_ms: int) -> ChunkSpec:
    """The one chunk whose time boundary fully contains ``[start_ms, end_ms]``.

    Used on both sides of chunk-scoped publication: a worker calls this to
    decide which chunk a frame/speech interval belongs to before
    publishing, and the coordinator (`EvidenceLifecycleService.
    _validate_media_publication`) calls it again to independently verify
    the worker's claim -- never trusting a worker-declared chunk index on
    its own. Raises `ValueError` (never guesses) when no chunk contains
    the interval (out of scope) or when the interval crosses a chunk
    boundary; either way this is a reject, not an arbitrary assignment.

    Chunk boundaries are half-open (``[start, end)``) on the upper side,
    except for the manifest's last chunk (inclusive), so a sample landing
    exactly on the shared boundary between two chunks deterministically
    belongs to the *later* chunk it starts, rather than being ambiguous --
    this is a fixed, principled tie-break, not an arbitrary one; only the
    manifest's own final instant (its last chunk's own upper bound) is
    ever inclusive, so nothing at a video/audio source's exact end is
    left with no containing chunk at all.
    """
    if end_ms < start_ms:
        raise ValueError("interval end must not precede start")
    last_index = max((chunk.index for chunk in manifest.chunks), default=None)
    containing = [
        chunk
        for chunk in manifest.chunks
        if chunk.boundary.time_start_ms is not None
        and chunk.boundary.time_end_ms is not None
        and chunk.boundary.time_start_ms <= start_ms
        and (
            end_ms < chunk.boundary.time_end_ms
            or (chunk.index == last_index and end_ms == chunk.boundary.time_end_ms)
        )
    ]
    if len(containing) != 1:
        raise ValueError(
            f"interval [{start_ms}, {end_ms}] is not contained within exactly one time-bounded "
            f"chunk of manifest {manifest.manifest_id} ({len(containing)} candidates)"
        )
    return containing[0]


class ArtifactRegistration(TraceXModel):
    artifact_id: UUID
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    parent_evidence_id: UUID
    object_uri: str = Field(min_length=1, max_length=512)
    artifact_kind: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int | None = Field(default=None, ge=0)
    source_locator: SourceLocator | None = None
    producer_version: str = Field(min_length=1, max_length=128)
    configuration_hash: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def safe_uri(self) -> ArtifactRegistration:
        _validate_safe_object_uri(self.object_uri)
        return self


class MediaChunkPublication(TraceXModel):
    manifest_id: UUID
    manifest_hash: str
    chunk_id: UUID
    chunk_index: int = Field(ge=0)
    batch: ObservationBatchSubmissionV1
    artifacts: tuple[ArtifactRegistration, ...] = ()
    checkpoint_id: UUID
    completed_at: datetime


def _validate_safe_object_uri(value: str) -> None:
    """Accept an opaque object reference, never an inline payload or credentials."""
    lowered = value.lower()
    if any(
        token in lowered for token in ("@", "token", "password", "secret", "credential", "data:")
    ):
        raise ValueError("object URI must not carry credentials, secrets, or inline bytes")
