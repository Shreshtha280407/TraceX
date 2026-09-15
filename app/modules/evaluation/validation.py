"""Shared safe-content validators reused across the evaluation module.

Mirrors the forbidden-substring recursive scan established in
`app.modules.graph.integration_models._validate_feature_value` and
`app.modules.integrity.models._validate_metadata_value`: reject a
source-like or secret-like payload at every nesting level, not only the
root. Nothing in this module ever stores raw evidence, transcript, chat,
OCR text, credentials, or a private local filesystem path -- only safe,
structural benchmark/dataset metadata.
"""

from __future__ import annotations

from typing import Any

#: Tokens forbidden in any free-text description field (`license_notes`,
#: `known_limitations`, `allowed_tasks`, a run's `failure_reason_safe`,
#: ...). Deliberately narrow -- credential/secret shapes only, and each
#: token is a whole-word marker, not a loose substring, so ordinary prose
#: mentioning "transcription" (an ASR task), "email headers" (a dataset's
#: record types), or "phone number normalization" (a CDR limitation) is
#: never caught by a naive substring match against a *content-type* noun
#: like "transcript". This module never carries raw evidence content in
#: the first place -- every field here is already meta-description -- so
#: the real, narrow risk this guards against is an accidentally pasted
#: credential/secret value, not ordinary domain vocabulary.
FORBIDDEN_CONTENT_TOKENS = frozenset(
    {
        "password",
        "secret",
        "credential",
        "object_uri",
        "dsn",
        "private_key",
        "api_key",
        "bearer",
    }
)

#: Tokens additionally forbidden as a *metric dict key* specifically (never
#: in the free-text sets above). A metric value is type-constrained to
#: `float | int | None`, so it cannot itself carry a raw string -- but a
#: key literally named e.g. `"phone_number"` on an otherwise-innocuous
#: numeric field is exactly how a real identifier could be smuggled through
#: a "just a number" value. Prose describing what a task covers is not
#: affected -- this set is checked only against `BenchmarkRunV1.metrics`
#: keys, never against a description field.
FORBIDDEN_METRIC_KEY_TOKENS = FORBIDDEN_CONTENT_TOKENS | frozenset(
    {
        "phone_number",
        "phone",
        "email",
        "aadhaar",
        "pan_number",
        "full_name",
        "account_number",
        "token",
    }
)

#: Local-data roots this module's `.gitignore` entries exclude -- every
#: `local_path_placeholder` must resolve under exactly one of these, never
#: an absolute path or a path outside the repository's own data-safety
#: convention. See `docs/architecture/phase-7-evaluation-and-model-governance.md`.
ALLOWED_LOCAL_DATA_PREFIXES = (
    "local-data/",
    "model-cache/",
    "benchmark-runs/",
    "private-evaluation/",
    "operation-nightfall-truth/",
)

_MAX_SAFE_STRING_LENGTH = 2_000


class UnsafeContentError(ValueError):
    """Raised when a value contains forbidden content or an unsafe path."""


def _normalized(value: str) -> str:
    return value.strip().lower()


def check_forbidden_tokens(
    value: Any, *, forbidden: frozenset[str] = FORBIDDEN_CONTENT_TOKENS
) -> None:
    """Recursively reject a forbidden substring in any string/key, at any nesting depth."""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = _normalized(str(key))
            if any(token in normalized_key for token in forbidden):
                raise UnsafeContentError(f"key '{key}' contains a prohibited token")
            check_forbidden_tokens(nested, forbidden=forbidden)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            check_forbidden_tokens(nested, forbidden=forbidden)
    elif isinstance(value, str):
        normalized_value = _normalized(value)
        if any(token in normalized_value for token in forbidden):
            raise UnsafeContentError("value contains a prohibited token")
        if len(value) > _MAX_SAFE_STRING_LENGTH:
            raise UnsafeContentError("string values must be bounded")


def check_safe_relative_path(path: str) -> None:
    """Reject an absolute path, a `..` traversal segment, or a path outside
    one of the repository's documented gitignored local-data roots."""
    if not path or path.strip() != path:
        raise UnsafeContentError("path must be non-empty and not padded with whitespace")
    if path.startswith("/") or path.startswith("~") or ":" in path.split("/")[0]:
        raise UnsafeContentError("path must be relative, never absolute or home-relative")
    segments = path.split("/")
    if any(segment == ".." for segment in segments):
        raise UnsafeContentError("path must not contain a '..' traversal segment")
    if not any(path.startswith(prefix) for prefix in ALLOWED_LOCAL_DATA_PREFIXES):
        raise UnsafeContentError(f"path must start with one of {ALLOWED_LOCAL_DATA_PREFIXES}")
