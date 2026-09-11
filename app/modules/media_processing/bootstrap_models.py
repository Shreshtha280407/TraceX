"""Explicit, operator-invoked model-asset bootstrap -- the *only* place this
repository ever downloads a model weight file.

    uv run python -m app.modules.media_processing.bootstrap_models

Never imported or invoked by `app.main`, the internal worker API, or
`media_processing.worker`'s `--once`/`--loop` CLI -- a real local detector
model is a large (tens of megabytes), licensed, third-party binary asset,
and this repository commits none of that to Git. An operator runs this
command once (or after clearing a stale/corrupt local cache) as a distinct,
auditable step before starting a worker that needs real detection, exactly
the same "explicit trusted-operator action, not something that happens
automatically during normal request/job handling" posture
`app.modules.access_control.worker_credentials`'s CLI already established
for worker identity provisioning.

## What this downloads

See `analysis/onnx_detector.py`'s module docstring ("Model provenance") for
the full source/license/commit details -- summarized here:

- File: `object_detection_yolox_2022nov.onnx` (YOLOX-s, COCO 80-class).
- Source: `github.com/opencv/opencv_zoo`, commit
  `0b263e423d012606b83d1f81238d11c177da2b9c` (pinned, not a branch head).
- License: Apache License 2.0.
- SHA-256: `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`
  (35,858,002 bytes).

The download is verified against this pinned checksum before the file is
written to its final destination -- a corrupted or tampered download is
deleted, never left in place under the expected filename. An
already-present, already-valid file at the destination is left untouched
(idempotent, safe to re-run) unless `--force` is passed.

## OCR

There is no equivalent "download a weight file" step for OCR: the
`tesseract` binary and its language packs are ordinary Linux system
packages (`tesseract-ocr`, `tesseract-ocr-eng`, ...), installed via the
Dockerfile / the host's package manager, not this script -- see
`docs/architecture/media-processing-worker.md`'s "OCR runtime setup".
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app.core.config import get_settings
from app.modules.media_processing.analysis.onnx_detector import (
    DEFAULT_MODEL_SHA256,
    DEFAULT_MODEL_SOURCE_URL,
)

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_READ_TIMEOUT_SECONDS = 60.0


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bootstrap_detector_model(
    *, destination: Path, source_url: str, expected_sha256: str, force: bool = False
) -> bool:
    """Download and checksum-verify the pinned detector model, if not already present.

    Returns `True` if a download happened, `False` if an already-valid file
    was left in place untouched. Raises `ValueError` if the downloaded
    content does not match `expected_sha256` (the partial download is
    removed, never left under the destination filename).
    """
    if destination.is_file() and not force:
        if _sha256_of(destination) == expected_sha256:
            print(f"already present and verified: {destination}")
            return False
        print(f"existing file at {destination} failed checksum verification; re-downloading")

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".partial")
    print(f"downloading {source_url}")
    try:
        with (
            urllib.request.urlopen(source_url, timeout=_READ_TIMEOUT_SECONDS) as response,  # noqa: S310 - fixed, documented, pinned HTTPS source URL
            tmp_path.open("wb") as handle,
        ):
            while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                handle.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {type(exc).__name__}") from exc

    actual_sha256 = _sha256_of(tmp_path)
    if actual_sha256 != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(
            f"downloaded file failed checksum verification: expected {expected_sha256}, "
            f"got {actual_sha256} -- refusing to install it; the source may have changed "
            f"or the download may have been corrupted/tampered with"
        )

    tmp_path.replace(destination)
    print(f"verified and installed: {destination} (sha256={actual_sha256})")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.media_processing.bootstrap_models",
        description=(
            "Download and checksum-verify the pinned local detector model asset. "
            "Never run automatically -- an explicit, operator-invoked step."
        ),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Where to write the model file (default: Settings.media_detector_model_path).",
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_MODEL_SOURCE_URL,
        help="Pinned source URL to download from (default: the documented, commit-pinned URL).",
    )
    parser.add_argument(
        "--expected-sha256",
        default=DEFAULT_MODEL_SHA256,
        help="Expected SHA-256 of the downloaded file (default: the pinned, documented value).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if a file already present at the destination verifies correctly.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    destination = args.destination or settings.media_detector_model_path

    try:
        bootstrap_detector_model(
            destination=destination,
            source_url=args.source_url,
            expected_sha256=args.expected_sha256,
            force=args.force,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["bootstrap_detector_model", "main"]
