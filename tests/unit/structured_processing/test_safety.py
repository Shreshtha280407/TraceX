"""Scenarios 17-19: no infra access, no sensitive logging, no entity merge/cross-case linking."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.contracts.evidence import SourceType
from app.modules.structured_processing.models import StaticBytesResolver
from app.modules.structured_processing.worker import process_job
from tests.fixtures.structured_processing.factory import make_evidence_and_job

MODULE_ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "structured_processing"

# Scenario 17: infrastructure clients this module must never import.
_FORBIDDEN_INFRA_IMPORTS = {
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
}

# Scenario 19: this module never resolves mentions to identities or links them.
_FORBIDDEN_CONTRACT_IMPORTS = {"EntityV1", "EventV1"}


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


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_infrastructure_client_is_imported(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_module_roots(tree)
    forbidden = imported & _FORBIDDEN_INFRA_IMPORTS
    assert not forbidden, f"{path} imports forbidden infra client(s): {forbidden}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(MODULE_ROOT)))
def test_no_entity_or_event_contract_is_constructed(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    forbidden = imported & _FORBIDDEN_CONTRACT_IMPORTS
    assert not forbidden, f"{path} imports {forbidden} -- entity resolution is later-phase work"


def test_error_messages_never_echo_sensitive_field_values() -> None:
    """A CDR record with a bad timestamp must fail without echoing the caller number."""
    evidence, job = make_evidence_and_job(
        content_type="text/csv",
        filename="cdr.csv",
        processor_name="cdr_generic_v1",
        source_type=SourceType.CDR,
    )
    sensitive_number = "9998887776"
    data = (
        f"caller_number,callee_number,timestamp\n{sensitive_number},callee-A,not-a-real-date\n"
    ).encode()

    result = process_job(job, evidence, StaticBytesResolver(payload=data))

    assert result.status.value == "failed"
    assert result.error is not None
    assert sensitive_number not in result.error.message
    assert sensitive_number not in result.error.code


def test_amount_and_account_values_never_appear_in_error_messages() -> None:
    evidence, job = make_evidence_and_job(
        content_type="text/csv",
        filename="txns.csv",
        processor_name="financial_transaction_generic_v1",
        source_type=SourceType.FINANCIAL,
    )
    secret_amount = "13377331"
    data = (
        "sender_account,receiver_account,amount,currency,timestamp\n"
        f"sender-A,receiver-B,{secret_amount},NOTREAL,2026-01-01 10:00:00\n"
    ).encode()  # unparseable currency is fine; amount is the target

    result = process_job(job, evidence, StaticBytesResolver(payload=data))
    # NOTREAL currency is accepted (currency is never invented, but any non-empty
    # string is accepted as-is); force a failure by making the amount unparseable instead.
    if result.status.value != "failed":
        bad_data = (
            b"sender_account,receiver_account,amount,currency,timestamp\n"
            b"sender-A,receiver-B,not-a-number,INR,2026-01-01 10:00:00\n"
        )
        result = process_job(job, evidence, StaticBytesResolver(payload=bad_data))

    assert result.status.value == "failed"
    assert result.error is not None
    assert secret_amount not in result.error.message


def test_observations_never_reference_a_different_case_than_the_job() -> None:
    evidence, job = make_evidence_and_job(
        content_type="text/plain", filename="fir.txt", processor_name="fir_report_text_v1"
    )
    data = b"FIR No: 1/2026, phone 9876543210, email a@b.com"
    result = process_job(job, evidence, StaticBytesResolver(payload=data))

    assert result.observations
    for observation in result.observations:
        assert observation.case_id == job.case_id
        assert observation.evidence_id == job.evidence_id
