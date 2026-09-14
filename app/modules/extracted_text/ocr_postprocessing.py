"""Conservative, deterministic post-processing for already-produced OCR text.

This is intentionally a pure utility boundary: it neither runs OCR nor writes
to an API, database, object store, outbox, or graph.  Raw OCR text is retained
only in the returned in-memory value; callers must apply their own canonical
observation safe-property policy before persistence or projection.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.contracts.common import BoundingBoxNormalized, SourceLocator
from app.core.canonical import canonical_sha256
from app.core.ids import deterministic_uuid

POST_PROCESSING_VERSION = "ocr_post_processing.v1"
MAX_RAW_TEXT_LENGTH = 10_000


class IdentifierCandidateType(StrEnum):
    PLATE_LIKE = "plate_like"
    PHONE_NUMBER = "phone_number"
    EMAIL_ADDRESS = "email_address"
    ACCOUNT_NUMBER = "account_number"
    REFERENCE_NUMBER = "reference_number"
    PLATFORM_HANDLE = "platform_handle"


@dataclass(frozen=True)
class OcrTokenProvenance:
    """A source token/line supplied by the OCR engine, never reconstructed."""

    raw_start: int
    raw_end: int
    line_index: int | None = None
    bbox_xyxy_normalized: BoundingBoxNormalized | None = None

    def __post_init__(self) -> None:
        if self.raw_start < 0 or self.raw_end <= self.raw_start:
            raise ValueError("OCR token span must be a non-empty ordered raw-text range")


@dataclass(frozen=True)
class OcrFragment:
    """Typed input from an OCR worker after it has selected a frame or ROI.

    ``source_locator`` is optional because a caller may only know a frame or
    ROI later, but case/evidence scope is mandatory.  Chunk, manifest,
    artifact, and prior-observation references are opaque IDs only: no URI,
    media bytes, credential, or raw engine payload belongs here.
    """

    case_id: UUID
    evidence_id: UUID
    raw_text: str
    ocr_engine_name: str
    ocr_engine_version: str
    ocr_configuration_hash: str
    extraction_confidence: float | None = None
    source_locator: SourceLocator | None = None
    language_hint: str | None = None
    script_hint: str | None = None
    observation_id: UUID | None = None
    chunk_id: UUID | None = None
    manifest_id: UUID | None = None
    artifact_id: UUID | None = None
    platform: str | None = None
    token_provenance: tuple[OcrTokenProvenance, ...] = ()

    def __post_init__(self) -> None:
        if len(self.raw_text) > MAX_RAW_TEXT_LENGTH:
            raise ValueError("OCR text exceeds the bounded post-processing input length")
        if (
            not self.ocr_engine_name
            or not self.ocr_engine_version
            or not self.ocr_configuration_hash
        ):
            raise ValueError("OCR engine name, version, and configuration hash are required")
        if self.extraction_confidence is not None and not 0 <= self.extraction_confidence <= 1:
            raise ValueError("OCR extraction confidence must be within [0, 1]")
        for token in self.token_provenance:
            if token.raw_end > len(self.raw_text):
                raise ValueError("OCR token span exceeds raw OCR text")


@dataclass(frozen=True)
class OcrPostProcessingProfile:
    """Explicit, versioned cleanup and extraction policy.

    The default preserves line boundaries.  It does not correct ``O`` to
    ``0`` (or any other visually similar character), dehyphenate, or make a
    linguistic correction.  ``matching_strip_punctuation`` affects only the
    matching display value, never raw text or identifier candidate text.
    """

    name: str = "conservative_ocr_fragment"
    version: str = "1.0.0"
    collapse_horizontal_whitespace: bool = True
    trim_line_edges: bool = True
    matching_strip_punctuation: bool = False
    extract_plate_like: bool = True
    extract_phone_numbers: bool = True
    extract_email_addresses: bool = True
    extract_labelled_account_references: bool = True
    extract_platform_handles: bool = True

    def config_hash(self) -> str:
        return canonical_sha256(
            {
                "utility_version": POST_PROCESSING_VERSION,
                "profile": {
                    "name": self.name,
                    "version": self.version,
                    "collapse_horizontal_whitespace": self.collapse_horizontal_whitespace,
                    "trim_line_edges": self.trim_line_edges,
                    "matching_strip_punctuation": self.matching_strip_punctuation,
                    "extract_plate_like": self.extract_plate_like,
                    "extract_phone_numbers": self.extract_phone_numbers,
                    "extract_email_addresses": self.extract_email_addresses,
                    "extract_labelled_account_references": self.extract_labelled_account_references,
                    "extract_platform_handles": self.extract_platform_handles,
                },
            }
        )


@dataclass(frozen=True)
class TextTransformation:
    name: str
    version: str
    matching_only: bool = False


@dataclass(frozen=True)
class IdentifierCandidate:
    """One unresolved regex candidate, anchored to exact raw OCR text."""

    candidate_id: UUID
    candidate_type: IdentifierCandidateType
    raw_matched_text: str
    normalized_matching_form: str
    raw_start: int
    raw_end: int
    extraction_rule: str
    extraction_rule_version: str
    case_id: UUID
    evidence_id: UUID
    source_locator: SourceLocator | None
    observation_id: UUID | None
    chunk_id: UUID | None
    manifest_id: UUID | None
    artifact_id: UUID | None
    source_tokens: tuple[OcrTokenProvenance, ...]
    warnings: tuple[str, ...] = ()
    extraction_confidence: float | None = None


@dataclass(frozen=True)
class OcrPostProcessingResult:
    """In-memory output; raw text is not graph-safe output."""

    fragment: OcrFragment
    cleaned_display_text: str
    normalized_matching_text: str
    transformations: tuple[TextTransformation, ...]
    configuration_hash: str
    warnings: tuple[str, ...]
    identifier_candidates: tuple[IdentifierCandidate, ...]


_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_INDIAN_PHONE = re.compile(r"(?<!\w)((?:\+91[ -]?)?(?:0[ -]?)?[6-9]\d{4}[ -]?\d{5})\b")
# Structural, India-style plate-like form only. It is not registration validation.
_PLATE_LIKE = re.compile(r"\b([A-Za-z]{2}[ -]?[0-9OI]{1,2}[ -]?[A-Za-z]{1,2}[ -]?\d{4})\b")
_ACCOUNT = re.compile(
    r"(?:A\/?C\.?\s*(?:No\.?)?|Account\s*(?:No\.?|Number)?)\s*[:\-]?\s*(\d{9,18})",
    re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"(?:Txn\.?\s*(?:ID|No\.?)?|Transaction\s*(?:ID|No\.?|Reference)?|UTR|"
    r"Reference\s*(?:No\.?)?)\s*[:\-]?\s*([A-Za-z0-9]{6,30})",
    re.IGNORECASE,
)
_HANDLE = re.compile(r"(?<![\w.@])(@[A-Za-z0-9_]{2,32})\b")


def post_process_ocr_fragment(
    fragment: OcrFragment, profile: OcrPostProcessingProfile | None = None
) -> OcrPostProcessingResult:
    """Produce deterministic display/matching variants and unresolved candidates."""
    profile = profile or OcrPostProcessingProfile()
    cleaned, source_map, warnings = _clean_display_text(fragment.raw_text, profile)
    matching = _matching_text(cleaned, profile)
    transformations = [
        TextTransformation("unicode_nfc", "1"),
        TextTransformation("remove_unsafe_controls", "1"),
        TextTransformation("line_preserving_whitespace", "1"),
        TextTransformation("matching_casefold", "1", matching_only=True),
    ]
    if profile.matching_strip_punctuation:
        transformations.append(
            TextTransformation("matching_strip_punctuation", "1", matching_only=True)
        )
    candidates = _extract_candidates(fragment, profile, cleaned, source_map)
    return OcrPostProcessingResult(
        fragment=fragment,
        cleaned_display_text=cleaned,
        normalized_matching_text=matching,
        transformations=tuple(transformations),
        configuration_hash=profile.config_hash(),
        warnings=tuple(sorted(set(warnings))),
        identifier_candidates=candidates,
    )


def _clean_display_text(
    raw: str, profile: OcrPostProcessingProfile
) -> tuple[str, tuple[tuple[int, int], ...], list[str]]:
    """NFC-normalize, remove unsafe controls, and preserve line boundaries."""
    warnings: list[str] = []
    chars: list[tuple[str, int, int]] = []
    # Normalize a base character plus its following combining marks as a
    # cluster, giving each normalized output character its exact raw range.
    index = 0
    while index < len(raw):
        end = index + 1
        while end < len(raw) and unicodedata.combining(raw[end]):
            end += 1
        normalized = unicodedata.normalize("NFC", raw[index:end])
        for char in normalized:
            chars.append((char, index, end))
        index = end

    filtered: list[tuple[str, int, int]] = []
    for position, (char, start, end) in enumerate(chars):
        if char == "\r" and position + 1 < len(chars) and chars[position + 1][0] == "\n":
            # A CRLF is one display line boundary, anchored by the LF's raw
            # span; do not fabricate an extra blank line.
            continue
        if unicodedata.category(char).startswith("C") and char not in "\n\r\t":
            warnings.append("unsafe_or_invisible_unicode_removed")
            continue
        filtered.append(("\n" if char == "\r" else char, start, end))

    lines: list[list[tuple[str, int, int]]] = [[]]
    for item in filtered:
        if item[0] == "\n":
            lines.append([])
        else:
            lines[-1].append(item)
    output: list[tuple[str, int, int]] = []
    for line_index, line in enumerate(lines):
        cleaned_line = _clean_line(line, profile)
        output.extend(cleaned_line)
        if line_index < len(lines) - 1:
            # The newline is still anchored to raw source even when a CRLF
            # source pair was represented by one retained line boundary.
            boundary = _line_boundary(filtered, line_index)
            output.append(("\n", boundary[0], boundary[1]))
    return (
        "".join(char for char, _, _ in output),
        tuple((start, end) for _, start, end in output),
        warnings,
    )


def _line_boundary(chars: list[tuple[str, int, int]], line_index: int) -> tuple[int, int]:
    seen = -1
    for char, start, end in chars:
        if char == "\n":
            seen += 1
            if seen == line_index:
                return start, end
    return (0, 0)


def _clean_line(
    line: list[tuple[str, int, int]], profile: OcrPostProcessingProfile
) -> list[tuple[str, int, int]]:
    output: list[tuple[str, int, int]] = []
    for char, start, end in line:
        is_horizontal_space = char.isspace()
        if is_horizontal_space and profile.collapse_horizontal_whitespace:
            if output and output[-1][0] == " ":
                previous = output[-1]
                output[-1] = (" ", previous[1], end)
            else:
                output.append((" ", start, end))
        else:
            output.append((char, start, end))
    if profile.trim_line_edges:
        while output and output[0][0] == " ":
            output.pop(0)
        while output and output[-1][0] == " ":
            output.pop()
    return output


def _matching_text(cleaned: str, profile: OcrPostProcessingProfile) -> str:
    matching = " ".join(cleaned.splitlines()).casefold()
    if profile.matching_strip_punctuation:
        matching = re.sub(r"[\u2010-\u2015\-._/]+", " ", matching)
        matching = " ".join(matching.split())
    return matching


def _extract_candidates(
    fragment: OcrFragment,
    profile: OcrPostProcessingProfile,
    cleaned: str,
    source_map: tuple[tuple[int, int], ...],
) -> tuple[IdentifierCandidate, ...]:
    specs: list[tuple[IdentifierCandidateType, re.Pattern[str], str]] = []
    if profile.extract_plate_like:
        specs.append((IdentifierCandidateType.PLATE_LIKE, _PLATE_LIKE, "india_style_plate_like"))
    if profile.extract_phone_numbers:
        specs.append((IdentifierCandidateType.PHONE_NUMBER, _INDIAN_PHONE, "indian_mobile"))
    if profile.extract_email_addresses:
        specs.append((IdentifierCandidateType.EMAIL_ADDRESS, _EMAIL, "email_address"))
    if profile.extract_labelled_account_references:
        specs.extend(
            [
                (IdentifierCandidateType.ACCOUNT_NUMBER, _ACCOUNT, "labelled_account_number"),
                (IdentifierCandidateType.REFERENCE_NUMBER, _REFERENCE, "labelled_reference_number"),
            ]
        )
    if profile.extract_platform_handles and fragment.platform:
        specs.append(
            (IdentifierCandidateType.PLATFORM_HANDLE, _HANDLE, "platform_scoped_at_handle")
        )

    candidates: list[IdentifierCandidate] = []
    seen: set[tuple[IdentifierCandidateType, int, int]] = set()
    for candidate_type, pattern, rule in specs:
        for match in pattern.finditer(cleaned):
            start, end = match.span(1)
            if start == end:
                continue
            raw_start = min(value[0] for value in source_map[start:end])
            raw_end = max(value[1] for value in source_map[start:end])
            key = (candidate_type, raw_start, raw_end)
            if key in seen:
                continue
            seen.add(key)
            raw_value = fragment.raw_text[raw_start:raw_end]
            normalized = re.sub(r"[\s-]+", "", match.group(1)).upper()
            warnings = _candidate_warnings(candidate_type, match.group(1))
            candidate_id = deterministic_uuid(
                "phase4_ocr_identifier_candidate",
                str(fragment.case_id),
                str(fragment.evidence_id),
                str(fragment.observation_id or ""),
                candidate_type.value,
                str(raw_start),
                str(raw_end),
                rule,
                POST_PROCESSING_VERSION,
            )
            source_tokens = tuple(
                token
                for token in fragment.token_provenance
                if token.raw_start < raw_end and token.raw_end > raw_start
            )
            candidates.append(
                IdentifierCandidate(
                    candidate_id=candidate_id,
                    candidate_type=candidate_type,
                    raw_matched_text=raw_value,
                    normalized_matching_form=normalized,
                    raw_start=raw_start,
                    raw_end=raw_end,
                    extraction_rule=rule,
                    extraction_rule_version=POST_PROCESSING_VERSION,
                    case_id=fragment.case_id,
                    evidence_id=fragment.evidence_id,
                    source_locator=fragment.source_locator,
                    observation_id=fragment.observation_id,
                    chunk_id=fragment.chunk_id,
                    manifest_id=fragment.manifest_id,
                    artifact_id=fragment.artifact_id,
                    source_tokens=source_tokens,
                    warnings=warnings,
                    extraction_confidence=fragment.extraction_confidence,
                )
            )
    return tuple(candidates)


def _candidate_warnings(candidate_type: IdentifierCandidateType, value: str) -> tuple[str, ...]:
    if candidate_type is IdentifierCandidateType.PLATE_LIKE:
        compact = re.sub(r"[\s-]+", "", value).upper()
        # O/I in a digit position and 0/1 in a letter position look plausible
        # in OCR but are never corrected or declared a valid registration.
        if any(char in "OI" for char in compact[2:4]) or any(char in "01" for char in compact[4:6]):
            return ("ambiguous_ocr_character", "not_jurisdictionally_validated")
        return ("not_jurisdictionally_validated",)
    return ()


__all__ = [
    "IdentifierCandidate",
    "IdentifierCandidateType",
    "MAX_RAW_TEXT_LENGTH",
    "OcrFragment",
    "OcrPostProcessingProfile",
    "OcrPostProcessingResult",
    "OcrTokenProvenance",
    "POST_PROCESSING_VERSION",
    "TextTransformation",
    "post_process_ocr_fragment",
]
