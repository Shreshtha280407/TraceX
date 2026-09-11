"""Real, local object detector: YOLOX-s via `onnxruntime`.

CPU-only by default; automatically uses a CUDA execution provider when one
is genuinely available in the installed `onnxruntime`/driver stack (see
`_select_providers`) -- never assumed, never silently required. Model
weights are never bundled in this repository or downloaded at import time:
`DetectorConfig.model_path` must point at a real local file an operator
placed there via the documented bootstrap command
(`app/modules/media_processing/bootstrap_models.py`), and its SHA-256 is
verified before the file is ever handed to `onnxruntime` -- a missing,
corrupt, or substituted file fails loudly at construction time
(`ModelAssetError`), never mid-job.

## Model provenance

- Model: YOLOX-s (object-anchor-free YOLO variant), COCO-2017 80-class
  object detector, ONNX export `object_detection_yolox_2022nov.onnx`.
- Source: the OpenCV Zoo project (`github.com/opencv/opencv_zoo`), commit
  `0b263e423d012606b83d1f81238d11c177da2b9c` (the commit that added this
  exact file -- pinned, not `main`, for byte-for-byte reproducibility).
- License: Apache License 2.0 (`opencv/opencv_zoo`'s repository license;
  see that repository's `LICENSE` and `models/object_detection_yolox/LICENSE`).
- SHA-256: `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`
  (35,858,002 bytes) -- see `DEFAULT_MODEL_SHA256`/`DEFAULT_MODEL_SOURCE_URL`
  below and `bootstrap_models.py`.
- Chosen over a raw Ultralytics YOLOv8n export because it is already
  published as a ready-to-use ONNX file under a permissive license from a
  well-known, security-conscious maintainer (OpenCV.org); exporting a
  YOLOv8 `.pt` checkpoint to ONNX would additionally require installing
  `ultralytics`+`torch` (a large, heavyweight, GPU-oriented toolchain,
  explicitly the kind of dependency `CLAUDE.md` asks this project to
  avoid) as a *runtime* dependency of this module just to produce the one
  file this adapter actually needs.

## Pre/post-processing (reimplemented here from OpenCV Zoo's reference
`yolox.py`/`demo.py`, not copied verbatim, since this adapter uses
`onnxruntime.InferenceSession` rather than `cv2.dnn.readNet` for its
independent, correct CPU/CUDA execution-provider selection)

1. Letterbox-resize the RGB input frame into a 640x640 canvas, padded with
   grey (114), preserving aspect ratio (`_letterbox`).
2. `CHW`, batch-dim-added `float32` tensor, no mean/std normalization (this
   model's own published preprocessing does not normalize -- verified
   against the reference implementation).
3. One `onnxruntime` forward pass.
4. Grid + stride decode (anchor-free: `(dx, dy)` offsets and `(w, h)`
   log-scale sizes per grid cell across three feature-map strides `8/16/32`)
   into `(x, y, w, h, objectness, class_scores...)` per candidate box
   (`_decode`).
5. `objectness * class_score` per class, `cv2.dnn.NMSBoxesBatched` for
   combined confidence-thresholding + per-class non-max suppression
   (`_postprocess`).
6. Undo the letterbox scale to map each surviving box back to the
   *original* frame's pixel coordinates before returning it -- `detect`'s
   caller (`worker.py`) normalizes from there via `image/geometry.py`,
   exactly as the fake detector's output already does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import onnxruntime as ort

from app.modules.media_processing.analysis.interfaces import ObjectDetection
from app.modules.media_processing.errors import ModelAssetError
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import Frame

#: See the module docstring's "Model provenance" section.
DEFAULT_MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
DEFAULT_MODEL_SOURCE_URL = (
    "https://github.com/opencv/opencv_zoo/raw/"
    "0b263e423d012606b83d1f81238d11c177da2b9c/"
    "models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
)
DEFAULT_MODEL_FILENAME = "object_detection_yolox_2022nov.onnx"
#: A stable identifier for `Extractor.model_version` -- distinct from the
#: SHA-256 (which already flows into `config_hash` via `DetectorConfig`'s
#: own values passed to `worker.py`'s extractor builder) so a human reading
#: an observation's provenance immediately recognizes which model family
#: produced it without decoding a hash.
MODEL_VERSION_ID = "yolox_s_2022nov"

_INPUT_HEIGHT = 640
_INPUT_WIDTH = 640
_STRIDES: tuple[int, ...] = (8, 16, 32)
_PAD_VALUE = 114.0
_READ_CHUNK_BYTES = 1024 * 1024

#: COCO 2017's 80 class names, in this model's exact output index order
#: (index 0 == "person") -- verified against OpenCV Zoo's own reference
#: `demo.py`. Never reordered; `worker.py` emits `detection.label` as one
#: of these strings verbatim (not filtered down to
#: `interfaces.SUPPORTED_DETECTION_LABELS`, which that module's own
#: docstring documents as advisory, not enforced, for a real detector's
#: own label set).
COCO_CLASSES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich",
    "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
    "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
)  # fmt: skip


@dataclass(frozen=True)
class DetectorConfig:
    """Typed, explicit local-detector configuration -- never a silent default path.

    `model_path` has no default: a caller must say exactly which local
    file to load (see `worker.py::_build_analysis_components`, which reads
    it from `Settings.media_detector_model_path`).
    """

    model_path: Path
    expected_sha256: str = DEFAULT_MODEL_SHA256
    device: Literal["auto", "cpu", "cuda"] = "auto"
    confidence_threshold: float = 0.5
    nms_threshold: float = 0.5
    max_detections_per_frame: int = 100


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_providers(device: Literal["auto", "cpu", "cuda"]) -> tuple[list[str], str]:
    """Resolve a requested device preference to real onnxruntime providers, safely.

    `"cpu"` always works (every onnxruntime install ships
    `CPUExecutionProvider`). `"cuda"` requires `CUDAExecutionProvider` to
    genuinely be present in this process's `onnxruntime` install (i.e.
    `onnxruntime-gpu` on a CUDA-capable host, not the base `onnxruntime`
    package) -- requesting it without that raises `ModelAssetError` rather
    than silently falling back, since a caller who explicitly asked for
    GPU execution should know if it didn't happen. `"auto"` (the default)
    prefers CUDA when available and falls back to CPU otherwise, with no
    error either way -- GPU use is optional, CPU must always work.
    """
    available = ort.get_available_providers()
    cuda_available = "CUDAExecutionProvider" in available
    if device == "cpu":
        return ["CPUExecutionProvider"], "cpu"
    if device == "cuda":
        if not cuda_available:
            raise ModelAssetError(
                "device='cuda' was requested but no CUDAExecutionProvider is available in "
                "this onnxruntime install -- install onnxruntime-gpu on a CUDA-capable host, "
                "or use device='auto'/'cpu'"
            )
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"
    if cuda_available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"
    return ["CPUExecutionProvider"], "cpu"


def _generate_anchors() -> tuple[np.ndarray, np.ndarray]:
    """Precompute the fixed per-cell grid coordinates and stride values this model's
    anchor-free decode needs -- identical every call (depends only on the fixed
    640x640 input size and the three fixed strides), so computed once per detector
    instance rather than per frame."""
    grids: list[np.ndarray] = []
    expanded_strides: list[np.ndarray] = []
    for stride in _STRIDES:
        h_cells, w_cells = _INPUT_HEIGHT // stride, _INPUT_WIDTH // stride
        x_coords, y_coords = np.meshgrid(np.arange(w_cells), np.arange(h_cells))
        grid = np.stack((x_coords, y_coords), axis=2).reshape(1, -1, 2)
        grids.append(grid)
        expanded_strides.append(np.full((*grid.shape[:2], 1), stride))
    return np.concatenate(grids, axis=1), np.concatenate(expanded_strides, axis=1)


def _letterbox(frame: Frame) -> tuple[np.ndarray, float]:
    """Aspect-ratio-preserving resize onto a fixed 640x640 grey canvas, plus the
    scale factor needed to map detections back to the original frame."""
    padded = np.full((_INPUT_HEIGHT, _INPUT_WIDTH, 3), _PAD_VALUE, dtype=np.float32)
    ratio = min(_INPUT_HEIGHT / frame.shape[0], _INPUT_WIDTH / frame.shape[1])
    new_height, new_width = int(frame.shape[0] * ratio), int(frame.shape[1] * ratio)
    resized = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    padded[:new_height, :new_width] = resized.astype(np.float32)
    return padded, ratio


def _decode(raw_output: np.ndarray, grids: np.ndarray, expanded_strides: np.ndarray) -> np.ndarray:
    """Anchor-free grid+stride decode: raw model output -> `(x, y, w, h, obj, cls...)`."""
    dets = raw_output[0] if raw_output.ndim == 3 else raw_output
    dets = dets.astype(np.float64, copy=True)
    dets[:, :2] = (dets[:, :2] + grids[0]) * expanded_strides[0]
    dets[:, 2:4] = np.exp(dets[:, 2:4]) * expanded_strides[0]
    return dets


def _postprocess(
    dets: np.ndarray, *, ratio: float, confidence_threshold: float, nms_threshold: float
) -> list[ObjectDetection]:
    boxes_xywh = np.empty_like(dets[:, :4])
    boxes_xywh[:, 0] = dets[:, 0] - dets[:, 2] / 2.0
    boxes_xywh[:, 1] = dets[:, 1] - dets[:, 3] / 2.0
    boxes_xywh[:, 2] = dets[:, 2]
    boxes_xywh[:, 3] = dets[:, 3]

    class_scores = dets[:, 4:5] * dets[:, 5:]
    max_scores = np.amax(class_scores, axis=1)
    max_score_indices = np.argmax(class_scores, axis=1)

    keep = cv2.dnn.NMSBoxesBatched(
        boxes_xywh.tolist(),
        max_scores.tolist(),
        max_score_indices.tolist(),
        confidence_threshold,
        nms_threshold,
    )
    detections: list[ObjectDetection] = []
    for index in np.asarray(keep).reshape(-1):
        x0, y0, w, h = (float(v) for v in boxes_xywh[index] / ratio)
        class_index = int(max_score_indices[index])
        label = COCO_CLASSES[class_index] if 0 <= class_index < len(COCO_CLASSES) else "unknown"
        detections.append(
            ObjectDetection(
                label=label,
                confidence=float(max_scores[index]),
                box=PixelBoundingBox(x_min=x0, y_min=y0, x_max=x0 + w, y_max=y0 + h),
                attributes={
                    "model_interface_version": MODEL_VERSION_ID,
                    "model_class_index": class_index,
                },
            )
        )
    return detections


@dataclass
class OnnxObjectDetector:
    """Real, local YOLOX-s object detector via `onnxruntime`. See module docstring."""

    config: DetectorConfig
    _session: ort.InferenceSession = field(init=False, repr=False)
    _input_name: str = field(init=False, repr=False)
    _device: str = field(init=False)
    _grids: np.ndarray = field(init=False, repr=False)
    _expanded_strides: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        path = self.config.model_path
        if not path.is_file():
            raise ModelAssetError(
                "configured detector model asset is missing -- run the documented bootstrap "
                "command (see docs/architecture/media-processing-worker.md's "
                "'Model asset bootstrap' section) before starting this worker"
            )
        if _sha256_of(path) != self.config.expected_sha256:
            raise ModelAssetError(
                "configured detector model asset failed SHA-256 verification -- the file may "
                "be corrupt, truncated, or was replaced; re-run the documented bootstrap command"
            )
        providers, device = _select_providers(self.config.device)
        try:
            session = ort.InferenceSession(str(path), providers=providers)
        except Exception as exc:  # onnxruntime's own load-time exception types are broad
            raise ModelAssetError(
                "configured detector model asset could not be loaded by onnxruntime -- it may "
                "not be a valid ONNX model"
            ) from exc
        self._session = session
        self._input_name = session.get_inputs()[0].name
        self._device = device
        self._grids, self._expanded_strides = _generate_anchors()

    @property
    def device(self) -> str:
        """The execution device actually selected (`"cpu"`/`"cuda"`) -- a fact, not a promise."""
        return self._device

    def detect(self, frame: Frame) -> list[ObjectDetection]:
        if frame.shape[0] <= 0 or frame.shape[1] <= 0:
            return []
        padded, ratio = _letterbox(frame)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis, :, :, :].astype(np.float32)
        outputs = self._session.run(None, {self._input_name: blob})
        dets = _decode(outputs[0], self._grids, self._expanded_strides)
        detections = _postprocess(
            dets,
            ratio=ratio,
            confidence_threshold=self.config.confidence_threshold,
            nms_threshold=self.config.nms_threshold,
        )
        return detections[: self.config.max_detections_per_frame]


__all__ = [
    "COCO_CLASSES",
    "DEFAULT_MODEL_FILENAME",
    "DEFAULT_MODEL_SHA256",
    "DEFAULT_MODEL_SOURCE_URL",
    "MODEL_VERSION_ID",
    "DetectorConfig",
    "OnnxObjectDetector",
]
