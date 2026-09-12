"""Deterministic text normalization with an exact offset map back to source.

Regex/NER/relation extraction all run against *normalized* text — collapsed
whitespace, NFC Unicode form, and line-wrap hyphenation removed — because a
pattern anchored on a label like `"FIR No."` or a name split across a
line-wrapped hyphen (`"Ram-\nesh"`) should still match. But every mention
this module's callers build still needs to point back at the *exact*
original character range for provenance (`SourceLocator.span_start/
span_end`), never at a position in text that doesn't exist in the source.

`normalize_text` therefore returns both the normalized string and an
`OffsetMap` recording, for every normalized character, exactly which
original-text character range produced it. `OffsetMap.to_source` turns a
normalized `[start, end)` span (e.g. a regex match) back into the original
span, so provenance is never lost, and nothing here ever invents text that
wasn't present in the original/OCR output — every rule is a pure,
deterministic rewrite of existing characters (drop, collapse, or re-form;
never insert new content).

Implementation note: each stage below is computed as a per-output-character
array of source positions (never a coarse "run" that could span more than
it should), then composed stage-by-stage by simple index lookups — this is
what keeps a query like `to_source(match.start(), match.end())` exact even
when the match crosses a whitespace-collapse or dehyphenation boundary.
`OffsetRun`s are only a compacted (merged-adjacent-entries), space-saving
*view* over that same exact per-character data, never a separate source of
truth.

Rules applied, in this fixed order (see `NORMALIZATION_VERSION`/
`normalization_config_hash` for the exact versioned configuration recorded
in transformation provenance):

1. Unicode NFC normalization (`unicodedata.normalize("NFC", ...)`).
2. Line-wrap dehyphenation: a hyphen immediately followed by a line break
   and a lowercase letter is removed along with the line break, rejoining
   the word (`"Ram-\nesh"` -> `"Ramesh"`). A hyphen followed by an
   uppercase letter or digit is left alone (more likely a genuine
   hyphenated compound/identifier than a wrapped word).
3. Whitespace collapsing: any run of whitespace (space, tab, newline,
   common OCR spacing artifacts) collapses to a single space.

Same input + same `NORMALIZATION_VERSION` always produces the same
normalized output and the same offset map — this is what lets downstream
observation IDs (deterministic over locator + profile version) stay stable
across repeated processing of the same evidence.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.core.canonical import canonical_sha256

NORMALIZATION_NAME = "layout_normalization_v1"
NORMALIZATION_VERSION = "1.0.0"

#: The exact, versioned configuration this normalizer applies — recorded
#: verbatim (via its hash) in every `TransformationProvenanceV1.config_hash`
#: for a `layout_normalization` step, so a future change to these rules is
#: always visible in provenance rather than silently changing past output.
NORMALIZATION_CONFIG: dict[str, object] = {
    "unicode_form": "NFC",
    "dehyphenate_line_wraps": True,
    "collapse_whitespace": True,
}


def normalization_config_hash() -> str:
    return canonical_sha256(NORMALIZATION_CONFIG)


@dataclass(frozen=True)
class OffsetRun:
    """One contiguous, compacted run of the normalized text and where it came from.

    `norm_start`/`norm_end` index into the normalized string;
    `source_start`/`source_end` index into the original string. Purely a
    compacted *view* for inspection/debugging — `OffsetMap.to_source` never
    relies on runs being maximally merged, only on the exact per-character
    arrays underlying them.
    """

    norm_start: int
    norm_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class OffsetMap:
    """Maps any `[start, end)` range in normalized text back to the source range.

    `char_source_start[i]`/`char_source_end[i]` give the exact original-text
    range that produced normalized character `i` — the sole source of
    truth; `runs` (see `OffsetRun`) is a compacted view derived from these
    same arrays for readability only.
    """

    char_source_start: tuple[int, ...]
    char_source_end: tuple[int, ...]

    def to_source(self, norm_start: int, norm_end: int) -> tuple[int, int]:
        """The exact original-text `[start, end)` span covering a normalized-text span.

        Raises `ValueError` for an out-of-range or empty span — a
        programming error in the caller, not a data problem.
        """
        if norm_start < 0 or norm_end > len(self.char_source_start) or norm_start >= norm_end:
            raise ValueError(
                f"normalized span [{norm_start}, {norm_end}) is out of range for this offset map "
                f"of length {len(self.char_source_start)}"
            )
        source_start = min(self.char_source_start[norm_start:norm_end])
        source_end = max(self.char_source_end[norm_start:norm_end])
        return source_start, source_end

    @property
    def runs(self) -> tuple[OffsetRun, ...]:
        """A compacted, human-readable view: adjacent characters with contiguous
        source ranges merged into one run."""
        if not self.char_source_start:
            return ()
        runs: list[OffsetRun] = []
        run_norm_start = 0
        run_src_start = self.char_source_start[0]
        run_src_end = self.char_source_end[0]
        for i in range(1, len(self.char_source_start)):
            if self.char_source_start[i] == run_src_end:
                run_src_end = self.char_source_end[i]
                continue
            runs.append(OffsetRun(run_norm_start, i, run_src_start, run_src_end))
            run_norm_start = i
            run_src_start = self.char_source_start[i]
            run_src_end = self.char_source_end[i]
        runs.append(
            OffsetRun(run_norm_start, len(self.char_source_start), run_src_start, run_src_end)
        )
        return tuple(runs)


@dataclass(frozen=True)
class NormalizedText:
    """The result of normalizing one source text segment."""

    normalized: str
    offset_map: OffsetMap
    source_text: str


def normalize_text(text: str) -> NormalizedText:
    """Normalize `text` deterministically, keeping an exact offset map back to it."""
    nfc = unicodedata.normalize("NFC", text)
    # NFC is length-changing only for decomposed/composed sequences this
    # project's fixtures/regex patterns never rely on (ASCII and common
    # punctuation are already NFC-stable); nfc offsets are therefore used
    # directly as offsets into the original `text`.
    dehyphenated, dehyph_src_start, dehyph_src_end = _dehyphenate(nfc)
    collapsed, collapse_dehyph_start, collapse_dehyph_end = _collapse_whitespace(dehyphenated)

    char_source_start = tuple(dehyph_src_start[i] for i in collapse_dehyph_start)
    char_source_end = tuple(
        dehyph_src_end[i - 1] if i > 0 else dehyph_src_start[0] if dehyph_src_start else 0
        for i in collapse_dehyph_end
    )

    return NormalizedText(
        normalized=collapsed,
        offset_map=OffsetMap(char_source_start, char_source_end),
        source_text=text,
    )


def _next_non_newline_is_lowercase(text: str, from_index: int) -> bool:
    j = from_index
    while j < len(text) and text[j] in "\r\n":
        j += 1
    return j < len(text) and text[j].isalpha() and text[j].islower()


def _dehyphenate(text: str) -> tuple[str, list[int], list[int]]:
    """Remove a line-wrap hyphen (`-\\n` or `-\\r\\n`) followed by a lowercase letter.

    Returns the dehyphenated text plus, for every output character `j`,
    `src_start[j]`/`src_end[j] = src_start[j] + 1` — the exact single
    source-text index that produced it (dehyphenation is a pure deletion of
    the hyphen and line-break characters; every surviving character is a
    direct 1:1 copy of a source character, never a merge).
    """
    out_chars: list[str] = []
    src_start: list[int] = []
    i = 0
    n = len(text)

    while i < n:
        if (
            text[i] == "-"
            and i + 1 < n
            and text[i + 1] in "\r\n"
            and _next_non_newline_is_lowercase(text, i + 1)
        ):
            j = i + 1
            if text[j] == "\r" and j + 1 < n and text[j + 1] == "\n":
                j += 1
            i = j + 1  # drop the hyphen and the line break entirely
        else:
            out_chars.append(text[i])
            src_start.append(i)
            i += 1

    src_end = [s + 1 for s in src_start]
    return "".join(out_chars), src_start, src_end


def _collapse_whitespace(text: str) -> tuple[str, list[int], list[int]]:
    """Collapse every run of whitespace to a single space.

    Returns the collapsed text plus, for every output character `k`,
    `src_start[k]`/`src_end[k]` — the `[start, end)` range in `text` (the
    dehyphenated text) that produced it. A collapsed-whitespace output
    character legitimately maps to the *entire* original whitespace run
    (more than one input character); every other output character maps
    1:1 to a single input character.
    """
    out_chars: list[str] = []
    src_start: list[int] = []
    src_end: list[int] = []
    i = 0
    n = len(text)

    while i < n:
        if text[i].isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            out_chars.append(" ")
            src_start.append(start)
            src_end.append(i)
        else:
            out_chars.append(text[i])
            src_start.append(i)
            src_end.append(i + 1)
            i += 1

    return "".join(out_chars), src_start, src_end


__all__ = [
    "NORMALIZATION_CONFIG",
    "NORMALIZATION_NAME",
    "NORMALIZATION_VERSION",
    "NormalizedText",
    "OffsetMap",
    "OffsetRun",
    "normalization_config_hash",
    "normalize_text",
]
