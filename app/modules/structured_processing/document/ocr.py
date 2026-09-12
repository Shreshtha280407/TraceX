"""Real, local OCR fallback for scanned/untrustworthy PDF pages.

Reuses the same real, local, no-cloud OCR engine
`media_processing.analysis.tesseract_ocr` already established for
image/video evidence (`tesseract` via `pytesseract` — a thin subprocess
wrapper around the system binary, no bundled model weights, no network
access). This module adds the one piece that engine doesn't need for a
photo/video frame but a PDF page does: rendering a page to a raster image
in the first place, via `pypdfium2` (a self-contained PDF renderer with no
external system binary of its own, unlike `pdftoppm`/`poppler-utils`).

OCR runs only on pages `page_trust.classify_page_trust` marked
`SCANNED_NO_TEXT` or `UNTRUSTWORTHY_TEXT_LAYER` — never on a
`TEXT_TRUSTED` page, and never automatically for an entire text-bearing
PDF (see `pdf.py`).

`recognize_page` returns per-line `OcrRegion`s (tesseract's own layout
analysis already groups words into lines/paragraphs/blocks — the same
grouping choice `TesseractTextRecognizer.recognize_regions` already made
for image/video evidence, for the same reason: a line is generally the more
useful atomic OCR observation for investigative evidence than an isolated
word). Every region's bounding box is normalized to `[0, 1]` image
coordinates (`bbox_xyxy_normalized`) and paired with the source page
number — never a raw pixel box with no page context.

Recognized text is never logged by this module — only counts, confidence,
and timing ever appear in a `structlog` call site (`worker.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytesseract
from pytesseract import Output, TesseractNotFoundError

from app.contracts.common import BoundingBoxNormalized
from app.core.canonical import canonical_sha256
from app.modules.structured_processing.errors import ErrorCode, ProcessingError

OCR_ENGINE_NAME = "structured_document_page_ocr_v1"
OCR_ENGINE_VERSION = "1.0.0"

#: tesseract's `image_to_data` row hierarchy: page(1) > block(2) > paragraph(3)
#: > line(4) > word(5). Only word-level rows carry real text/confidence.
_WORD_LEVEL = 5


@dataclass(frozen=True)
class OcrConfig:
    """Typed, explicit local-OCR configuration for document pages."""

    language: str = "eng"
    #: Pixels-per-inch to render each PDF page at before OCR. Higher values
    #: improve small-text recognition at the cost of more CPU/memory per
    #: page; 300 is a common "good enough for printed documents" default.
    dpi: int = 300
    #: Minimum per-line confidence (tesseract's own `0-100` scale, divided
    #: by 100 here) to keep a recognized region.
    min_confidence: float = 0.4
    #: Page-segmentation mode 3 ("fully automatic page segmentation") suits
    #: a scanned document page, unlike media_processing's PSM 11 choice for
    #: an arbitrary photo/video frame (see that module's own OCR adapter).
    page_segmentation_mode: int = 3
    #: Override the `tesseract` binary path. `None` uses `pytesseract`'s
    #: own `PATH` lookup.
    tesseract_cmd: str | None = None


def ocr_config_hash(config: OcrConfig) -> str:
    return canonical_sha256(
        {
            "language": config.language,
            "dpi": config.dpi,
            "min_confidence": config.min_confidence,
            "page_segmentation_mode": config.page_segmentation_mode,
        }
    )


@dataclass(frozen=True)
class OcrRegion:
    """One recognized text line, with its exact page-relative bounding box."""

    text: str
    confidence: float  # [0, 1]
    bbox: BoundingBoxNormalized
    word_count: int


@dataclass(frozen=True)
class OcrPageResult:
    """Everything OCR produced for one page, plus the joined text for downstream extraction."""

    page: int
    regions: tuple[OcrRegion, ...]
    joined_text: str
    #: `joined_text` character offsets `[start, end)` for each region, in
    #: the same order as `regions` — lets a caller (e.g. regex/NER
    #: extraction over `joined_text`) map a matched span back to the exact
    #: region(s), and therefore the exact bounding box(es), it came from.
    region_spans: tuple[tuple[int, int], ...]
    engine_version: str
    tesseract_version: str
    average_confidence: float | None


@dataclass
class DocumentPageOcrEngine:
    """Real, local Tesseract-backed page OCR. See module docstring."""

    config: OcrConfig = field(default_factory=OcrConfig)

    def __post_init__(self) -> None:
        if self.config.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.config.tesseract_cmd
        try:
            self._tesseract_version = str(pytesseract.get_tesseract_version())
        except (TesseractNotFoundError, OSError) as exc:
            raise ProcessingError(
                ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                "the tesseract OCR binary is not available on PATH -- install the "
                "'tesseract-ocr' system package (see docs/architecture/"
                "document-structured-processing.md's 'OCR runtime setup')",
            ) from exc
        try:
            available_languages = set(pytesseract.get_languages(config=""))
        except pytesseract.TesseractError as exc:
            raise ProcessingError(
                ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                "could not list installed tesseract language packs",
            ) from exc
        if self.config.language not in available_languages:
            raise ProcessingError(
                ErrorCode.OCR_RUNTIME_UNAVAILABLE,
                f"the requested tesseract language pack '{self.config.language}' is not "
                f"installed -- install 'tesseract-ocr-{self.config.language}'",
            )

    def recognize_page(self, page_image: object, *, page_number: int) -> OcrPageResult:
        """Run OCR over one already-rendered page image (a PIL `Image`).

        Never raises for "no text found" (a legitimate, common outcome for
        a blank scanned page) — only for a genuine OCR runtime failure,
        turned into a safe `ProcessingError(OCR_RUNTIME_UNAVAILABLE)` by
        the caller if `pytesseract` itself raises mid-recognition.
        """
        width, height = page_image.size  # type: ignore[attr-defined]
        try:
            data = pytesseract.image_to_data(
                page_image,
                lang=self.config.language,
                config=f"--psm {self.config.page_segmentation_mode}",
                output_type=Output.DICT,
            )
        except pytesseract.TesseractError as exc:
            raise ProcessingError(
                ErrorCode.OCR_RUNTIME_UNAVAILABLE, f"OCR failed for page {page_number}"
            ) from exc

        lines: dict[tuple[int, int, int], list[int]] = {}
        for index, level in enumerate(data["level"]):
            if level != _WORD_LEVEL or not data["text"][index].strip():
                continue
            try:
                confidence = float(data["conf"][index])
            except (TypeError, ValueError):
                continue
            if confidence < 0:  # tesseract's own "not applicable" sentinel
                continue
            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            lines.setdefault(key, []).append(index)

        regions: list[OcrRegion] = []
        joined_parts: list[str] = []
        region_spans: list[tuple[int, int]] = []
        cursor = 0
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
            line_text = " ".join(data["text"][i].strip() for i in indices)

            if joined_parts:
                joined_parts.append(" ")
                cursor += 1
            start = cursor
            joined_parts.append(line_text)
            cursor += len(line_text)
            region_spans.append((start, cursor))

            regions.append(
                OcrRegion(
                    text=line_text,
                    confidence=average_confidence,
                    bbox=BoundingBoxNormalized(
                        x_min=max(0.0, min(1.0, left / width)),
                        y_min=max(0.0, min(1.0, top / height)),
                        x_max=max(0.0, min(1.0, right / width)),
                        y_max=max(0.0, min(1.0, bottom / height)),
                    ),
                    word_count=len(indices),
                )
            )

        average = sum(r.confidence for r in regions) / len(regions) if regions else None
        return OcrPageResult(
            page=page_number,
            regions=tuple(regions),
            joined_text="".join(joined_parts),
            region_spans=tuple(region_spans),
            engine_version=OCR_ENGINE_VERSION,
            tesseract_version=self._tesseract_version,
            average_confidence=average,
        )


def locate_bbox_for_span(
    result: OcrPageResult, start: int, end: int
) -> BoundingBoxNormalized | None:
    """The union bounding box of every OCR region overlapping `[start, end)` in `joined_text`.

    Returns `None` if no region overlaps (e.g. the span came from
    whitespace inserted between regions) — the caller falls back to a
    page-only locator rather than fabricating a box.
    """
    overlapping = [
        region
        for region, (span_start, span_end) in zip(result.regions, result.region_spans, strict=True)
        if span_end > start and span_start < end
    ]
    if not overlapping:
        return None
    return BoundingBoxNormalized(
        x_min=min(r.bbox.x_min for r in overlapping),
        y_min=min(r.bbox.y_min for r in overlapping),
        x_max=max(r.bbox.x_max for r in overlapping),
        y_max=max(r.bbox.y_max for r in overlapping),
    )


__all__ = [
    "OCR_ENGINE_NAME",
    "OCR_ENGINE_VERSION",
    "DocumentPageOcrEngine",
    "OcrConfig",
    "OcrPageResult",
    "OcrRegion",
    "locate_bbox_for_span",
    "ocr_config_hash",
]
