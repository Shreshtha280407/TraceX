"""Scenario 8, 9 (JSON half), 10 (JSON half): exact JSON paths, malformed input, limits."""

from __future__ import annotations

import json

import pytest

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.structured import json_parser


def test_parse_json_records_from_bare_array() -> None:
    data = json.dumps([{"caller_number": "9876543210"}, {"caller_number": "9123456789"}]).encode()
    records = json_parser.parse_json_records(data)

    assert len(records) == 2
    assert records[0].values == {"caller_number": "9876543210"}
    locator = records[0].locator_for("caller_number")
    assert locator.json_path == "$[0].caller_number"
    assert records[1].locator_for(None).json_path == "$[1]"


def test_parse_json_records_unwraps_single_array_valued_key() -> None:
    data = json.dumps({"records": [{"a": "1"}]}).encode()
    records = json_parser.parse_json_records(data)
    assert records[0].values == {"a": "1"}


def test_parse_json_records_rejects_non_array_top_level() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        json_parser.parse_json_records(json.dumps({"a": "1", "b": "2"}).encode())
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON


def test_malformed_json_fails_safely() -> None:
    with pytest.raises(ProcessingError) as exc_info:
        json_parser.parse_json_records(b"{not valid json")
    assert exc_info.value.code == ErrorCode.MALFORMED_JSON


def test_traverse_json_scalars_yields_exact_paths() -> None:
    data = json.dumps({"a": 1, "b": {"c": "x"}, "d": [10, 20]}).encode()
    results = dict(json_parser.traverse_json_scalars(data))

    assert results["$.a"] == 1
    assert results["$.b.c"] == "x"
    assert results["$.d[0]"] == 10
    assert results["$.d[1]"] == 20


def test_traverse_json_scalars_skips_null() -> None:
    data = json.dumps({"a": None, "b": 1}).encode()
    results = dict(json_parser.traverse_json_scalars(data))
    assert "$.a" not in results
    assert results["$.b"] == 1


def test_traverse_json_scalars_depth_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_parser, "MAX_JSON_DEPTH", 2)
    nested: dict[str, object] = {"x": 1}
    for _ in range(5):
        nested = {"x": nested}
    with pytest.raises(ProcessingError) as exc_info:
        json_parser.traverse_json_scalars(json.dumps(nested).encode())
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_traverse_json_scalars_node_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_parser, "MAX_JSON_NODES", 3)
    data = json.dumps({"a": 1, "b": 2, "c": 3, "d": 4}).encode()
    with pytest.raises(ProcessingError) as exc_info:
        json_parser.traverse_json_scalars(data)
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED


def test_json_input_size_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(json_parser, "MAX_INPUT_BYTES", 10)
    with pytest.raises(ProcessingError) as exc_info:
        json_parser.traverse_json_scalars(json.dumps({"a": "way too long for the limit"}).encode())
    assert exc_info.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED
