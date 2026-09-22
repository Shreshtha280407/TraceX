"""Gap-Closure WP-7B (G1): loads truth data for offline evaluation.

`TRACEX_SYNTHETIC_DATA_ROOT` is the one environment variable this module
ever reads for a filesystem root -- mirrors `structured_processing.
benchmark_validation`'s `TRACEX_BENCHMARK_DATA_ROOT` pattern exactly
(explicit env var, never a hardcoded path, never a Settings field baked
into the main app config since this is offline-evaluation-only, never a
live request path).

Truth data itself is authored in the sibling `TraceX-Synthetic-Data`
repository (out of this codebase's scope) -- see `docs/qa/known-
limitations.md`'s "WP-7B" section for the exact JSON schema this module
expects at `<root>/<case_id>/entity_resolution_truth.json`.

**Import boundary**: this module -- and `evaluation.py`, which depends on
it -- must never be imported by `app/api/*.py`, any `*_api.py` route
module, or `app/main.py`. `tests/unit/graph/test_intelligence_evaluation.
py::test_no_api_route_ever_imports_the_evaluator_or_truth_loader`
statically enforces this by grepping every route/API file's imports; it
is this module's real safety guarantee, not a comment. Offline evaluation
runs only from a CLI/test harness, never from a live request.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ENV_SYNTHETIC_DATA_ROOT = "TRACEX_SYNTHETIC_DATA_ROOT"

_TRUTH_FILENAME = "entity_resolution_truth.json"


class TruthLoadError(ValueError):
    """Raised when `TRACEX_SYNTHETIC_DATA_ROOT` is set but the requested
    case's truth file is missing or malformed. Never raised merely because
    the environment variable itself is unset -- see `resolve_synthetic_
    data_root`, which returns `None` (a clean skip) for that case."""


class EntityPairLabel(StrEnum):
    """A human-authored ground-truth judgement on one entity pair --
    never inferred, always sourced from the truth file."""

    SAME = "same"
    DIFFERENT = "different"


class TruthEntityPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    left_entity_id: UUID
    right_entity_id: UUID
    label: EntityPairLabel


class TruthSpec(BaseModel):
    """One case's entity-resolution ground truth.

    `schema_version` lets a future truth-spec revision change shape
    without this loader silently misinterpreting an old or new file --
    an unrecognized version raises `TruthLoadError` rather than guessing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    case_id: UUID
    entity_pairs: tuple[TruthEntityPair, ...]


TRUTH_SPEC_SCHEMA_VERSION = "entity_resolution_truth.v1"


def resolve_synthetic_data_root(explicit: Path | None = None) -> Path | None:
    """`None` (never an exception) when no root is configured -- the
    caller's signal to skip evaluation cleanly rather than fail. An
    explicit argument (a future CLI `--truth-root`) always wins over the
    environment variable, mirroring `resolve_benchmark_data_root`."""
    if explicit is not None:
        return explicit
    raw = os.environ.get(ENV_SYNTHETIC_DATA_ROOT)
    if not raw or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def load_truth_spec(root: Path, case_id: UUID) -> TruthSpec:
    """Load and validate `<root>/<case_id>/entity_resolution_truth.json`.

    Raises `TruthLoadError` for anything short of a fully valid, matching
    truth file -- never returns a partially-trusted or guessed spec.
    """
    path = root / str(case_id) / _TRUTH_FILENAME
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TruthLoadError(f"truth file not found or unreadable: {path}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise TruthLoadError(f"truth file is not valid JSON: {path}") from exc
    try:
        spec = TruthSpec.model_validate(payload)
    except Exception as exc:
        raise TruthLoadError(f"truth file does not match the expected schema: {path}") from exc
    if spec.schema_version != TRUTH_SPEC_SCHEMA_VERSION:
        raise TruthLoadError(
            f"truth file schema_version '{spec.schema_version}' is not "
            f"'{TRUTH_SPEC_SCHEMA_VERSION}': {path}"
        )
    if spec.case_id != case_id:
        raise TruthLoadError(
            f"truth file at {path} is for case_id {spec.case_id}, not the requested {case_id}"
        )
    return spec


__all__ = [
    "ENV_SYNTHETIC_DATA_ROOT",
    "TRUTH_SPEC_SCHEMA_VERSION",
    "EntityPairLabel",
    "TruthEntityPair",
    "TruthLoadError",
    "TruthSpec",
    "load_truth_spec",
    "resolve_synthetic_data_root",
]
