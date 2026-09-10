"""TXT extraction with safe, documented encoding handling.

Decoding strategy (in order):

1. `utf-8-sig` — plain UTF-8, transparently stripping a leading BOM if
   present. This is correct for the overwhelming majority of real text
   files and is tried first.
2. `latin-1` (ISO-8859-1) — a total function over all 256 byte values, so
   it never raises `UnicodeDecodeError`. Used only if step 1 fails. This
   favors *never failing outright* over rendering fidelity: some non-Latin
   text may come out visually wrong, but the deterministic regex
   extraction this phase performs (phone/email/date/amount patterns) is
   ASCII-anchored and unaffected by mis-decoded characters elsewhere in
   the stream.

No other fallback encodings are attempted — this two-step strategy is the
complete, documented policy.
"""

from __future__ import annotations

from app.modules.structured_processing.document.text_extractors import enforce_text_limits
from app.modules.structured_processing.models import TextSegment


def extract_txt(data: bytes) -> list[TextSegment]:
    """Decode TXT bytes into a single `TextSegment` (`page=None`)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")

    segments = [TextSegment(page=None, text=text)]
    enforce_text_limits(segments)
    return segments
