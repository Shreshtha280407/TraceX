"""Master plan §23.3: resolved build identity (commit/hardware) never raises,
never silently blank, explicit override always wins over auto-detection."""

from __future__ import annotations

from app.core.build_info import (
    GIT_COMMIT_ENV_VAR,
    HARDWARE_LABEL_ENV_VAR,
    resolve_git_commit,
    resolve_hardware_label,
)


def test_resolve_git_commit_explicit_argument_wins(monkeypatch) -> None:
    monkeypatch.setenv(GIT_COMMIT_ENV_VAR, "should-not-be-used")
    assert resolve_git_commit("explicit-commit-hash") == "explicit-commit-hash"


def test_resolve_git_commit_reads_the_env_var_over_git_rev_parse(monkeypatch) -> None:
    monkeypatch.setenv(GIT_COMMIT_ENV_VAR, "env-supplied-commit-hash")
    assert resolve_git_commit() == "env-supplied-commit-hash"


def test_resolve_git_commit_falls_back_to_real_git_rev_parse(monkeypatch) -> None:
    """No explicit arg, no env var -- this repo's own checkout has real
    git history, so the live `git rev-parse HEAD` path must return a real,
    well-formed 40-character commit hash, never a crash and never blank."""
    monkeypatch.delenv(GIT_COMMIT_ENV_VAR, raising=False)
    commit = resolve_git_commit()
    assert commit != "unknown"
    assert len(commit) == 40
    assert all(character in "0123456789abcdef" for character in commit)


def test_resolve_git_commit_degrades_to_unknown_never_raises(monkeypatch, tmp_path) -> None:
    """Simulates the built-Docker-image case (`.dockerignore` excludes
    `.git`): `git rev-parse` run from a directory with no repository must
    degrade to the explicit placeholder, never raise."""
    monkeypatch.delenv(GIT_COMMIT_ENV_VAR, raising=False)
    monkeypatch.setattr("app.core.build_info._REPO_ROOT", tmp_path)
    assert resolve_git_commit() == "unknown"


def test_resolve_hardware_label_explicit_argument_wins(monkeypatch) -> None:
    monkeypatch.setenv(HARDWARE_LABEL_ENV_VAR, "should-not-be-used")
    assert resolve_hardware_label("explicit-hardware-label") == "explicit-hardware-label"


def test_resolve_hardware_label_reads_the_env_var_over_platform_node(monkeypatch) -> None:
    monkeypatch.setenv(HARDWARE_LABEL_ENV_VAR, "lan-laptop-2")
    assert resolve_hardware_label() == "lan-laptop-2"


def test_resolve_hardware_label_falls_back_to_platform_node(monkeypatch) -> None:
    monkeypatch.delenv(HARDWARE_LABEL_ENV_VAR, raising=False)
    label = resolve_hardware_label()
    assert label != "unknown"
    assert label.strip() != ""
