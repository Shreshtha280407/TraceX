"""Scenario 22 and 30: no entity/identity merge, no forbidden imports anywhere in this module."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "communication_processing"

# Scenario 30: infrastructure/ML/subprocess clients this module must never
# import. `httpx` is deliberately *not* forbidden (Phase 2): `client.py`
# legitimately needs an HTTP client to reach Nipun's internal worker API
# (`/api/v1/internal/worker-jobs/*`) -- the same, already-reviewed
# reasoning `structured_processing`'s identical static-safety test applies
# to its own `client.py`. Direct PostgreSQL/Neo4j/Redis/MinIO/queue access
# remains forbidden, unchanged.
_FORBIDDEN_IMPORTS = {
    "psycopg2",
    "psycopg",
    "asyncpg",
    "sqlalchemy",
    "neo4j",
    "redis",
    "minio",
    "celery",
    "kafka",
    "pika",
    "requests",
    "urllib3",
    "subprocess",
    "torch",
    "transformers",
    "whisper",
    "faster_whisper",
    "pyannote",
    "speechbrain",
    "librosa",
    "sklearn",
    "numpy",
}

# Scenario 22: this module never resolves mentions/labels/aliases to identities.
_FORBIDDEN_CONTRACT_IMPORTS = {"EntityV1", "EventV1"}

# Phase 4 narrowly authorizes these two offline bridges to invoke an
# operator-configured local executable with fixed argv.  No other module may
# spawn a process, and the adapters never use a shell or network client.
_LOCAL_COMMAND_BACKENDS = {
    "audio/asr_adapter.py",
    "audio/diarization_adapter.py",
}


def _python_files() -> list[Path]:
    return sorted(MODULE_ROOT.rglob("*.py"))


def _imported_module_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _calls_named(tree: ast.Module, forbidden: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in forbidden:
                found.add(name)
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_forbidden_infra_or_ml_import(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    forbidden = imported & _FORBIDDEN_IMPORTS
    if str(path.relative_to(MODULE_ROOT)) in _LOCAL_COMMAND_BACKENDS:
        forbidden.discard("subprocess")
    assert not forbidden, f"{path} imports forbidden module(s): {forbidden}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_entity_or_event_contract_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    forbidden = imported & _FORBIDDEN_CONTRACT_IMPORTS
    assert not forbidden, f"{path} imports {forbidden} -- entity resolution is out of scope"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_eval_exec_or_pickle(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden_calls = _calls_named(tree, {"eval", "exec"})
    assert not forbidden_calls, f"{path} calls forbidden function(s): {forbidden_calls}"
    imported = _imported_module_roots(tree)
    assert "pickle" not in imported, f"{path} imports pickle"
    assert "yaml" not in imported, f"{path} imports yaml"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_subprocess_or_shell_execution(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    relative_path = str(path.relative_to(MODULE_ROOT))
    assert "subprocess" not in imported or relative_path in _LOCAL_COMMAND_BACKENDS, (
        f"{path} imports subprocess"
    )
    assert "os" not in imported, f"{path} imports os -- this module never needs OS-level access"
