"""SQLAlchemy engine/session configuration and schema initialization for SQLite.

* `create_db_engine` is lazy: no connection (and no database file) until first use.
* `init_database` creates the schema and records a schema version in ``PRAGMA user_version``.
  Version 2 only *adds* tables, so ``create_all`` upgrades a version-1 database in place; a
  later migration tool can build on the marker.
"""

from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

SCHEMA_VERSION = 2  # 2: Phase 02 adds mandates, handoffs, receipts, checks
_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


class Base(DeclarativeBase):
    """Declarative base for the ORM models in `app.repositories.orm`."""


class SchemaVersionError(RuntimeError):
    """The database was created by a newer version of the application."""


def _configure_sqlite_connection(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=15000")
    # WAL lets readers proceed while one writer commits (not applicable to :memory:).
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    kwargs: dict[str, Any] = {"echo": echo}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if database_url in _MEMORY_URLS:
            # One shared connection, otherwise every connection would see its own empty DB.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_database(engine: Engine) -> None:
    """Create tables, indexes and triggers if missing, and stamp the schema version."""
    from app.repositories import orm  # noqa: F401  (registers the models on Base.metadata)

    with engine.begin() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}."
            )
        Base.metadata.create_all(connection)
        if version < SCHEMA_VERSION:
            connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))  # noqa: S608
