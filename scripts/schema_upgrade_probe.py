"""Exercise additive upgrades in a newly created disposable database only.

Run inside the copied appliance using --postgres-database; credentials are
read from its configuration, never printed or passed as command arguments.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, ensure_schema, make_engine
from app.models import AnalysisRequest, Case, EvidenceSource, ImportJob, User


def probe(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(external_subject="disposable-schema-upgrade-review")
        session.add(user)
        session.flush()
        case = Case(name="Synthetic schema upgrade fixture", synthetic=True, created_by=user.id)
        session.add(case)
        session.flush()
        source = EvidenceSource(case_id=case.id, sha256="0" * 64, byte_size=0,
                                original_filename="fixture.ndjson", source_format="ndjson",
                                storage_relative_path="disposable-schema-fixture", synthetic=True)
        session.add(source)
        session.flush()
        job = ImportJob(case_id=case.id, source_id=source.id, idempotency_key="disposable-upgrade-fixture",
                        state="completed", stage="completed")
        session.add(job)
        session.flush()
        session.add(AnalysisRequest(job_id=job.id, attempt=2, refresh_analytics=True, fulfilled=False))
        session.commit()
        identity = job.id
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE import_jobs DROP COLUMN total_records"))
        connection.execute(text("ALTER TABLE analysis_requests DROP COLUMN fulfilled"))
        connection.execute(text("ALTER TABLE cases DROP COLUMN scoring_mode"))
        connection.execute(text("ALTER TABLE cases DROP COLUMN candidate_domain"))
    added = ensure_schema(engine)
    repeated = ensure_schema(engine)
    with Session(engine) as session:
        request = session.get(AnalysisRequest, (identity, 2))
        preserved = (session.get(ImportJob, identity).total_records is None and
                     request.refresh_analytics is True and request.fulfilled is False and
                     session.query(Case).count() == 1 and session.query(EvidenceSource).count() == 1 and
                     session.query(Case).one().scoring_mode == "unsupervised" and session.query(Case).one().candidate_domain is None)
    columns = {table: [c["name"] for c in inspect(engine).get_columns(table)]
               for table in ("cases", "import_jobs", "analysis_requests")}
    passed = added == ["cases.scoring_mode", "cases.candidate_domain", "import_jobs.total_records", "analysis_requests.fulfilled"] and not repeated and preserved
    return {"status": "pass" if passed else "failed", "dialect": engine.dialect.name,
            "added": added, "second_upgrade_added": repeated, "fixture_rows_preserved": preserved,
            "columns": columns, "scope": "new empty disposable database, never an existing case database"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sqlite", type=Path)
    mode.add_argument("--postgres-database")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if args.sqlite:
        if args.sqlite.exists():
            raise FileExistsError(args.sqlite)
        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{args.sqlite.resolve()}")
    else:
        if not re.fullmatch(r"tracex_review_upgrade_[a-z0-9_]+", args.postgres_database):
            raise ValueError("Only a new tracex_review_upgrade_* database may be created")
        admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
        if admin.dialect.name != "postgresql":
            raise ValueError("Appliance configuration must use PostgreSQL")
        # CREATE, not IF NOT EXISTS: never alter a pre-existing database.
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{args.postgres_database}"'))
        engine = create_engine(admin.url.set(database=args.postgres_database))
        admin.dispose()
    report = {"status": "failed", "scope": "new disposable database only"}
    try:
        report = probe(engine)
    except Exception as error:  # noqa: BLE001 - preserve probe failure without credentials
        report["error_type"] = type(error).__name__
    finally:
        engine.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(report, handle, indent=2)
    print(json.dumps(report))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
