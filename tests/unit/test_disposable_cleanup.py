"""Disposable benchmark cleanup guards must preserve real/active/changed data."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from sqlalchemy.orm import Session

from app.db import Base, make_engine
from app.models import Case, EvidenceSource, ImportJob, RiskSeed, User
from scripts.disposable_appliance_cleanup import archive


@pytest.fixture
def disposable(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(external_subject="offline-review-disposable-test")
        session.add(user)
        session.flush()
        case = Case(name="scale offline unit-test", synthetic=True, created_by=user.id)
        session.add(case)
        session.flush()
        raw = b"synthetic raw bytes"
        digest = hashlib.sha256(raw).hexdigest()
        relative = f"{case.id}/{digest}/original"
        target = tmp_path / "vault" / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(raw)
        source = EvidenceSource(case_id=case.id, sha256=digest, byte_size=len(raw), synthetic=True,
            original_filename="source.ndjson", source_format="ndjson", storage_relative_path=relative)
        session.add(source)
        session.flush()
        job = ImportJob(case_id=case.id, source_id=source.id, idempotency_key="disposable-test", state="completed")
        session.add(job)
        session.commit()
        report = {"status": "pass", "case_id": case.id, "job_id": job.id, "source_sha256": digest}
        yield session, report, case, job, tmp_path / "vault", target
    engine.dispose()


def test_disposable_archive_verifies_raw_bytes_without_deleting(disposable):
    session, result, _, _, root, target = disposable
    report = archive(session, result, name="unit-test", root=root)
    assert report["receipt_hashes_verified"] == 1
    assert report["vault_logical_bytes"] == len(b"synthetic raw bytes")
    assert target.read_bytes() == b"synthetic raw bytes"
    assert report["database_counts"]["evidence_sources"] == 1


@pytest.mark.parametrize("unsafe", ["real", "active", "changed", "analyst"])
def test_disposable_archive_refuses_unsafe_case(disposable, unsafe):
    session, result, case, job, root, target = disposable
    if unsafe == "real":
        case.synthetic = False
    elif unsafe == "active":
        job.state = "checkpointed"
    elif unsafe == "changed":
        target.write_bytes(b"changed")
    else:
        session.add(RiskSeed(case_id=case.id, wallet_ref="addr:test", label="analyst-assertion",
                            reason="Must be retained", created_by=case.created_by))
    session.commit()
    before = target.read_bytes()
    with pytest.raises(ValueError):
        archive(session, result, name="unit-test", root=root)
    assert target.read_bytes() == before and session.get(Case, case.id) is not None


@pytest.fixture
def duplicate_source(tmp_path, monkeypatch):
    from scripts import deduplicate_benchmark_source as module

    monkeypatch.setattr(module, "REPO", tmp_path)
    run = tmp_path / "var/scale-run/test"
    source = tmp_path / "datasets/review_test/ingestion_rows.ndjson"
    raw = run / "evidence/case/hash/original"
    raw.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    payload = b"synthetic-only-source\n"
    digest = hashlib.sha256(payload).hexdigest()
    source.write_bytes(payload)
    raw.write_bytes(payload)
    (source.parent / "dataset_manifest.json").write_text(json.dumps({
        "seed": "tracex-review-unit-test", "files": {source.name: {"sha256": digest}}}))
    (run / "result.json").write_text(json.dumps({"status": "failed", "case_id": "case", "job_id": "job"}))
    with sqlite3.connect(run / "control.db") as database:
        database.executescript("CREATE TABLE cases(id,name,synthetic); CREATE TABLE import_jobs(id,state); "
                               "CREATE TABLE evidence_sources(storage_relative_path,sha256,byte_size);")
        database.execute("INSERT INTO cases VALUES ('case','scale test',1)")
        database.execute("INSERT INTO import_jobs VALUES ('job','failed')")
        database.execute("INSERT INTO evidence_sources VALUES (?,?,?)", ("case/hash/original", digest, len(payload)))
    return module, run, source, raw, payload


def test_verified_source_dedup_retains_both_paths_and_bytes(duplicate_source):
    module, run, source, raw, payload = duplicate_source
    report = module.deduplicate(run, source)
    assert report["status"] == "pass" and source.samefile(raw)
    assert source.read_bytes() == raw.read_bytes() == payload
    raw.unlink()  # source survives later approved benchmark-vault retirement
    assert source.read_bytes() == payload


@pytest.mark.parametrize("unsafe", ["real", "active", "changed"])
def test_source_dedup_refuses_unsafe_or_nonidentical_data(duplicate_source, unsafe):
    module, run, source, raw, _ = duplicate_source
    with sqlite3.connect(run / "control.db") as database:
        if unsafe == "real":
            database.execute("UPDATE cases SET synthetic=0")
        elif unsafe == "active":
            database.execute("UPDATE import_jobs SET state='ingesting'")
        else:
            source.write_bytes(b"different-source-data\n")
    before = source.read_bytes()
    with pytest.raises(ValueError):
        module.deduplicate(run, source)
    assert source.read_bytes() == before and not source.samefile(raw)


@pytest.fixture
def obsolete_bundle(tmp_path, monkeypatch):
    import shutil
    import tarfile

    from scripts import disposable_bundle_cleanup as module

    monkeypatch.setattr(module, "REPO", tmp_path)
    source = tmp_path / "dist/tracex-offline-0.1.0-test"
    source.mkdir(parents=True)
    manifest = {"images": [{"tags": ["tracex-review-appliance:20261003-release-02"], "id": "old"}]}
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "images.tar").write_bytes(b"disposable bundle unit test")
    sums = {name: module.digest(source / name) for name in ("manifest.json", "images.tar")}
    (source / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sums.items()))
    compressed = source.with_name(source.name + ".tar.gz")
    with tarfile.open(compressed, "w:gz") as archive:
        archive.add(source, arcname=source.name)
    installation = tmp_path / "var/offline-install-20261003-release-02"
    shutil.copytree(source, installation)
    (installation / "installation_result.json").write_text(json.dumps({"status": "pass", "source": str(source)}))
    current = tmp_path / "var/offline-install-20261003-release-06"
    current.mkdir()
    (current / "installation_result.json").write_text(json.dumps({"status": "pass"}))
    (current / "manifest.json").write_text(json.dumps({"images": [{"id": "new"}]}))
    return module, source, installation, current, compressed


def test_obsolete_bundle_verifies_exact_payloads_without_deletion(obsolete_bundle):
    module, source, installation, current, compressed = obsolete_bundle
    report = module.payloads(installation, current)
    assert {item["path"] for item in report} == {str(p.relative_to(module.REPO)) for p in
                                                (source / "images.tar", installation / "images.tar", compressed)}
    assert compressed.is_file() and (source / "images.tar").is_file()


@pytest.mark.parametrize("unsafe", ["changed", "current"])
def test_obsolete_bundle_refuses_changed_or_current_payload(obsolete_bundle, unsafe):
    module, source, installation, current, compressed = obsolete_bundle
    if unsafe == "changed":
        (installation / "images.tar").write_bytes(b"changed")
    else:
        installation = current
    with pytest.raises(ValueError):
        module.payloads(installation, current)
    assert compressed.is_file() and (source / "images.tar").is_file()


@pytest.mark.parametrize("status", ["pass", "failed"])
def test_last_generated_ingestion_requires_matching_passing_benchmark(tmp_path, status):
    from scripts.disposable_dataset_cleanup import verified_ingestion_only

    names = ["transactions.ndjson", "inputs.ndjson", "outputs.ndjson", "network_observations.ndjson"]
    files = {name: {"sha256": "canonical-" + name} for name in names}
    files["ingestion_rows.ndjson"] = {"sha256": "last-source"}
    (tmp_path / "disposable-input-archive.json").write_text(json.dumps({"delete_requested": True,
        "artifacts": [{"name": name, "sha256": files[name]["sha256"]} for name in names]}))
    (tmp_path / "ingestion_rows.ndjson").write_bytes(b"retained source bytes")
    benchmark = {"status": status, "acceptance_errors": [], "source_sha256": "last-source"}
    if status == "pass":
        assert verified_ingestion_only(tmp_path, {"files": files}, benchmark) == ["ingestion_rows.ndjson"]
    else:
        with pytest.raises(ValueError):
            verified_ingestion_only(tmp_path, {"files": files}, benchmark)
    assert (tmp_path / "ingestion_rows.ndjson").read_bytes() == b"retained source bytes"
