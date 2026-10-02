"""Entity clustering, Geo-IP enrichment, network correlation, embeddings and risk propagation."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import routes
from app.config import Settings
from app.db import Base, get_session, make_engine
from app.engine import analytics, geoip
from app.main import app
from workers import runner


def _txid(tag: str) -> str:
    return (tag.encode().hex() * 64)[:64]


FUND = _txid("fund")
MIX_FUND = _txid("mixfund")
MIX = _txid("coinjoin")
SPENDS = [_txid(f"spend{i}") for i in range(6)]
RELAY_IP = "81.2.69.142"  # public; the test Geo-IP database places it in GB / AS20712
CHAIN_RELAY = "8.8.8.77"


def _rows() -> list[dict]:
    rows: list[dict] = []
    fund_outputs = (
        [{"address": "bcrt1qwalleta1", "amount_sats": 1_000_000} for _ in range(6)]
        + [{"address": "bcrt1qwalleta2", "amount_sats": 1_000_000} for _ in range(4)]
        + [{"address": "bcrt1qwalleta3", "amount_sats": 1_000_000} for _ in range(2)]
    )
    rows.append({"txid": FUND, "network": "bitcoin-regtest", "inputs": [], "outputs": fund_outputs,
                 "fee_sats": 0, "timestamp": "2026-01-01T00:00:00Z", "src_ip": "198.51.100.1",
                 "geo_country": "DE", "asn": "AS64500"})
    for index, txid in enumerate(SPENDS):
        second = (FUND, 6 + index, "bcrt1qwalleta2") if index < 4 else (FUND, 10 + index - 4, "bcrt1qwalleta3")
        rows.append({
            "txid": txid, "network": "bitcoin-regtest",
            "inputs": [
                {"prev_txid": FUND, "prev_vout": index, "address": "bcrt1qwalleta1", "amount_sats": 1_000_000},
                {"prev_txid": second[0], "prev_vout": second[1], "address": second[2], "amount_sats": 1_000_000},
            ],
            "outputs": [{"address": f"bcrt1qpayee{index}", "amount_sats": 1_500_000},
                        {"address": f"bcrt1qchange{index}", "amount_sats": 499_000}],
            "fee_sats": 1_000, "timestamp": f"2026-01-01T01:0{index}:00Z",
            # Every spend of this wallet is relayed by one public endpoint, whose
            # reported location contradicts the Geo-IP database.
            "src_ip": RELAY_IP, "src_port": 8333, "dst_ip": "203.0.113.5", "dst_port": 8333,
            "geo_country": "US", "asn": "AS15169",
        })
    rows.append({"txid": MIX_FUND, "network": "bitcoin-regtest", "inputs": [],
                 "outputs": [{"address": f"bcrt1qmixer{i}", "amount_sats": 200_000} for i in range(3)],
                 "fee_sats": 0, "timestamp": "2026-01-01T02:00:00Z", "src_ip": "198.51.100.2"})
    rows.append({
        "txid": MIX, "network": "bitcoin-regtest",
        "inputs": [{"prev_txid": MIX_FUND, "prev_vout": i, "address": f"bcrt1qmixer{i}", "amount_sats": 200_000}
                   for i in range(3)],
        "outputs": [{"address": f"bcrt1qmixout{i}", "amount_sats": 190_000} for i in range(3)],
        "fee_sats": 30_000, "timestamp": "2026-01-01T02:10:00Z", "src_ip": "198.51.100.3",
    })
    # A peel-style chain: every hop spends the previous hop's change, each to a
    # fresh address, all broadcast by one public node (relay-flow coherence).
    chain_funding = _txid("chainfund")
    rows.append({"txid": chain_funding, "network": "bitcoin-regtest", "inputs": [],
                 "outputs": [{"address": "bcrt1qchain0", "amount_sats": 50_000_000}], "fee_sats": 0,
                 "timestamp": "2026-01-01T04:00:00Z", "src_ip": "198.51.100.200"})
    previous, value = chain_funding, 50_000_000
    for hop in range(1, 6):
        txid = _txid(f"chainhop{hop}")
        peel = 1_000_000
        rows.append({
            "txid": txid, "network": "bitcoin-regtest",
            "inputs": [{"prev_txid": previous, "prev_vout": 0 if hop == 1 else 1, "address": f"bcrt1qchain{hop - 1}",
                        "amount_sats": value}],
            "outputs": [{"address": f"bcrt1qpeelout{hop}", "amount_sats": peel},
                        {"address": f"bcrt1qchain{hop}", "amount_sats": value - peel - 1_000}],
            "fee_sats": 1_000, "timestamp": f"2026-01-01T04:{hop * 5:02d}:00Z", "src_ip": CHAIN_RELAY,
        })
        previous, value = txid, value - peel - 1_000
    for index in range(40):
        rows.append({"txid": _txid(f"filler{index}"), "network": "bitcoin-regtest", "inputs": [],
                     "outputs": [{"address": f"bcrt1qfiller{index}", "amount_sats": 10_000}], "fee_sats": 0,
                     "timestamp": f"2026-01-01T03:{index:02d}:00Z", "src_ip": f"198.51.100.{10 + index}"})
    return rows


def _write_geoip(directory: Path) -> None:
    country = directory / "dbip-country-test.csv"
    country.write_text("81.2.69.0,81.2.69.255,GB\n8.8.8.0,8.8.8.255,US\n2001:4860::,2001:4860:ffff:ffff:ffff:ffff:ffff:ffff,US\n")
    asn = directory / "iptoasn-asn-test.csv"
    asn.write_text('81.2.69.0,81.2.69.255,20712,"Andrews & Arnold Ltd"\n8.8.8.0,8.8.8.255,15169,GOOGLE\n')
    geoip.compile_database([country, asn], directory)


@pytest.fixture()
def analytics_case(tmp_path: Path, monkeypatch):
    geo_dir = tmp_path / "geoip"
    geo_dir.mkdir()
    _write_geoip(geo_dir)
    monkeypatch.setenv("TRACEX_GEOIP_DIR", str(geo_dir))
    engine = make_engine(f"sqlite:///{tmp_path / 'control.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}", evidence_root=tmp_path / "evidence",
        max_upload_bytes=4 * 1024 * 1024, lease_seconds=30, event_heartbeat_seconds=1,
    )

    def override_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(routes, "settings", settings)
    from app.api import analytics_routes

    monkeypatch.setattr(analytics_routes, "settings", settings)
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "SessionLocal", sessions)
    client = TestClient(app)
    headers = {"X-TraceX-Actor": "entity-analyst"}
    case_id = client.post("/v1/cases", headers=headers, json={"name": "entities"}).json()["case_id"]
    payload = "\n".join(json.dumps(row) for row in _rows()).encode()
    response = client.post(
        f"/v1/cases/{case_id}/imports", headers={**headers, "Idempotency-Key": "entities"},
        files={"file": ("rows.ndjson", payload, "application/x-ndjson")},
    )
    assert response.status_code == 202, response.text
    assert runner.process_one("entity-worker")
    job = client.get(f"/v1/jobs/{response.json()['job_id']}", headers=headers).json()
    assert job["state"] == "completed", job
    try:
        yield client, headers, case_id
    finally:
        app.dependency_overrides.clear()


def test_geoip_compiles_and_classifies(tmp_path: Path) -> None:
    _write_geoip(tmp_path)
    database = geoip.open_database(tmp_path)
    assert database is not None and database.has_country and database.has_asn
    hit = database.lookup("81.2.69.142")
    assert (hit.scope, hit.country, hit.asn, hit.as_org) == ("public", "GB", 20712, "Andrews & Arnold Ltd")
    assert database.lookup("2001:4860::8888").country == "US"
    assert database.lookup("9.9.9.9").country is None  # public but outside the test ranges
    assert database.lookup("203.0.113.7").scope == "documentation"
    assert database.lookup("10.0.0.1").scope == "private"
    assert database.lookup("not-an-ip").scope == "invalid"


def test_geoip_imports_npm_style_packages(tmp_path: Path) -> None:
    package = tmp_path / "dbip-country-9.9.9.tgz"
    with tarfile.open(package, "w:gz") as archive:
        for name, body in (("package/dbip-country-ipv4.csv", b"1.0.0.0,1.0.0.255,AU\n"),
                           ("package/dbip-country-ipv4-num.csv", b"16777216,16777471,AU\n"),
                           ("package/DBIP-LICENSE", b"CC BY 4.0")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    combined = tmp_path / "ip2asn-combined.tsv.gz"
    combined.write_bytes(gzip.compress(b"1.0.0.0\t1.0.0.255\t13335\tUS\tCLOUDFLARENET\n"))
    manifest = geoip.compile_database([package, combined], tmp_path / "out")
    assert manifest["tables"] == {"asn_v4": 1, "country_v4": 1}  # the -num duplicate is skipped
    assert {source["licence"] for source in manifest["sources"]} == {"CC-BY-4.0", "PDDL-1.0"}
    hit = geoip.open_database(tmp_path / "out").lookup("1.0.0.1")
    assert (hit.country, hit.asn, hit.as_org) == ("AU", 13335, "CLOUDFLARENET")


def test_binomial_tail() -> None:
    assert analytics.binomial_tail(0, 10, 0.3) == 1.0
    assert analytics.binomial_tail(10, 10, 0.5) == pytest.approx(0.5 ** 10)
    assert analytics.binomial_tail(3, 5, 0.5) == pytest.approx(0.5)


def test_common_input_ownership_clusters_and_skips_coinjoin(analytics_case) -> None:
    client, headers, case_id = analytics_case
    page = client.get(f"/v1/cases/{case_id}/entities", headers=headers).json()
    assert page["total"] == 1
    entity = page["entities"][0]
    assert entity["address_count"] == 3
    assert entity["linking_tx_count"] == 6
    summary = page["summary"]
    assert summary["mixing_excluded_transactions"] == 1  # the 3-in / 3-equal-out transaction
    assert summary["clustered_addresses"] == 3
    detail = client.get(f"/v1/cases/{case_id}/entities/address:bcrt1qwalleta2", headers=headers).json()
    assert detail["kind"] == "entity" and detail["wallet"] == entity["entity_id"]
    assert detail["addresses"] == ["bcrt1qwalleta1", "bcrt1qwalleta2", "bcrt1qwalleta3"]
    assert {link["txid"] for link in detail["linking_transactions"]} == set(SPENDS)
    assert detail["relay_endpoints"][0]["ip"] == RELAY_IP and detail["relay_endpoints"][0]["spends"] == 6
    mixer = client.get(f"/v1/cases/{case_id}/entities/bcrt1qmixer0", headers=headers).json()
    assert mixer["kind"] == "address" and mixer["addresses"] == ["bcrt1qmixer0"]


def test_network_correlation_and_geoip_mismatch_findings(analytics_case) -> None:
    client, headers, case_id = analytics_case
    findings = client.get(
        f"/v1/cases/{case_id}/findings", headers=headers,
        params={"rule_id": ["relay_endpoint_concentration", "reported_geo_mismatch"]},
    ).json()["findings"]
    by_rule = {finding["rule_id"]: finding for finding in findings}
    concentration = by_rule["relay_endpoint_concentration"]
    assert concentration["entity_ref"].startswith("entity:E-")
    assert concentration["rule_version"] == analytics.NETWORK_RULE_VERSION
    vector = json.loads(json.dumps(concentration["coverage"]))
    assert vector["geoip_installed"] is True
    assert "6 of its 6 observed spends" in concentration["claim"]
    assert concentration["source_refs"], "network findings must cite source records"
    mismatch = by_rule["reported_geo_mismatch"]
    assert mismatch["entity_ref"] == f"endpoint:{RELAY_IP}"
    assert "GB" in mismatch["claim"] and "US" in mismatch["claim"]
    network = client.get(f"/v1/cases/{case_id}/network", headers=headers).json()
    assert network["summary"]["geoip_installed"] is True
    assert network["summary"]["observation_country_checks"]["mismatch"] == 6
    assert network["summary"]["endpoint_scopes"]["documentation"] >= 40
    endpoint = client.get(f"/v1/cases/{case_id}/network/endpoints/{RELAY_IP}", headers=headers).json()
    assert (endpoint["db_country"], endpoint["db_asn"], endpoint["country_check"]) == ("GB", 20712, "mismatch")
    assert endpoint["findings"] and endpoint["findings"][0]["rule_id"] == "reported_geo_mismatch"


def test_risk_propagates_from_a_seed_wallet(analytics_case) -> None:
    client, headers, case_id = analytics_case
    response = client.post(f"/v1/cases/{case_id}/risk/seeds", headers=headers,
                           json={"wallet_ref": "bcrt1qwalleta1", "reason": "named in a ransomware report"})
    assert response.status_code == 201, response.text
    run = client.get(f"/v1/cases/{case_id}/risk", headers=headers, params={"limit": 2000}).json()["run"]
    scores = {item["wallet"]: item for item in run["scores"]}
    seed_entity = run["seeds"][0]["wallet"]
    assert run["seeds"][0]["found"] and seed_entity.startswith("E-")
    assert scores[seed_entity]["risk"] == 1.0 and scores[seed_entity]["is_seed"]
    # Every payee was paid entirely from the seed's coins, one hop away.
    assert scores["bcrt1qpayee0"]["downstream"] == pytest.approx(0.85)
    # The funder of the seed sent it value: upstream exposure, not downstream.
    unrelated = scores.get("bcrt1qfiller0")
    assert unrelated is None
    flow = client.get(f"/v1/cases/{case_id}/graph/flow", headers=headers, params={"node": SPENDS[0]}).json()
    drawn = {item["id"]: item for item in [*flow["inputs"], *flow["outputs"]]}
    assert drawn["address:bcrt1qwalleta1"]["entity_id"] == seed_entity
    assert drawn["address:bcrt1qwalleta1"]["entity_address_count"] == 3
    assert drawn["address:bcrt1qpayee0"]["risk"] == pytest.approx(0.85)
    deleted = client.delete(f"/v1/cases/{case_id}/risk/seeds/{run['seeds'][0]['seed_id']}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["run"]["summary"]["wallets_with_risk"] == 0


def test_similar_wallets_use_graph_embeddings(analytics_case) -> None:
    client, headers, case_id = analytics_case
    similar = client.get(f"/v1/cases/{case_id}/entities/bcrt1qwalleta1/similar", headers=headers).json()
    assert similar["status"] == "complete"
    assert similar["similar"] and all(-1.01 <= item["similarity"] <= 1.01 for item in similar["similar"])


def test_confidence_is_calibrated_and_bounded() -> None:
    from app.engine.confidence import anomaly_p_value, confidence_for

    low = confidence_for("peeling_chain_candidate", "deterministic-v1", 0.55, {})
    high = confidence_for("peeling_chain_candidate", "deterministic-v1", 0.9999, {})
    assert low["method"] == "calibrated" and low["calibration_id"] == "confidence-v1"
    assert 0.0 < low["value"] <= high["value"] < 1.0  # monotone, never certain
    assert high["reliability"]["auc"] is not None
    ml = confidence_for("anomaly_stack_rank", "anomaly-stack-v1", 3.0, {"fused_score": 3.0})
    assert ml["anomaly_p_value"] == pytest.approx(anomaly_p_value(3.0)) and ml["anomaly_p_value"] < 0.002
    network = confidence_for("relay_endpoint_concentration", "network-correlation-v1", 9.0,
                             {"adjusted_p_value": 1e-6})
    assert network["method"] == "statistical" and network["value"] == pytest.approx(1 - 1e-6)
    unknown = confidence_for("not_a_rule", "deterministic-v1", 1.0, {})
    assert unknown["value"] is None and unknown["method"] == "uncalibrated"


def test_findings_api_carries_confidence(analytics_case) -> None:
    client, headers, case_id = analytics_case
    findings = client.get(f"/v1/cases/{case_id}/findings", headers=headers).json()["findings"]
    assert findings and all("confidence" in finding for finding in findings)
    assert all(finding["confidence"]["method"] in {"calibrated", "statistical", "evidence_check", "uncalibrated"}
               for finding in findings)


def test_compiled_geoip_is_readable_by_other_users(tmp_path: Path) -> None:
    _write_geoip(tmp_path)
    compiled = tmp_path / geoip.COMPILED_DIR
    assert compiled.stat().st_mode & 0o005 == 0o005
    assert all(path.stat().st_mode & 0o004 for path in compiled.iterdir())


def test_relay_flow_coherence_links_a_chain_to_its_node(analytics_case) -> None:
    client, headers, case_id = analytics_case
    findings = client.get(f"/v1/cases/{case_id}/findings", headers=headers,
                          params={"rule_id": ["relay_flow_coherence"]}).json()["findings"]
    assert [finding["entity_ref"] for finding in findings] == [f"endpoint:{CHAIN_RELAY}"]
    finding = findings[0]
    assert "4 of them spend outputs of a transaction this same endpoint relayed" in finding["claim"]
    assert finding["confidence"]["method"] == "statistical" and finding["confidence"]["value"] > 0.99
    network = client.get(f"/v1/cases/{case_id}/network", headers=headers).json()
    assert network["summary"]["relay_coherent_endpoints"] == 1
