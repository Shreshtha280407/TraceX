from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import appliance_acceptance as acceptance
from scripts.prepare_review_install import review_environment
from scripts.scratch_usage_probe import scratch_usage


def test_review_install_uses_real_ml_key_and_preserves_existing_secrets():
    template = "TRACEX_SECRET_KEY=prior\nTRACEX_DB_PASSWORD=prior-db\nTRACEX_ML_FINDINGS_ENABLED=0\n"
    values = {"TRACEX_ML_FINDINGS": "1", "TRACEX_MEMORY_BUDGET_MB": "5120"}
    environment = review_environment(template, values)
    assert "TRACEX_SECRET_KEY=prior\n" in environment
    assert "TRACEX_DB_PASSWORD=prior-db\n" in environment
    assert "TRACEX_ML_FINDINGS=1\n" in environment
    assert "TRACEX_ML_FINDINGS_ENABLED" not in environment
    assert environment.count("TRACEX_ML_FINDINGS=") == 1
    assert review_environment(environment, values) == environment


def test_scratch_observer_counts_only_transient_work_and_spill(tmp_path):
    work = tmp_path / "case" / ".work" / "snapshot"
    spill = work / "spill" / "child"
    spill.mkdir(parents=True)
    (work / "facts.duckdb").write_bytes(b"facts")
    (spill / "temp.bin").write_bytes(b"spill-data")
    (tmp_path / "case" / "raw-evidence").write_bytes(b"raw")
    observed = scratch_usage(tmp_path)
    assert observed["scratch_files"] == 2
    assert observed["scratch_logical_bytes"] == 15
    assert observed["spill_logical_bytes"] == 10
    assert observed["filesystem_free_bytes"] > 0


def test_appliance_upload_streams_exact_bytes_and_uses_bearer_auth(tmp_path):
    source = tmp_path / "source.ndjson"
    raw = b'{"txid":"example"}\n' * 100
    source.write_bytes(raw)
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["path"] = self.path
            received["authorization"] = self.headers["Authorization"]
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"job_id":"job"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = acceptance.upload(f"http://127.0.0.1:{server.server_port}", "case", source, "test-token", timeout=5)
        assert result == {"job_id": "job"}
        assert received["path"] == "/v1/cases/case/imports"
        assert received["authorization"] == "Bearer test-token"
        assert received["body"].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0] == raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_appliance_missing_source_retains_failed_result(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "REPO", tmp_path)
    code = acceptance.main(["--base", "http://127.0.0.1:1", "--compose", str(tmp_path / "compose.yml"),
                            "--project", "test", "--source", str(tmp_path / "missing.ndjson"),
                            "--expected-counts", str(tmp_path / "counts.json"), "--name", "missing"])
    assert code == 1
    report = json.loads((tmp_path / "var/appliance-runs/missing/result.json").read_text())
    assert report["status"] == "failed" and "FileNotFoundError" in report["error"]


def test_appliance_ml_parity_normalizes_only_known_ids_without_mutating_scores():
    from datetime import UTC, datetime

    from scripts.appliance_ml_parity import digest, normalize

    original = {"case_id": "case-run", "feature_vector": {"model_run_id": "model-run", "score": .75},
                "raw_score": 3.5, "timestamp": datetime(2026, 1, 1, tzinfo=UTC)}
    normalized = normalize(original, {"case-run": "<CASE>", "model-run": "<MODEL_RUN>"})
    assert normalized == {"case_id": "<CASE>", "feature_vector": {"model_run_id": "<MODEL_RUN>", "score": .75},
                          "raw_score": 3.5, "timestamp": "2026-01-01T00:00:00+00:00"}
    assert original["feature_vector"]["model_run_id"] == "model-run"
    assert digest([normalized]) != digest([{**normalized, "raw_score": 3.6}])


@pytest.mark.parametrize("result", [{"status": "failed"}, {"status": "pass", "acceptance_errors": ["missing ML"]}])
def test_appliance_ml_parity_refuses_incomplete_acceptance_before_reading_data(result):
    from scripts.appliance_ml_parity import fingerprint

    with pytest.raises(ValueError, match="Both complete benchmarks must pass"):
        fingerprint(None, result)
