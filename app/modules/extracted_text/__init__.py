"""Pure, provenance-preserving extracted-text utilities.

This package deliberately has no worker, API, storage, graph, or OCR-engine
dependency.  It turns one already-recognized OCR fragment into deterministic
review material that a producer may choose to place in a canonical observation.
"""

from app.modules.extracted_text.ocr_postprocessing import (
    OcrFragment,
    OcrPostProcessingProfile,
    OcrPostProcessingResult,
    post_process_ocr_fragment,
)

__all__ = [
    "OcrFragment",
    "OcrPostProcessingProfile",
    "OcrPostProcessingResult",
    "post_process_ocr_fragment",
]
