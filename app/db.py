"""SQLAlchemy engine/session construction and schema bootstrap for local development."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import JSON, bindparam, create_engine, insert, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    url = database_url or settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


class SerializedJSON(TypeDecorator):
    """Bind type for a JSON column whose value is already a serialized document.

    Rendered exactly like the column's own JSON type (including PostgreSQL's
    `::JSON` bind cast), but the string is sent as-is instead of being run
    through `json.dumps` a second time.
    """

    impl = JSON
    cache_ok = True

    def bind_processor(self, dialect):
        return None


def bulk_insert_serialized(session: Session, model, rows: list[dict], json_columns: frozenset[str]) -> None:
    """Core executemany of plain dict rows whose `json_columns` hold JSON text.

    Used for write-once analytical rows (hundreds of thousands per snapshot):
    it skips the ORM bulk path's per-row bookkeeping and lets the caller
    serialize a value shared by every row -- e.g. snapshot coverage -- once.
    """
    if not rows:
        return
    table = model.__table__
    connection = session.connection()
    if connection.dialect.name == "postgresql" and connection.dialect.driver == "psycopg":
        # PostgreSQL: COPY streams the rows in one protocol operation instead of
        # thousands of multi-row INSERT statements, inside the same transaction.
        # JSON columns already hold JSON text, which COPY's text format accepts
        # for a json column as-is.
        columns = list(rows[0])
        cursor = connection.connection.driver_connection.cursor()
        with cursor.copy(f'COPY {table.name} ({", ".join(columns)}) FROM STDIN') as copy:
            for row in rows:
                copy.write_row([row[name] for name in columns])
        return
    if connection.dialect.name == "sqlite":
        # SQLite: one prepared statement and the driver's own executemany. Each
        # non-JSON column still goes through its SQLAlchemy type's bind
        # processor (so e.g. datetimes are stored in exactly the same format);
        # only the per-row statement compilation is skipped.
        columns = list(rows[0])
        dialect = connection.dialect
        processors = [
            None if name in json_columns else table.c[name].type.dialect_impl(dialect).bind_processor(dialect)
            for name in columns
        ]
        prepared = [
            tuple(value if process is None else process(value) for value, process in zip(
                (row[name] for name in columns), processors, strict=True
            ))
            for row in rows
        ]
        connection.exec_driver_sql(
            f'INSERT INTO {table.name} ({", ".join(columns)}) VALUES ({", ".join("?" for _ in columns)})', prepared
        )
        return
    statement = insert(table).values(
        {
            name: bindparam(name, type_=SerializedJSON() if name in json_columns else table.c[name].type)
            for name in rows[0]
        }
    )
    session.execute(statement, rows)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


#: Nullable columns added to existing tables after their first release.
#: `create_all` only creates missing *tables*, so a database initialised before
#: one of these existed would otherwise fail every query that touches the model
#: (e.g. `import_jobs.total_records` broke job polling and worker claims).
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (("import_jobs", "total_records", "INTEGER"),)


def ensure_schema(bind=None) -> list[str]:
    """Create missing tables and add missing nullable columns. Idempotent and
    safe to run on every process start; returns the columns it added."""
    import app.models  # noqa: F401

    target = bind or engine
    Base.metadata.create_all(target)
    added: list[str] = []
    inspector = inspect(target)
    for table, column, sql_type in ADDITIVE_COLUMNS:
        if not inspector.has_table(table):
            continue
        if column in {existing["name"] for existing in inspector.get_columns(table)}:
            continue
        with target.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        added.append(f"{table}.{column}")
    return added


def init_database() -> None:
    # Import models before metadata creation; production uses the matching migration.
    added = ensure_schema()
    if added:
        print(f"Added missing columns: {', '.join(added)}")
    print("TraceX database schema is ready")
