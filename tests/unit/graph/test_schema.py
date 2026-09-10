"""GRAPH-SCHEMA-001: every schema statement is idempotent and case-scoped."""

from __future__ import annotations

from app.modules.graph.schema import (
    CONSTRAINT_STATEMENTS,
    INDEX_STATEMENTS,
    get_schema_statements,
)

_CASE_SCOPED_LABELS = {
    "Evidence": "evidence_id",
    "Observation": "observation_id",
    "Entity": "entity_id",
    "Event": "event_id",
}


def test_every_statement_uses_if_not_exists() -> None:
    for statement in get_schema_statements():
        assert "IF NOT EXISTS" in statement.cypher, statement.name


def test_statement_names_are_unique() -> None:
    names = [s.name for s in get_schema_statements()]
    assert len(names) == len(set(names))


def test_case_node_constraint_is_bare_case_id_only() -> None:
    (case_constraint,) = [s for s in CONSTRAINT_STATEMENTS if s.name == "case_case_id_unique"]
    assert "REQUIRE c.case_id IS UNIQUE" in case_constraint.cypher
    # Bare, non-compound: Case is the case-isolation anchor itself.
    assert "case_id, " not in case_constraint.cypher


def test_evidence_observation_entity_event_constraints_are_compound_on_case_id() -> None:
    for label, domain_id_field in _CASE_SCOPED_LABELS.items():
        matches = [s for s in CONSTRAINT_STATEMENTS if label in s.cypher]
        assert len(matches) == 1, f"expected exactly one constraint statement for {label}"
        cypher = matches[0].cypher
        assert "case_id" in cypher
        assert domain_id_field in cypher
        assert "IS UNIQUE" in cypher
        # Compound identity: both case_id and the domain ID inside one REQUIRE tuple,
        # never a bare uniqueness constraint on the domain ID alone.
        assert "(" in cypher.split("REQUIRE")[1].split("IS UNIQUE")[0]


def test_expected_indexes_are_present() -> None:
    index_names = {s.name for s in INDEX_STATEMENTS}
    assert index_names == {
        "observation_case_id_idx",
        "observation_event_time_idx",
        "event_case_id_idx",
        "event_event_time_idx",
        "entity_case_id_idx",
        "entity_entity_type_idx",
    }


def test_get_schema_statements_is_deterministic_and_stable() -> None:
    first = get_schema_statements()
    second = get_schema_statements()
    assert first == second
    assert first[: len(CONSTRAINT_STATEMENTS)] == CONSTRAINT_STATEMENTS
