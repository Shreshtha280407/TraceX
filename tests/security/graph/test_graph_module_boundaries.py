"""Static checks: the graph module never touches object storage/worker-credential
internals, and its Phase 2 additions never reference a forbidden field name.

Mirrors `tests/security/evidence_lifecycle/test_evidence_lifecycle_boundaries.py`'s
static-AST approach. `app/modules/graph/` legitimately imports `neo4j` (it is
the one module whose entire job is talking to Neo4j) and, since Phase 2,
`app.modules.evidence_lifecycle` (a one-directional, sanctioned dependency --
see `outbox_repository.py`'s module docstring) -- neither is checked here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.modules.graph.schemas import (
    CaseGraphObservationsResponse,
    GraphEntityMentionView,
    GraphObservationView,
)

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "graph"

#: Object-storage / worker-credential client libraries and modules this
#: module has no legitimate reason to import: it is a Neo4j/PostgreSQL
#: projection service, never a MinIO client, never a worker-authentication
#: implementer.
_FORBIDDEN_LIBRARY_ROOTS = {"minio", "boto3", "botocore"}
_FORBIDDEN_DOTTED_MODULES = {"app.modules.access_control.worker_credentials"}

#: OCR/media/vector/ML libraries -- graph projection is not content analysis.
_FORBIDDEN_ML_LIBRARY_ROOTS = {
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

#: Fields the new Phase 2 *response models* (what the read API actually
#: returns to a caller) must never declare. `outbox_repository.py`
#: legitimately reconstructs a full `EvidenceRecordV1` (which does have an
#: `object_uri` field) to feed the pre-existing, already-reviewed
#: `project_evidence` -- that internal reconstruction is not a leak; only
#: the HTTP response shape is checked here.
_FORBIDDEN_RESPONSE_FIELDS = {
    "object_uri",
    "credential_digest",
    "claim_token",
    "worker_token",
    "credential",
    "password",
    "secret",
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
def test_no_object_storage_or_worker_credential_library_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_roots = _imported_module_roots(tree)
    forbidden_roots = imported_roots & _FORBIDDEN_LIBRARY_ROOTS
    assert not forbidden_roots, f"{path} imports {forbidden_roots} -- not this module's job"

    dotted = _imported_dotted_modules(tree)
    for forbidden in _FORBIDDEN_DOTTED_MODULES:
        matches = {m for m in dotted if m == forbidden or m.startswith(forbidden + ".")}
        assert not matches, f"{path} imports {matches} -- worker-credential internals"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_ocr_media_vector_or_ml_library_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    forbidden = imported & _FORBIDDEN_ML_LIBRARY_ROOTS
    assert not forbidden, f"{path} imports forbidden OCR/media/vector/ML library: {forbidden}"


def test_response_models_never_declare_a_forbidden_field() -> None:
    """Introspects the actual Pydantic response models, not source text.

    A precise check on what the HTTP response can actually contain --
    unlike a text scan, this can't be defeated (or falsely tripped) by a
    docstring, comment, or internal-only reconstruction of a full contract
    object that happens to have a same-named field elsewhere in the file.
    """
    for model in (CaseGraphObservationsResponse, GraphObservationView, GraphEntityMentionView):
        field_names = set(model.model_fields)
        forbidden = field_names & _FORBIDDEN_RESPONSE_FIELDS
        assert not forbidden, f"{model.__name__} declares forbidden field(s) {forbidden}"
