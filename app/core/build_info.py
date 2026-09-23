"""Runtime build/deployment identity: which commit, which machine.

Used by `intelligence.evaluation.OfflineEvaluationReport` (master plan
§23.3: "Performance and accuracy reports name dataset hash, source
profile, model/config, hardware and commit") so a reported metric can
always be traced back to exactly which code and which machine produced
it.

Neither value is a secret and neither is required for the app to run --
both degrade to an explicit `"unknown"` placeholder rather than crashing
or silently omitting the field, matching this codebase's "never a
fabricated number, but never a missing one either" convention (see
`evaluation.py`'s own `temporal_boundary_correctness: None` precedent).
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

#: An operator/CI can set this explicitly (e.g. baked into a Docker image
#: at build time via a build ARG, or set in a deployed container's own
#: environment) -- takes precedence over `git rev-parse` below, since a
#: built image's `.git` directory is deliberately excluded from the
#: Docker build context (see `.dockerignore`) and has no working git
#: history to inspect at all.
GIT_COMMIT_ENV_VAR = "TRACEX_BUILD_COMMIT"
#: Same pattern for the hardware/host label -- lets an operator supply a
#: stable, human-meaningful name (e.g. "lan-laptop-2" for the Phase 8
#: three-laptop rehearsal) instead of whatever `platform.node()` returns
#: inside a container, which is often an opaque container ID rather than
#: something that identifies the physical machine.
HARDWARE_LABEL_ENV_VAR = "TRACEX_HARDWARE_LABEL"

_UNKNOWN = "unknown"
#: `app/core/build_info.py` -> parents[2] is the repo root (`app/core/` ->
#: `app/` -> repo root) -- correct for a host `uv run ...` invocation
#: (this codebase's own documented pattern for `--evaluate`), irrelevant
#: inside a built image (no `.git` present there at all; the `git rev-
#: parse` call below fails cleanly and falls through to `_UNKNOWN`).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_git_commit(explicit: str | None = None) -> str:
    """The commit this process is running from, best-effort, never raising.

    `explicit` wins first; then `TRACEX_BUILD_COMMIT`; then a live `git
    rev-parse HEAD` against this file's own repo root. Falls back to
    `"unknown"` -- never a crash, never a blank/missing report field.
    """
    if explicit:
        return explicit
    from_env = os.environ.get(GIT_COMMIT_ENV_VAR)
    if from_env:
        return from_env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    return result.stdout.strip() or _UNKNOWN


def resolve_hardware_label(explicit: str | None = None) -> str:
    """A best-effort identifier for the machine this process runs on.

    `explicit` wins first; then `TRACEX_HARDWARE_LABEL`; then
    `platform.node()` (the OS hostname). Falls back to `"unknown"` --
    never raises.
    """
    if explicit:
        return explicit
    from_env = os.environ.get(HARDWARE_LABEL_ENV_VAR)
    if from_env:
        return from_env
    try:
        return platform.node() or _UNKNOWN
    except OSError:
        return _UNKNOWN


__all__ = [
    "GIT_COMMIT_ENV_VAR",
    "HARDWARE_LABEL_ENV_VAR",
    "resolve_git_commit",
    "resolve_hardware_label",
]
