"""Real, local OCR adapter: Tesseract via `pytesseract`.

`pytesseract` is a thin subprocess wrapper around the system `tesseract`
binary -- no bundled model weights, no network access, no cloud OCR API.
Requires the `tesseract-ocr` system package (and any additional language
packs beyond the bundled `eng`) to already be installed on the host/
container; this module never installs, downloads, or silently substitutes
a different engine when that's missing -- see `TesseractTextRecognizer.__post_init__`
and `docs/architecture/media-processing-worker.md`'s "OCR runtime setup".

`recognize_regions` is the primary entry point `worker.py` calls directly on
a whole frame/image (see `interfaces.TextRecognizer`'s docstring for why):
`pytesseract.image_to_data` gives per-word text, confidence, and pixel
bounding boxes in one pass, which this adapter groups into per-line regions
(consecutive words sharing the same block/paragraph/line index) -- a line is
generally the more useful atomic OCR observation for investigative evidence
(a sign, a plate, a document heading) than a single isolated word, and
tesseract's own layout analysis already computes the grouping; nothing here
merges text that wasn't already judged to be one line by the engine itself.

Recognized text is never logged by this module -- only counts and
confidence values ever appear in a `structlog` call site (`worker.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytesseract
from pytesseract import Output, TesseractNotFoundError

from app.modules.media_processing.analysis.interfaces import RecognizedText
from app.modules.media_processing.errors import OcrRuntimeError
from app.modules.media_processing.image.geometry import PixelBoundingBox
from app.modules.media_processing.models import Frame

TESSERACT_OCR_VERSION = "tesseract_ocr_v1"

#: tesseract's `image_to_data` row hierarchy: page(1) > block(2) > paragraph(3)
#: > line(4) > word(5). Only word-level rows carry real text/confidence --
#: every coarser level's `text` is always empty and `conf` is always `-1`.
_WORD_LEVEL = 5


@dataclass(frozen=True)
class TesseractOcrConfig:
    """Typed, explicit local-OCR configuration."""

    language: str = "eng"
    #: Minimum per-line confidence (`[0, 1]`, tesseract's own `0-100` scale
    #: divided by 100) to keep a recognized region -- natural-photo OCR is
    #: far noisier than scanned-document OCR, so a non-zero floor matters
    #: more here than it would for `structured_processing`'s document text.
    min_confidence: float = 0.3
    #: Page-segmentation mode 11 ("sparse text: find as much text as
    #: possible, in no particular order") suits an arbitrary photo/video
    #: frame -- unlike PSM 3 (the tesseract default, "fully automatic page
    #: segmentation"), it does not assume the image is a structured document.
    page_segmentation_mode: int = 11
    #: Override the `tesseract` binary path (e.g. a non-standard container
    #: image layout). `None` uses `pytesseract`'s own `PATH` lookup.
    tesseract_cmd: str | None = None


@dataclass
class TesseractTextRecognizer:
    """Real, local Tesseract-backed `TextRecognizer`. See module docstring."""

    config: TesseractOcrConfig = field(default_factory=TesseractOcrConfig)

    def __post_init__(self) -> None:
        if self.config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
        try:
            pytesseract.get_tesseract_version()
        except (TesseractNotFoundError, OSError) as exc:
            raise OcrRuntimeError(
                "the tesseract OCR binary is not available on PATH -- install the "
                "'tesseract-ocr' system package (see "
                "docs/architecture/media-processing-worker.md's 'OCR runtime setup')"
            ) from exc
        try:
            available_languages = set(pytesseract.get_languages(config=""))
        except pytesseract.TesseractError as exc:
            raise OcrRuntimeError("could not list installed tesseract language packs") from exc
        if self.config.language not in available_languages:
            raise OcrRuntimeError(
                f"the requested tesseract language pack '{self.config.language}' is not "
                f"installed -- install 'tesseract-ocr-{self.config.language}'"
            )

    def recognize(self, crop: Frame) -> RecognizedText | None:
        """Answer "what text, if any, is in this whole crop" as one combined result.

        Delegates to `recognize_regions` (so the same word grouping,
        confidence floor, and language/runtime configuration apply) and
        joins every surviving line into one answer -- the caller already
        chose this exact crop as *the* text region (see
        `interfaces.TextRecognizer`'s docstring), so one aggregate result
        for the whole thing is what it's asking for.
        """
        regions = self.recognize_regions(crop)
        if not regions:
            return None
        height, width = int(crop.shape[0]), int(crop.shape[1])
        return RecognizedText(
            text=" ".join(region.text for region in regions),
            confidence=sum(region.confidence for region in regions) / len(regions),
            box=PixelBoundingBox(x_min=0, y_min=0, x_max=width, y_max=height),
            attributes={
                "model_interface_version": TESSERACT_OCR_VERSION,
                "region_count": len(regions),
            },
        )

    def recognize_regions(self, image: Frame) -> list[RecognizedText]:
        """Find and recognize every text line tesseract's own layout analysis identifies."""
        if image.shape[0] <= 0 or image.shape[1] <= 0:
            return []
        data = pytesseract.image_to_data(
            image,
            lang=self.config.language,
            config=f"--psm {self.config.page_segmentation_mode}",
            output_type=Output.DICT,
        )
        lines: dict[tuple[int, int, int], list[int]] = {}
        for index, level in enumerate(data["level"]):
            if level != _WORD_LEVEL:
                continue
            if not data["text"][index].strip():
                continue
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                continue
            if confidence < 0:  # tesseract's own "not applicable" sentinel
                continue
            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            lines.setdefault(key, []).append(index)

        results: list[RecognizedText] = []
        for key in sorted(lines):
            indices = lines[key]
            confidences = [float(data["conf"][i]) / 100.0 for i in indices]
            average_confidence = sum(confidences) / len(confidences)
            if average_confidence < self.config.min_confidence:
                continue
            left = min(data["left"][i] for i in indices)
            top = min(data["top"][i] for i in indices)
            right = max(data["left"][i] + data["width"][i] for i in indices)
            bottom = max(data["top"][i] + data["height"][i] for i in indices)
            results.append(
                RecognizedText(
                    text=" ".join(data["text"][i].strip() for i in indices),
                    confidence=average_confidence,
                    box=PixelBoundingBox(
                        x_min=float(left), y_min=float(top), x_max=float(right), y_max=float(bottom)
                    ),
                    attributes={
                        "model_interface_version": TESSERACT_OCR_VERSION,
                        "word_count": len(indices),
                    },
                )
            )
        return results


__all__ = ["TESSERACT_OCR_VERSION", "TesseractOcrConfig", "TesseractTextRecognizer"]
