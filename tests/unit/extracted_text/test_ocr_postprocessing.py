"""Synthetic unit coverage for Phase 4 OCR post-processing utilities."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.contracts.common import BoundingBoxNormalized, SourceLocator
from app.modules.extracted_text.ocr_postprocessing import (
    IdentifierCandidateType,
    OcrFragment,
    OcrPostProcessingProfile,
    OcrTokenProvenance,
    post_process_ocr_fragment,
)


def _fragment(raw_text: str, **overrides: object) -> OcrFragment:
    return OcrFragment(
        case_id=uuid4(),
        evidence_id=uuid4(),
        raw_text=raw_text,
        ocr_engine_name="fixture_ocr",
        ocr_engine_version="1.0",
        ocr_configuration_hash="fixture-config",
        extraction_confidence=0.8,
        source_locator=SourceLocator(
            frame_number=7,
            time_start_ms=1200,
            time_end_ms=1400,
            bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5),
        ),
        chunk_id=uuid4(),
        manifest_id=uuid4(),
        artifact_id=uuid4(),
        **overrides,
    )


def test_raw_text_is_unchanged_and_variants_are_distinct() -> None:
    fragment = _fragment("  Cafe\u0301\t\tLine one\r\nLine two  ")
    result = post_process_ocr_fragment(fragment)

    assert result.fragment.raw_text == "  Cafe\u0301\t\tLine one\r\nLine two  "
    assert result.cleaned_display_text == "Café Line one\nLine two"
    assert result.normalized_matching_text == "café line one line two"
    assert [step.name for step in result.transformations] == [
        "unicode_nfc",
        "remove_unsafe_controls",
        "line_preserving_whitespace",
        "matching_casefold",
    ]


def test_cleanup_and_configuration_hash_are_deterministic() -> None:
    fragment = _fragment("DL 01 AB 1234")
    first = post_process_ocr_fragment(fragment)
    second = post_process_ocr_fragment(fragment)
    changed = post_process_ocr_fragment(
        fragment, OcrPostProcessingProfile(matching_strip_punctuation=True)
    )

    assert first.cleaned_display_text == second.cleaned_display_text
    assert first.configuration_hash == second.configuration_hash
    assert first.identifier_candidates == second.identifier_candidates
    assert changed.configuration_hash != first.configuration_hash


def test_controls_and_surrogate_are_safe_without_overwriting_raw_text() -> None:
    raw = "DL\u200b 01\x00 AB 1234 \ud800"
    result = post_process_ocr_fragment(_fragment(raw))

    assert result.fragment.raw_text == raw
    assert result.cleaned_display_text == "DL 01 AB 1234"
    assert "unsafe_or_invisible_unicode_removed" in result.warnings


def test_plate_candidate_preserves_span_lineage_and_ambiguity() -> None:
    raw = "camera DL O1 AB 1234"
    start = raw.index("DL")
    token = OcrTokenProvenance(
        raw_start=start,
        raw_end=len(raw),
        line_index=3,
        bbox_xyxy_normalized=BoundingBoxNormalized(x_min=0.1, y_min=0.1, x_max=0.5, y_max=0.3),
    )
    fragment = _fragment(raw, token_provenance=(token,))
    result = post_process_ocr_fragment(fragment)
    candidate = next(
        item
        for item in result.identifier_candidates
        if item.candidate_type is IdentifierCandidateType.PLATE_LIKE
    )

    assert candidate.raw_matched_text == "DL O1 AB 1234"
    assert candidate.normalized_matching_form == "DLO1AB1234"
    assert candidate.raw_start == start
    assert candidate.source_locator == fragment.source_locator
    assert candidate.source_tokens == (token,)
    assert "ambiguous_ocr_character" in candidate.warnings
    assert candidate.case_id == fragment.case_id
    assert candidate.evidence_id == fragment.evidence_id


def test_conservative_identifier_categories_require_documented_context() -> None:
    fragment = _fragment(
        "Call +91 98765-43210 or a@b.example. Account No: 123456789. "
        "UTR: ABCD1234. contact @safe_handle"
    )
    no_platform = post_process_ocr_fragment(fragment)
    platform = post_process_ocr_fragment(_fragment(fragment.raw_text, platform="telegram"))

    kinds = {candidate.candidate_type for candidate in no_platform.identifier_candidates}
    assert {
        IdentifierCandidateType.PHONE_NUMBER,
        IdentifierCandidateType.EMAIL_ADDRESS,
        IdentifierCandidateType.ACCOUNT_NUMBER,
        IdentifierCandidateType.REFERENCE_NUMBER,
    } <= kinds
    assert IdentifierCandidateType.PLATFORM_HANDLE not in kinds
    assert IdentifierCandidateType.PLATFORM_HANDLE in {
        candidate.candidate_type for candidate in platform.identifier_candidates
    }


def test_false_positive_and_malformed_candidates_produce_no_identifier() -> None:
    result = post_process_ocr_fragment(_fragment("ID 12, maybe 12345, @x, DL ABC 12"))
    assert result.identifier_candidates == ()


def test_same_source_span_is_deduplicated_deterministically() -> None:
    fragment = _fragment("Account No: 123456789 Account No: 123456789")
    result = post_process_ocr_fragment(fragment)
    accounts = [
        item
        for item in result.identifier_candidates
        if item.candidate_type is IdentifierCandidateType.ACCOUNT_NUMBER
    ]
    assert len(accounts) == 2  # same value, distinct evidence spans
    replay = post_process_ocr_fragment(fragment)
    assert [item.candidate_id for item in accounts] == [
        item.candidate_id
        for item in replay.identifier_candidates
        if item.candidate_type is IdentifierCandidateType.ACCOUNT_NUMBER
    ]


def test_token_ranges_and_bounded_input_are_validated() -> None:
    with pytest.raises(ValueError, match="token span"):
        _fragment("tiny", token_provenance=(OcrTokenProvenance(raw_start=0, raw_end=8),))
    with pytest.raises(ValueError, match="bounded"):
        _fragment("x" * 10_001)
