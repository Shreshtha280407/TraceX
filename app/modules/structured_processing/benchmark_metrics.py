"""Pure benchmark-metric functions: CER/WER, field extraction P/R/F1, latency, memory.

Every function here is deterministic and takes only already-in-memory
values (a reference string, a hypothesis string, small dicts of field
values) -- none of them touch a file, a network, or a real OCR/model
engine. This module is what makes `benchmark_adapters.py`'s aggregate
metrics independently testable with synthetic strings, and it is the only
place character/word-level text ever exists in this benchmark subsystem:
callers pass a reference/hypothesis pair in, get a numeric score out, and
must never persist the strings themselves in a `BenchmarkRunV1` (see
`benchmark_adapters.py`'s module docstring).

Percentile/latency and memory helpers report only what was actually
measured on this run, mirroring `app.modules.media_processing.performance`'s
"never a promised or extrapolated figure" policy -- `peak_memory_mb`
returns `None` (never a guess) when the platform doesn't expose a
resource-usage API.
"""

from __future__ import annotations

import math
import platform
import resource
from collections.abc import Mapping, Sequence


def _levenshtein_distance(reference: Sequence[object], hypothesis: Sequence[object]) -> int:
    """Classic O(len(reference) * len(hypothesis)) edit distance, single-row DP."""
    if reference == hypothesis:
        return 0
    previous_row = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, start=1):
        current_row = [i] + [0] * len(hypothesis)
        for j, hyp_item in enumerate(hypothesis, start=1):
            deletion = previous_row[j] + 1
            insertion = current_row[j - 1] + 1
            substitution = previous_row[j - 1] + (0 if ref_item == hyp_item else 1)
            current_row[j] = min(deletion, insertion, substitution)
        previous_row = current_row
    return previous_row[-1]


def character_error_rate(reference: str, hypothesis: str) -> float | None:
    """CER = edit_distance(chars) / len(reference chars).

    Returns `None` (never a fabricated `0.0`/`1.0`) when `reference` is
    empty -- CER is undefined with no reference text to measure against.
    """
    if not reference:
        return None
    distance = _levenshtein_distance(list(reference), list(hypothesis))
    return distance / len(reference)


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """WER = edit_distance(words) / word_count(reference)."""
    reference_words = reference.split()
    if not reference_words:
        return None
    hypothesis_words = hypothesis.split()
    distance = _levenshtein_distance(reference_words, hypothesis_words)
    return distance / len(reference_words)


def field_extraction_prf(
    expected: Mapping[str, str], extracted: Mapping[str, str]
) -> tuple[float | None, float | None, float | None]:
    """Precision/recall/F1 over exact `(field_kind, value)` matches.

    A field kind present in `expected` but not `extracted` (or with a
    different value) is a miss; a field kind in `extracted` not present in
    `expected` (or with a different value) is a false positive. Returns
    `(None, None, None)` when `expected` is empty -- there is nothing to
    score field extraction against without ground truth.
    """
    if not expected:
        return None, None, None
    expected_pairs = set(expected.items())
    extracted_pairs = set(extracted.items())
    true_positives = len(expected_pairs & extracted_pairs)
    precision = true_positives / len(extracted_pairs) if extracted_pairs else 0.0
    recall = true_positives / len(expected_pairs)
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (`pct` in `[0, 100]`) over already-measured values.

    Returns `None` for an empty sequence -- never a fabricated latency.
    """
    if not values:
        return None
    if not 0.0 <= pct <= 100.0:
        raise ValueError("pct must be within [0, 100]")
    ordered = sorted(values)
    rank = max(0, math.ceil((pct / 100.0) * len(ordered)) - 1)
    return ordered[rank]


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 50.0)


def peak_memory_mb() -> float | None:
    """This process's peak resident set size in MiB, or `None` if unavailable.

    `resource.getrusage(RUSAGE_SELF).ru_maxrss` is reported in **kilobytes**
    on Linux but **bytes** on macOS/BSD (the platform Aditya's MacBook
    validation actually runs on) -- normalizing by `platform.system()` here
    is required for a correct cross-machine comparison, not a cosmetic
    choice. `resource` itself is POSIX-only (absent on Windows), so this
    degrades to `None` there rather than raising.
    """
    try:
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError, ValueError):  # pragma: no cover - platform-dependent
        return None
    if max_rss <= 0:
        return None
    # macOS/BSD report ru_maxrss in bytes; Linux reports it in kilobytes.
    bytes_per_unit = 1.0 if platform.system() == "Darwin" else 1024.0
    return (max_rss * bytes_per_unit) / (1024.0 * 1024.0)


__all__ = [
    "character_error_rate",
    "field_extraction_prf",
    "median",
    "peak_memory_mb",
    "percentile",
    "word_error_rate",
]
