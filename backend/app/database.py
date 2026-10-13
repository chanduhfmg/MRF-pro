"""
SQLAlchemy engine + session setup. Works against MySQL (server / when
DATABASE_URL is set) or a local SQLite file (dev default). The rest of the
app never needs to know which one is in use.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

_url = settings.effective_database_url

# SQLite needs check_same_thread=False because the worker thread/process and
# the request handlers may share a connection pool. MySQL wants pool_pre_ping
# so long-idle worker connections don't blow up on the server.
if settings.is_sqlite:
    engine = create_engine(
        _url,
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
else:
    engine = create_engine(
        _url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=10,
        max_overflow=20,
        future=True,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone context manager for the worker / scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they don't exist, and add any columns the models
    declare that the live tables lack. Safe to call repeatedly."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Additive, in-place migration.

    create_all() only creates missing *tables*; a new column on an existing
    table used to mean wiping the database. That throws away every job the
    user has run. This adds the column instead. Only additions are handled -
    renames, type changes and drops are rare enough to do by hand.
    """
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("mrf.db")
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                default = None
                if col.default is not None and not callable(col.default.arg):
                    default = col.default.arg
                if default is not None:
                    if isinstance(default, bool):
                        lit = "1" if default else "0"
                    elif isinstance(default, (int, float)):
                        lit = str(default)
                    else:
                        lit = "'" + str(default).replace("'", "''") + "'"
                    ddl += f" DEFAULT {lit}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                log.info("added column %s.%s", table.name, col.name)
