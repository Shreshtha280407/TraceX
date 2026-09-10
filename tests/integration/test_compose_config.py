"""CORE-COMPOSE-001: Docker Compose configuration validates.

Skips (never fabricates a pass) if the `docker` CLI isn't available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available in this environment"
)


def test_compose_config_is_valid() -> None:
    result = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
