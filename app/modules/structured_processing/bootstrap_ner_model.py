"""Explicit, operator-invoked NER model-asset bootstrap -- the *only* place
this repository ever downloads the local NER model data.

    uv run python -m app.modules.structured_processing.bootstrap_ner_model

Never imported or invoked by `app.main`, the internal worker API, or
`structured_processing.worker`'s `--once` CLI -- exactly the same posture
`media_processing.bootstrap_models` already established for the real
detector model (see that module's docstring). An operator runs this
command once (or after clearing a stale/corrupt local cache) as a
distinct, auditable step before starting a worker that needs real local
NER; `document.ner_spacy.SpacyNerAdapter` raises a clear, safe error and
`worker.py` degrades to the deterministic fallback adapter if this was
never run (see that adapter's own docstring).

## What this downloads, and why no `pip install`/`spacy download` is ever run

See `document/ner_spacy.py`'s module docstring ("Model provenance") for
the full source/license/version. Concretely: the pinned wheel is a plain
ZIP archive whose payload is a directory
(`en_core_web_sm/en_core_web_sm-<version>/`) containing the model's
`config.cfg`, `meta.json`, and per-component weight files -- spaCy can
`spacy.load()` that directory *directly*, with no package installation
step of any kind. This script downloads the wheel, verifies it against
the pinned SHA-256 below, then extracts exactly that inner directory to
the configured destination -- it never runs `pip install`, `uv pip
install`, or `spacy download` (all of which this project's `CLAUDE.md`
either forbids outright or reserves for `uv add`/dependency-management
commands, not a model-asset bootstrap step). This is the same "download a
file, verify its checksum, place it at a destination" shape
`bootstrap_models.py` already uses for the ONNX detector -- the only
difference is that this asset happens to be a zip archive containing a
directory, not a single file, so the last step is "extract" instead of
"rename into place".
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from app.core.config import get_settings

#: Pinned to a specific release tag, not a version-agnostic "latest" URL.
DEFAULT_NER_MODEL_SOURCE_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
DEFAULT_NER_MODEL_SHA256 = "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
#: The path *inside* the wheel's zip archive that is the actual loadable
#: spaCy model directory -- everything else in the wheel (the
#: `*.dist-info` metadata, the outer `en_core_web_sm/` package `__init__`)
#: is pip-packaging scaffolding this script deliberately does not need or
#: extract, since it never runs `pip install`.
_MODEL_DIR_PREFIX = "en_core_web_sm/en_core_web_sm-3.8.0/"

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_READ_TIMEOUT_SECONDS = 60.0


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_valid_model_dir(path: Path) -> bool:
    return (path / "config.cfg").is_file() and (path / "meta.json").is_file()


def bootstrap_ner_model(
    *,
    destination: Path,
    source_url: str,
    expected_sha256: str,
    model_dir_prefix: str = _MODEL_DIR_PREFIX,
    force: bool = False,
) -> bool:
    """Download, checksum-verify, and extract the pinned local NER model, if not already present.

    Returns `True` if a download/extract happened, `False` if an
    already-valid model directory was left in place untouched. Raises
    `ValueError` if the downloaded wheel does not match `expected_sha256`
    (the partial download is removed, never left under any filename).
    """
    if destination.is_dir() and _is_valid_model_dir(destination) and not force:
        print(f"already present and verified: {destination}")
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.parent / f"{destination.name}.download.partial"
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

    try:
        with zipfile.ZipFile(tmp_path) as archive:
            members = [n for n in archive.namelist() if n.startswith(model_dir_prefix)]
            if not members:
                raise ValueError(
                    f"verified wheel does not contain expected model directory "
                    f"'{model_dir_prefix}' -- refusing to install a mismatched asset"
                )
            for member in members:
                archive.extract(member, path=destination.parent / ".ner_model_extract_tmp")
    finally:
        tmp_path.unlink(missing_ok=True)

    extracted_root = destination.parent / ".ner_model_extract_tmp" / model_dir_prefix.rstrip("/")
    if destination.exists():
        _remove_tree(destination)
    extracted_root.rename(destination)
    _remove_tree(destination.parent / ".ner_model_extract_tmp")

    print(f"verified and installed: {destination} (wheel sha256={actual_sha256})")
    return True


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.modules.structured_processing.bootstrap_ner_model",
        description=(
            "Download, checksum-verify, and extract the pinned local NER model asset. "
            "Never run automatically -- an explicit, operator-invoked step. Never runs "
            "pip install/spacy download -- see this module's docstring."
        ),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="Where to write the model directory (default: Settings.ner_model_path).",
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_NER_MODEL_SOURCE_URL,
        help="Pinned source URL to download from (default: the documented, pinned release URL).",
    )
    parser.add_argument(
        "--expected-sha256",
        default=DEFAULT_NER_MODEL_SHA256,
        help="Expected SHA-256 of the downloaded wheel (default: the pinned, documented value).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if a valid model directory is already present.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    destination = args.destination or settings.ner_model_path

    try:
        bootstrap_ner_model(
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


__all__ = [
    "DEFAULT_NER_MODEL_SHA256",
    "DEFAULT_NER_MODEL_SOURCE_URL",
    "bootstrap_ner_model",
    "main",
]
