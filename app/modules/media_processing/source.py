"""Media classification and the source-resolver/temp-file boundary.

Classification uses `EvidenceRecordV1.content_type` *and* the filename
extension as two independent, cross-checked signals -- never the extension
alone and never the declared content-type alone -- mirroring
`app.modules.structured_processing.document.classifier`.

`SourceResolver` is how the worker gets bytes without ever touching MinIO
directly (see `docs/architecture/phase-1-decisions.md`); `temporary_media_file`
is the one place resolved bytes are ever written to local disk, since
`ffprobe`/`ffmpeg` require a real file path -- and the one place that
temporary file is guaranteed to be cleaned up.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.modules.media_processing.errors import ErrorCode, ProcessingError


class MediaKind(StrEnum):
    """The Phase 1 supported media content types.

    `VIDEO_MATROSKA` (Phase 2 completion) is an additive addition: Nipun's
    `evidence_lifecycle/routing.py` has always listed `video/x-matroska` in
    `SourceType.VIDEO`'s accepted content types, but this module had no
    matching `MediaKind` for it -- a real `.mkv` upload would pass routing's
    content-type check and then fail here with `unsupported_content_type`,
    discoverable only by actually wiring this worker to a real upload (see
    `docs/architecture/media-processing-worker.md`). `ffprobe`/`ffmpeg`
    (this module's only container-reading tools) are container-agnostic, so
    no other code needed to change.
    """

    VIDEO_MP4 = "video/mp4"
    VIDEO_QUICKTIME = "video/quicktime"
    VIDEO_X_MSVIDEO = "video/x-msvideo"
    VIDEO_MATROSKA = "video/x-matroska"
    IMAGE_JPEG = "image/jpeg"
    IMAGE_PNG = "image/png"
    IMAGE_WEBP = "image/webp"


#: Video kinds this module treats as video (probe + sample + extract).
VIDEO_KINDS = frozenset(
    {
        MediaKind.VIDEO_MP4,
        MediaKind.VIDEO_QUICKTIME,
        MediaKind.VIDEO_X_MSVIDEO,
        MediaKind.VIDEO_MATROSKA,
    }
)

#: Image kinds this module treats as image (decode only).
IMAGE_KINDS = frozenset({MediaKind.IMAGE_JPEG, MediaKind.IMAGE_PNG, MediaKind.IMAGE_WEBP})

_KIND_TO_EXTENSIONS: dict[MediaKind, frozenset[str]] = {
    MediaKind.VIDEO_MP4: frozenset({"mp4", "m4v"}),
    MediaKind.VIDEO_QUICKTIME: frozenset({"mov"}),
    MediaKind.VIDEO_X_MSVIDEO: frozenset({"avi"}),
    MediaKind.VIDEO_MATROSKA: frozenset({"mkv"}),
    MediaKind.IMAGE_JPEG: frozenset({"jpg", "jpeg"}),
    MediaKind.IMAGE_PNG: frozenset({"png"}),
    MediaKind.IMAGE_WEBP: frozenset({"webp"}),
}


def is_video(kind: MediaKind) -> bool:
    return kind in VIDEO_KINDS


def _extension_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def classify_media(content_type: str, filename: str) -> MediaKind:
    """Classify a source by content type, cross-checked against its filename extension.

    Raises `ProcessingError(UNSUPPORTED_CONTENT_TYPE)` for a content type
    outside the Phase 1 supported set, or for a filename extension
    inconsistent with that content type -- an inconsistency is treated the
    same as an unsupported type rather than trusted on either signal alone.
    """
    try:
        kind = MediaKind(content_type.lower().strip())
    except ValueError as exc:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_CONTENT_TYPE,
            f"content type '{content_type}' is not supported in this phase",
        ) from exc

    extension = _extension_of(filename)
    if extension not in _KIND_TO_EXTENSIONS[kind]:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_CONTENT_TYPE,
            f"filename extension '.{extension}' is inconsistent with content type '{content_type}'",
        )
    return kind


@runtime_checkable
class SourceResolver(Protocol):
    """Resolves an evidence object URI to bytes, without any storage credentials.

    A worker never talks to MinIO directly. Actual MinIO-backed resolution
    is later-phase (evidence-lifecycle integration) work -- Phase 1 tests
    use `StaticBytesResolver`. Mirrors
    `app.modules.structured_processing.models.SourceResolver` exactly.
    """

    def read_bytes(self, object_uri: str) -> bytes: ...


@dataclass(frozen=True)
class StaticBytesResolver:
    """A `SourceResolver` that always returns one fixed byte payload."""

    payload: bytes

    def read_bytes(self, object_uri: str) -> bytes:  # noqa: ARG002 - protocol conformance
        return self.payload


@contextmanager
def temporary_media_file(data: bytes, *, suffix: str) -> Iterator[Path]:
    """Materialize resolved bytes to a local temp file for `ffprobe`/`ffmpeg`.

    The only place this module writes evidence bytes to disk, and the only
    place that guarantees their cleanup (`finally`, so a probe/extraction
    failure never leaves the file behind). The returned path is never
    included in any error message raised elsewhere in this module.
    """
    fd, name = tempfile.mkstemp(suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        yield path
    finally:
        path.unlink(missing_ok=True)
