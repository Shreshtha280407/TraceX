"""Scenario 22: no access-control module imports graph, OCR, media, vector, or ML code.

Mirrors `tests/unit/structured_processing/test_safety.py`'s static-AST
approach for the equivalent check in that module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "access_control"

#: Sibling feature modules this module must never reach into -- each is
#: owned by a different contributor (Shreshtha's graph work, Jasraj's
#: document/structured-data processing).
_FORBIDDEN_SIBLING_MODULE_PATHS = {
    "app.modules.graph",
    "app.modules.structured_processing",
}

#: OCR/media/vector/ML libraries this module has no business importing --
#: access control is authentication and policy, not content analysis.
_FORBIDDEN_LIBRARY_ROOTS = {
    "pytesseract",
    "cv2",
    "PIL",
    "Pillow",
    "moviepy",
    "librosa",
    "whisper",
    "numpy",
    "scipy",
    "torch",
    "tensorflow",
    "sklearn",
    "faiss",
    "chromadb",
    "pinecone",
    "transformers",
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


def _imported_dotted_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_sibling_feature_module_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    dotted = _imported_dotted_modules(tree)
    for forbidden in _FORBIDDEN_SIBLING_MODULE_PATHS:
        matches = {m for m in dotted if m == forbidden or m.startswith(forbidden + ".")}
        assert not matches, f"{path} imports {matches} -- owned by a different module"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_ocr_media_vector_or_ml_library_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    forbidden = imported & _FORBIDDEN_LIBRARY_ROOTS
    assert not forbidden, f"{path} imports forbidden OCR/media/vector/ML library: {forbidden}"


def test_module_has_no_direct_neo4j_or_minio_dependency() -> None:
    # Access control is PostgreSQL + Redis only; graph/evidence-object
    # storage credentials are out of scope for this module entirely.
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_module_roots(tree)
        assert "neo4j" not in imported, f"{path} imports neo4j"
        assert "minio" not in imported, f"{path} imports minio"
