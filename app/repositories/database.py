"""SQLAlchemy engine/session configuration and schema initialization for SQLite.

* `create_db_engine` is lazy: no connection (and no database file) until first use.
* `init_database` creates the schema and records a schema version in ``PRAGMA user_version``.
  Version 2 only *added* tables. Version 3 (Phase 03) adds token and session tables AND
  rebuilds ``mandates`` (nullable application binding, ``kind``, ``issuer``). SQLite cannot
  alter a column in place, so the old table is renamed, the new one created, the rows copied
  as ``onboarding`` grants and the old table dropped. Every step checks the current state
  first, so an interrupted upgrade simply resumes on the next start.
"""

from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.domain.mandate import AUTHORIZATION_ISSUER

# 2: Phase 02 adds mandates, handoffs, receipts, checks
# 3: Phase 03 rebuilds mandates; adds agent_access_tokens and customer_sessions
SCHEMA_VERSION = 3
_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}
_LEGACY_MANDATES = "_mandates_v2"


class Base(DeclarativeBase):
    """Declarative base for the ORM models in `app.repositories.orm`."""


class SchemaVersionError(RuntimeError):
    """The database was created by a newer version of the application."""


class SchemaMigrationError(RuntimeError):
    """An upgrade step found data it cannot carry over safely; nothing was dropped."""


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


def _columns(connection: Connection, table: str) -> set[str]:
    rows = connection.execute(text(f"PRAGMA table_info({table})"))  # noqa: S608 - fixed names
    return {row[1] for row in rows}


def _set_aside_v2_mandates(connection: Connection) -> None:
    """v2 -> v3, step 1: move a pre-Phase-03 ``mandates`` table out of the way."""
    columns = _columns(connection, "mandates")
    if not columns or "kind" in columns or _columns(connection, _LEGACY_MANDATES):
        return
    # Drop the old trigger first: otherwise it would follow the renamed table and block the
    # (IF NOT EXISTS) creation of the same trigger on the new table.
    connection.execute(text("DROP TRIGGER IF EXISTS trg_mandates_revocation_is_final"))
    connection.execute(text(f"ALTER TABLE mandates RENAME TO {_LEGACY_MANDATES}"))


def _carry_over_v2_mandates(connection: Connection) -> None:
    """v2 -> v3, step 3 (after the new table exists): copy rows as onboarding grants."""
    if not _columns(connection, _LEGACY_MANDATES):
        return
    connection.execute(
        text(
            "INSERT OR IGNORE INTO mandates (mandate_id, jti, kind, application_id, "  # noqa: S608
            "agent_actor_id, customer_id, issuer, audience, scopes, issued_at, expires_at, "
            "revoked_at) SELECT mandate_id, jti, 'onboarding', application_id, agent_actor_id, "
            f"customer_id, :issuer, audience, scopes, issued_at, expires_at, revoked_at "
            f"FROM {_LEGACY_MANDATES}"
        ),
        {"issuer": AUTHORIZATION_ISSUER},  # issued by this deployment before Phase 03
    )
    missing = connection.execute(
        text(
            f"SELECT COUNT(*) FROM {_LEGACY_MANDATES} "  # noqa: S608 - fixed table name
            "WHERE mandate_id NOT IN (SELECT mandate_id FROM mandates)"
        )
    ).scalar_one()
    if missing:
        raise SchemaMigrationError(f"{missing} pre-Phase-03 mandates could not be carried over")
    connection.execute(text(f"DROP TABLE {_LEGACY_MANDATES}"))


def init_database(engine: Engine) -> None:
    """Create tables, indexes and triggers if missing, upgrade older schemas, stamp the version."""
    from app.repositories import orm  # noqa: F401  (registers the models on Base.metadata)

    with engine.begin() as connection:
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}."
            )
        _set_aside_v2_mandates(connection)
        Base.metadata.create_all(connection)
        _carry_over_v2_mandates(connection)
        if version < SCHEMA_VERSION:
            connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))  # noqa: S608
