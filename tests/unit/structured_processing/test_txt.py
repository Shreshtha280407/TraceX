"""Scenario 1: valid TXT extraction with deterministic spans and observations."""

from __future__ import annotations

import pytest

from app.modules.structured_processing.document.fir_report import extract_fir_mentions
from app.modules.structured_processing.document.txt import extract_txt


def test_txt_extraction_produces_single_segment_with_no_page() -> None:
    segments = extract_txt(b"FIR No: 12/2026\nPhone: 9876543210\n")
    assert len(segments) == 1
    assert segments[0].page is None
    assert "FIR No: 12/2026" in segments[0].text


def test_txt_extraction_is_deterministic() -> None:
    data = b"Section 420 IPC, phone 9876543210"
    first = extract_txt(data)
    second = extract_txt(data)
    assert first == second


def test_txt_utf8_bom_is_stripped() -> None:
    segments = extract_txt(b"\xef\xbb\xbfHello world")
    assert segments[0].text == "Hello world"


def test_txt_invalid_utf8_falls_back_to_latin1_without_raising() -> None:
    # 0xE9 alone is invalid UTF-8 but a valid Latin-1 byte ('é').
    segments = extract_txt(b"Caf\xe9 report")
    assert "Caf" in segments[0].text


def test_txt_spans_are_exact_offsets_into_extracted_text() -> None:
    text = "prefix FIR No: 55/2026 suffix"
    segments = extract_txt(text.encode())
    mentions = extract_fir_mentions(segments)
    fir_mentions = [m for m in mentions if m.observation_type == "fir_reference"]
    assert len(fir_mentions) == 1
    mention = fir_mentions[0]
    span_start = mention.locator.span_start
    span_end = mention.locator.span_end
    assert span_start is not None
    assert span_end is not None
    assert text[span_start:span_end] == mention.text


@pytest.mark.parametrize("filler", ["", "x" * 10])
def test_txt_handles_short_and_empty_input(filler: str) -> None:
    segments = extract_txt(filler.encode())
    assert segments[0].text == filler
