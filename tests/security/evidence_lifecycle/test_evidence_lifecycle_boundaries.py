"""No sibling-module reach-in, no OCR/media/ML libraries, no entity/event/observation construction.

Mirrors `tests/security/access_control/test_module_boundaries.py`'s static-AST
approach. Unlike `access_control`, this module legitimately imports
`sqlalchemy`, `redis`, and `minio` (evidence persistence and object storage
are exactly its job) -- only `neo4j` and content-analysis libraries are
forbidden here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "evidence_lifecycle"

#: Sibling feature modules this module must never reach into -- each is
#: owned by a different contributor and consumes `WorkerJobV1` from this
#: module, never the other way around.
_FORBIDDEN_SIBLING_MODULE_PATHS = {
    "app.modules.graph",
    "app.modules.structured_processing",
    "app.modules.communication_processing",
    "app.modules.media_processing",
}

#: OCR/media/vector/ML libraries this module has no business importing --
#: evidence lifecycle is upload/storage/persistence, not content analysis.
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

#: This module produces `WorkerJobV1`, and (Phase 2.1) validates/persists a
#: worker-*submitted* `WorkerResultV1`/`ObservationV1` -- it never
#: fabricates either from raw evidence content itself (that remains
#: extraction-module territory), and it never touches entity/event
#: resolution or worker progress reporting at all.
_FORBIDDEN_CONTRACT_IMPORTS = {
    "EntityV1",
    "EventV1",
    "WorkerProgressV1",
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


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


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


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_forbidden_contract_is_constructed(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    forbidden = imported & _FORBIDDEN_CONTRACT_IMPORTS
    assert not forbidden, f"{path} imports {forbidden} -- not this module's responsibility"


def test_module_has_no_direct_neo4j_dependency() -> None:
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _imported_module_roots(tree)
        assert "neo4j" not in imported, f"{path} imports neo4j"
