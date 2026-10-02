"""Source-schema profiles, tolerant field encodings and the `tracex-dataset` intake check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.adapters import ParsedRow
from app.engine.canonical import NormalizationError, normalize_row
from app.engine.canonical import normalize as normalize_module
from app.engine.canonical.profiles import canonicalize, describe
from app.engine.dataset_cli import inspect, register

TXID = "ab" * 32


def _normalize(value: dict) -> dict:
    row = ParsedRow(1, "csv_logical_record", "record:1", value)
    return normalize_row(case_id="c", source_id="s", source_sha256="0" * 64, row=row)


def test_canonical_rows_pass_through_untouched() -> None:
    raw = {"txid": TXID, "timestamp": "2026-01-01T00:00:00Z", "output_addresses": ["a"], "output_amounts": [1]}
    value, renames = canonicalize(raw)
    assert value is raw and renames == {}


def test_ps_style_headers_map_onto_v1_fields() -> None:
    raw = {"Timestamp": "2026-01-01T00:00:00Z", "Source IP": "8.8.8.8", "Destination Port": "8333", "TxID": TXID,
           "From Addresses": "x", "Output Values": "1", "Country": "US", "AS Number": "AS15169", "extra": 1}
    value, renames = canonicalize(raw)
    assert renames == {"Timestamp": "timestamp", "Source IP": "src_ip", "Destination Port": "dst_port",
                       "TxID": "txid", "From Addresses": "input_addresses", "Output Values": "output_amounts",
                       "Country": "geo_country", "AS Number": "asn"}
    assert value["extra"] == 1  # unknown columns are kept, never dropped
    # A canonical key present in the same record wins over its alias.
    value, renames = canonicalize({"txid": TXID, "hash": "f" * 64})
    assert value["txid"] == TXID and "hash" not in renames
    assert set(describe(list(raw))["missing"]) == {
        "src_port", "dst_ip", "fee", "script_type", "output_addresses[]", "input_amounts[]"
    }


@pytest.mark.parametrize("cell", ['["a", "b"]', "['a', 'b']", "a;b", "a|b", "a, b", "a b"])
def test_list_cells_in_any_common_encoding(cell: str) -> None:
    normalized = _normalize({"txid": TXID, "timestamp": "2026-01-01T00:00:00Z", "output_addresses": cell,
                             "output_amounts": "0.1;0.2"})
    outputs = normalized.facts["outputs"]
    assert [(o["address"], o["amount_sats"]) for o in outputs] == [("a", 10_000_000), ("b", 20_000_000)]


def test_timestamps_epoch_and_utc_suffix() -> None:
    for value, expected in (("1767225600", "2026-01-01T00:00:00Z"), ("1767225600000", "2026-01-01T00:00:00Z"),
                            ("2026-01-01 00:00:00 UTC", "2026-01-01T00:00:00Z")):
        normalized = _normalize({"txid": TXID, "timestamp": value, "output_addresses": ["a"],
                                 "output_amounts": [1]})
        assert normalized.facts["transactions"][0]["source_timestamp"] == expected
    with pytest.raises(NormalizationError, match="timezone"):
        _normalize({"txid": TXID, "timestamp": "2026-01-01T00:00:00", "output_addresses": ["a"],
                    "output_amounts": [1]})


def test_naive_timestamps_only_with_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(normalize_module, "NAIVE_TIMESTAMPS_AS_UTC", True)
    normalized = _normalize({"txid": TXID, "timestamp": "2026-01-01T05:30:00", "output_addresses": ["a"],
                             "output_amounts": [1]})
    assert normalized.facts["transactions"][0]["source_timestamp"] == "2026-01-01T05:30:00Z"


def test_satoshi_amount_columns_and_plain_address_inputs() -> None:
    normalized = _normalize({"txid": TXID, "timestamp": "2026-01-01T00:00:00Z", "inputs": ["in1", "in2"],
                             "input_amounts_sats": "500;700", "outputs": ["out"], "output_values_sats": "1100"})
    assert [(i["address"], i["amount_sats"]) for i in normalized.facts["inputs"]] == [("in1", 500), ("in2", 700)]
    assert [(o["address"], o["amount_sats"]) for o in normalized.facts["outputs"]] == [("out", 1100)]
    assert normalized.field_mapping == {"output_values_sats": "output_amounts_sats"}


def test_dataset_inspection_and_manifest_registration(tmp_path: Path) -> None:
    source = tmp_path / "ps.csv"
    source.write_text(
        "Timestamp,Source IP,Destination IP,Source Port,Destination Port,TxID,Input Addresses,Output Addresses,"
        "Input Amounts,Output Amounts,Fee,Script Type,Country,ASN\n"
        f"1767225600,8.8.8.8,203.0.113.9,1,8333,{TXID},a;b,c|d,0.5;0.25,0.6|0.149,0.001,p2wpkh,US,AS15169\n"
        f"1767225601,10.0.0.1,203.0.113.9,2,8333,{TXID},a,c,0.5,0.4,0.001,p2wpkh,US,AS15169\n"
        f"2026-01-01T00:00:00,10.0.0.2,203.0.113.9,3,8333,{'cd' * 32},a,c,0.5,0.4,0.001,p2wpkh,US,AS15169\n",
        encoding="utf-8",
    )
    report = inspect(source, sample=None)
    assert report["records"] == 3 and report["format"] == "csv"
    assert report["schema"]["missing"] == []
    assert report["normalisation"] == {"accepted": 1, "duplicate_txids": 1, "quarantined": 1,
                                       "quarantine_reasons": {"timestamp must include a timezone": 1}}
    assert report["network"]["src_ip_scope_observations"] == {"public": 1}
    manifest = tmp_path / "data_manifest.json"
    manifest.write_text(json.dumps({"schema_version": "1.0.0", "sources": [{"source_id": "keep"}]}))
    entry = register(report, manifest=manifest, source_id="src-ps", note="synthetic test")
    saved = json.loads(manifest.read_text())
    assert [source["source_id"] for source in saved["sources"]] == ["keep", "src-ps"]
    assert entry["sha256"] == report["sha256"] and entry["status"] == "bytes_verified_locally"


def test_scale_fixture_copies_keep_spends_inside_each_copy(tmp_path: Path) -> None:
    import subprocess
    import sys

    funding, spend = "11" * 32, "22" * 32
    rows = [
        {"txid": funding, "timestamp": "2026-01-01T00:00:00Z", "inputs": [], "input_addresses": [],
         "outputs": [{"address": "bcrt1qa", "amount_sats": 5}], "output_addresses": ["bcrt1qa"]},
        {"txid": spend, "timestamp": "2026-01-01T01:00:00Z",
         "inputs": [{"prev_txid": funding, "prev_vout": 0, "address": "bcrt1qa"}], "input_addresses": ["bcrt1qa"],
         "outputs": [{"address": "bcrt1qb", "amount_sats": 4}], "output_addresses": ["bcrt1qb"]},
    ]
    source = tmp_path / "rows.ndjson"
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    output = tmp_path / "scaled.ndjson"
    subprocess.run([sys.executable, "scripts/scale_fixture.py", "--source", str(source), "--copies", "3",
                    "--output", str(output)], check=True, cwd=Path(__file__).parents[2], capture_output=True)
    copies = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(copies) == 6 and len({row["txid"] for row in copies}) == 6
    for index in range(3):
        fund, spent = copies[2 * index], copies[2 * index + 1]
        assert spent["inputs"][0]["prev_txid"] == fund["txid"]  # spends resolve within the copy
        assert spent["inputs"][0]["address"] == fund["outputs"][0]["address"] == spent["input_addresses"][0]
    assert copies[0]["outputs"][0]["address"] == "bcrt1qa"  # copy 0 is the original
    assert len({row["outputs"][0]["address"] for row in copies[::2]}) == 3
    assert copies[0]["timestamp"] < copies[2]["timestamp"] < copies[4]["timestamp"]
