"""Real local ONNX detector adapter: failure modes always run; real-detection
scenarios self-skip if the model asset has not been bootstrapped locally
(mirrors `ffmpeg_available()`'s self-skip convention in `synthetic.py` --
this suite never downloads the model itself, per the task's "tests must not
download model weights" requirement)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.analysis.onnx_detector import (
    DEFAULT_MODEL_SHA256,
    DetectorConfig,
    OnnxObjectDetector,
)
from app.modules.media_processing.errors import ModelAssetError
from app.modules.media_processing.image.geometry import PixelBoundingBox
from tests.fixtures.media_processing.synthetic import make_solid_frame

#: Where a locally-bootstrapped model would live -- overridable so a
#: developer who bootstrapped it somewhere else can still run the
#: real-detection scenarios; never downloaded by this suite itself.
_MODEL_PATH = Path(
    os.environ.get("MEDIA_DETECTOR_MODEL_PATH", "models/media/object_detection_yolox_2022nov.onnx")
)


def _model_available() -> bool:
    return _MODEL_PATH.is_file()


def _skip_unless_model_available() -> None:
    if not _model_available():
        pytest.skip(
            f"detector model asset not found at {_MODEL_PATH}; run "
            "`uv run python -m app.modules.media_processing.bootstrap_models` to enable "
            "this real-detection scenario (this suite never downloads it itself)"
        )


# --- Failure modes: always run, never need the real model asset ------------


def test_missing_model_file_fails_clearly() -> None:
    with pytest.raises(ModelAssetError, match="missing"):
        OnnxObjectDetector(config=DetectorConfig(model_path=Path("/nonexistent/model.onnx")))


def test_checksum_mismatch_fails_clearly_and_never_loads_the_file(tmp_path: Path) -> None:
    """A file that exists but doesn't match the pinned checksum is rejected before
    ever being handed to onnxruntime -- garbage bytes are enough to prove this,
    since the checksum check runs first."""
    garbage = tmp_path / "not-a-real-model.onnx"
    garbage.write_bytes(b"definitely not a valid onnx model")
    with pytest.raises(ModelAssetError, match="SHA-256"):
        OnnxObjectDetector(
            config=DetectorConfig(model_path=garbage, expected_sha256=DEFAULT_MODEL_SHA256)
        )


def test_checksum_verification_uses_the_configured_expected_value(tmp_path: Path) -> None:
    """A caller-supplied `expected_sha256` (not just the module default) is what's checked."""
    payload = b"some deterministic bytes"
    path = tmp_path / "custom.onnx"
    path.write_bytes(payload)
    import hashlib

    real_sha256 = hashlib.sha256(payload).hexdigest()
    # Matches the real (if non-ONNX) content -- passes the checksum gate, so
    # the failure that follows must come from onnxruntime rejecting the
    # invalid model content, not from checksum verification.
    with pytest.raises(ModelAssetError, match="could not be loaded"):
        OnnxObjectDetector(config=DetectorConfig(model_path=path, expected_sha256=real_sha256))


def test_cuda_device_without_a_cuda_provider_fails_clearly(tmp_path: Path) -> None:
    """This sandbox's `onnxruntime` install has no CUDAExecutionProvider -- a real,
    unmocked assertion that requesting device='cuda' here fails safely rather than
    silently running on CPU."""
    import onnxruntime as ort

    if "CUDAExecutionProvider" in ort.get_available_providers():
        pytest.skip("a real CUDA execution provider is available in this environment")
    import hashlib

    payload = b"checksum must match so this fails on device selection, not checksum verification"
    garbage = tmp_path / "model.onnx"
    garbage.write_bytes(payload)
    with pytest.raises(ModelAssetError, match="cuda"):
        OnnxObjectDetector(
            config=DetectorConfig(
                model_path=garbage,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                device="cuda",
            )
        )


# --- Real detection: self-skips without a locally-bootstrapped model asset -


def test_real_detector_loads_and_selects_a_device() -> None:
    _skip_unless_model_available()
    detector = OnnxObjectDetector(config=DetectorConfig(model_path=_MODEL_PATH))
    assert detector.device in ("cpu", "cuda")


def test_real_detector_returns_well_formed_output_on_a_synthetic_frame() -> None:
    """A committed, solid-color synthetic frame is not expected to contain any
    COCO-class object -- this proves the real pipeline (letterbox -> onnxruntime
    forward pass -> grid/stride decode -> NMS -> unletterbox) runs end to end
    without crashing and returns a well-typed, possibly-empty result; it does not
    assert a specific detection, since a solid color frame has no real object in it.
    """
    _skip_unless_model_available()
    detector = OnnxObjectDetector(config=DetectorConfig(model_path=_MODEL_PATH))
    frame = make_solid_frame(width=320, height=240, value=128)
    detections = detector.detect(frame)
    assert isinstance(detections, list)
    for detection in detections:
        assert isinstance(detection, ObjectDetection)
        assert isinstance(detection.label, str) and detection.label
        assert 0.0 <= detection.confidence <= 1.0
        assert isinstance(detection.box, PixelBoundingBox)
        assert detection.box.x_min < detection.box.x_max
        assert detection.box.y_min < detection.box.y_max
        assert isinstance(detection.confidence, float)  # never a numpy scalar leaking out


def test_real_detector_is_deterministic_for_the_same_frame() -> None:
    _skip_unless_model_available()
    detector = OnnxObjectDetector(config=DetectorConfig(model_path=_MODEL_PATH))
    frame = make_solid_frame(width=320, height=240, value=200)
    first = detector.detect(frame)
    second = detector.detect(frame)
    assert [(d.label, d.confidence, d.box) for d in first] == [
        (d.label, d.confidence, d.box) for d in second
    ]


def test_real_detector_rejects_a_degenerate_empty_frame() -> None:
    _skip_unless_model_available()
    detector = OnnxObjectDetector(config=DetectorConfig(model_path=_MODEL_PATH))
    empty_frame = np.zeros((0, 0, 3), dtype=np.uint8)
    assert detector.detect(empty_frame) == []
