"""Deterministic, regex-only FIR/police-report field extraction.

No NLP models, no LLMs, no fuzzy matching — every pattern here is a fixed
regular expression requiring an explicit label (`FIR No.`, `Police
Station`, `Section`, `A/C No.`) or a structurally-distinctive format (an
email address, an Indian vehicle plate, a currency-symbol-prefixed
amount). A value with no such signal is never guessed at.

Every match becomes exactly one `RawMention`, pointing at the exact
matched substring (not the surrounding label) via `span_start`/`span_end`
relative to its `TextSegment`. `extract_fir_mentions` is profile-agnostic
— `worker.py` pairs its output with the `fir_report_text_v1` profile from
`structured/profiles.py` to build actual `ObservationV1`s.

Documented, known limitations of this pattern set (see
docs/qa/known-limitations.md): Indian mobile numbers only (no landline/
international formats); Indian vehicle-plate format only; a curated list
of common UPI handles; `date_time_mention` captures the raw matched text
only and does not attempt to resolve DD/MM vs MM/DD ambiguity into a
parsed date.
"""

from __future__ import annotations

import re

from app.contracts.common import SourceLocator
from app.modules.structured_processing.limits import MAX_MENTION_TEXT_LENGTH
from app.modules.structured_processing.models import RawMention, TextSegment
from app.modules.structured_processing.provenance import CONFIDENCE_REGEX_EXACT_MATCH

_FIR_REFERENCE = re.compile(
    # The literal "FIR" label is always required. What follows it (before
    # the actual identifier) accepts the canonical "No."/"Number" label OR
    # a short (<=6 letters) OCR-garbled stand-in for it -- observed on Gate
    # B macOS Tesseract (5.5.0/5.5.3): "FIR Nex 91/2026", "FIR Ma 20/2026",
    # "FIR Not 30/2026", where "No"/"No." was misread as a different short
    # word. The identifier itself is never guessed at: the trailing
    # lookahead requires it to actually contain a digit (every real FIR
    # reference this project extracts -- "45/2026", "TEST/2026/001",
    # "SECRET/9999/999" -- has one), which is exactly what keeps the
    # tolerant short-word fallback from matching ordinary prose ("FIR
    # discussions ..." never has a digit-bearing token immediately after
    # a <=6-letter word, so it never matches; see
    # test_fir_report.py::test_fir_reference_tolerates_an_ocr_garbled_label
    # for both directions).
    r"F\.?I\.?R\.?\s*(?:No\.?|Number|[A-Za-z]{1,6})\s*[:\-]?\s*"
    r"(?=[A-Za-z0-9/\-]*\d)([A-Za-z0-9][A-Za-z0-9/\-]{2,29})",
    re.IGNORECASE,
)
_POLICE_STATION = re.compile(
    r"(?:Police\s+Station|P\.?S\.?)\s*[:\-]\s*([^\n,;]{2,60})",
    re.IGNORECASE,
)
# DD/MM/YYYY (Indian convention) with an optional 24h time.
_DATE_DMY = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4}(?:[ ,T]+\d{1,2}:\d{2}(?::\d{2})?)?)\b")
_DATE_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)\b")
# Indian mobile numbers only: 10 digits starting 6-9, optional +91/0 prefix.
_PHONE = re.compile(r"(?:\+91[-\s]?|0)?([6-9]\d{9})\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_VEHICLE = re.compile(r"\b([A-Z]{2}[ \-]?\d{1,2}[ \-]?[A-Z]{1,2}[ \-]?\d{4})\b", re.IGNORECASE)
_UPI_HANDLES = (
    "ybl|oksbi|okhdfcbank|okicici|okaxis|paytm|ibl|axl|apl|jio|"
    "airtel|freecharge|yapl|axisb|hdfcbank|sbi"
)
_UPI_ID = re.compile(rf"\b([\w.\-]{{2,64}}@(?:{_UPI_HANDLES}))\b", re.IGNORECASE)
_ACCOUNT_NUMBER = re.compile(
    r"(?:A\/?C\.?\s*(?:No\.?)?|Account\s*(?:No\.?|Number)?)\s*[:\-]?\s*(\d{9,18})",
    re.IGNORECASE,
)
_TXN_REFERENCE = re.compile(
    r"(?:Txn\.?\s*(?:ID|No\.?)?|Transaction\s*(?:ID|No\.?|Reference)?|UTR|Reference\s*(?:No\.?)?)"
    r"\s*[:\-]?\s*([A-Za-z0-9]{6,30})",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_LEGAL_SECTION = re.compile(
    r"(?:Section|Sec\.?|U\/S)\s*[:\-]?\s*(\d+[A-Za-z]?(?:\(\d+\))?"
    r"(?:\s*(?:of\s+)?(?:IPC|CrPC|BNS|BNSS|IT\s*Act))?)",
    re.IGNORECASE,
)

# (observation_type, pattern, entity_type_hint, match_kind)
_PATTERNS: tuple[tuple[str, re.Pattern[str], str, str], ...] = (
    ("fir_reference", _FIR_REFERENCE, "fir_number", "labelled_fir_number"),
    ("police_station_mention", _POLICE_STATION, "police_station", "labelled_police_station"),
    ("date_time_mention", _DATE_DMY, "date_time", "date_dmy"),
    ("date_time_mention", _DATE_ISO, "date_time", "date_iso"),
    ("phone_number_mention", _PHONE, "phone_number", "indian_mobile"),
    ("email_address_mention", _EMAIL, "email_address", "email"),
    ("vehicle_identifier_mention", _VEHICLE, "vehicle_registration", "indian_vehicle_plate"),
    ("financial_identifier_mention", _UPI_ID, "upi_id", "upi_id"),
    (
        "financial_identifier_mention",
        _ACCOUNT_NUMBER,
        "account_number",
        "labelled_account_number",
    ),
    (
        "financial_identifier_mention",
        _TXN_REFERENCE,
        "transaction_reference",
        "labelled_transaction_reference",
    ),
    ("amount_mention", _AMOUNT, "amount", "currency_prefixed_amount"),
    ("legal_section_mention", _LEGAL_SECTION, "legal_section", "labelled_legal_section"),
)


def extract_fir_mentions(segments: list[TextSegment]) -> list[RawMention]:
    """Run every deterministic pattern over each extracted text segment."""
    mentions: list[RawMention] = []
    for segment in segments:
        for observation_type, pattern, entity_type_hint, match_kind in _PATTERNS:
            for match in pattern.finditer(segment.text):
                value = match.group(1).strip()[:MAX_MENTION_TEXT_LENGTH]
                if not value:
                    continue
                locator = SourceLocator(
                    page=segment.page,
                    span_start=match.start(1),
                    span_end=match.end(1),
                )
                mentions.append(
                    RawMention(
                        observation_type=observation_type,
                        text=value,
                        locator=locator,
                        confidence=CONFIDENCE_REGEX_EXACT_MATCH,
                        entity_type_hint=entity_type_hint,
                        attributes={"match_kind": match_kind},
                    )
                )
    return mentions
