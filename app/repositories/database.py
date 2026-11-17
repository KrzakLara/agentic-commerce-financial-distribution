"""SQLAlchemy engine/session configuration and schema initialization for SQLite.

* `create_db_engine` is lazy: no connection (and no database file) until first use.
* `init_database` creates the schema, upgrades older schemas in place and records the schema
  version in ``PRAGMA user_version``. A database from a newer version is refused.

The whole upgrade runs in ONE explicit transaction (`_begin_atomic`). Through the pysqlite driver,
DDL otherwise commits statement by statement, so a failure after a rename used to leave a
half-upgraded schema behind. Now a failed step rolls everything back and the database stays at its
old version. Each step still checks the current state first, so a database that an older version
of this code left half-upgraded is completed on the next start.

Upgrade steps:

* v2 -> v3 (Phase 03): ``mandates`` is rebuilt (nullable application binding, ``kind``,
  ``issuer``); rows are carried over as ``onboarding`` grants.
* v3 -> v4 (Phase 04): ``check_results`` is rebuilt (numbered ``attempt``s; rows carried over
  as attempt 1); ``disclosure_receipts`` gains its binding columns; the
  application-identity trigger is added; ``review_cases`` and ``channel_attributions`` are new.
* v4 -> v5 (final integration): ``tax_information`` and ``appropriateness_assessments`` are new
  tables. Nothing existing changes; ``create_all`` adds them and existing rows stay untouched.
* v5 -> v6 (trust integration): new tables (`app.repositories.trust_orm`), in the same way, and
  ``review_cases`` is rebuilt: v6 added two review reasons (`IDENTITY_PROOFING_NOT_COMPLETED`,
  `SIGNATURE_NOT_COMPLETED`) to its CHECK list. Rows, indexes and triggers are carried over.

SQLite cannot drop a NOT NULL / UNIQUE constraint or change a CHECK constraint in place, hence
"rebuild": rename the old table, let ``create_all`` create the new one with all constraints,
indexes and triggers, copy the rows, verify none is missing, drop the old table.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.domain.enums import ReviewCaseReason
from app.domain.mandate import AUTHORIZATION_ISSUER

# 2: Phase 02 adds mandates, handoffs, receipts, checks
# 3: Phase 03 rebuilds mandates; adds agent_access_tokens and customer_sessions
# 4: Phase 04 check attempts, review cases, channel attributions, disclosure binding
# 5: final integration adds tax_information and appropriateness_assessments (new tables only)
# 6: trust integration adds delegation, identity, contract, attribution and policy tables, and
#    rebuilds review_cases for its two new reasons
SCHEMA_VERSION = 6
_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


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


def _table_sql(connection: Connection, table: str) -> str:
    """The table's CREATE statement as SQLite stores it, or an empty string."""
    sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).scalar()
    return sql or ""


@dataclass(frozen=True)
class _Rebuild:
    table: str
    legacy: str  # temporary name of the old table while the new one is filled
    marker: str | None  # a column only the NEW schema has: its absence means "old schema"
    triggers: tuple[str, ...]  # old triggers that would otherwise follow the renamed table
    columns: str  # target column list
    select: str  # matching select list over the old table (constants allowed)
    key: str  # primary key used to verify that every row was carried over
    # Text only the NEW table definition contains, such as a value added to a CHECK list: its
    # absence means "old schema" as well.
    marker_sql: tuple[str, ...] = ()
    indexes: tuple[str, ...] = ()  # old named indexes: they would follow the renamed table too


_REVIEW_CASE_COLUMNS = (
    "case_id, application_id, reason, status, check_type, check_attempt, request_category, "
    "interrupted_step, opened_by, requested_information, evidence, resolution, resolved_by, "
    "resolved_at, created_at, updated_at, version"
)
_REBUILDS = (
    _Rebuild(  # v2 -> v3
        table="mandates",
        legacy="_mandates_v2",
        marker="kind",
        triggers=("trg_mandates_revocation_is_final",),
        columns="mandate_id, jti, kind, application_id, agent_actor_id, customer_id, issuer, "
        "audience, scopes, issued_at, expires_at, revoked_at",
        select="mandate_id, jti, 'onboarding', application_id, agent_actor_id, customer_id, "
        f"'{AUTHORIZATION_ISSUER}', audience, scopes, issued_at, expires_at, revoked_at",
        key="mandate_id",
    ),
    _Rebuild(  # v3 -> v4
        table="check_results",
        legacy="_check_results_v3",
        marker="attempt",
        triggers=("trg_check_results_no_update", "trg_check_results_no_delete"),
        columns="id, application_id, check_type, outcome, reference, mock, created_at, attempt",
        select="id, application_id, check_type, outcome, reference, mock, created_at, 1",
        key="id",
    ),
    _Rebuild(  # v5 -> v6: two review reasons were added to the CHECK list
        table="review_cases",
        legacy="_review_cases_v5",
        marker=None,
        marker_sql=tuple(f"'{reason.value}'" for reason in ReviewCaseReason),
        triggers=(
            "trg_review_cases_closed_is_final",
            "trg_review_cases_facts_are_immutable",
            "trg_review_cases_no_delete",
        ),
        indexes=("ix_review_cases_application_id", "uq_review_cases_one_active_per_application"),
        columns=_REVIEW_CASE_COLUMNS,
        select=_REVIEW_CASE_COLUMNS,
        key="case_id",
    ),
)
# v3 -> v4: plain additions to an existing table (nullable, so ALTER TABLE ADD COLUMN works).
_ADDED_COLUMNS = {
    "disclosure_receipts": (
        ("provider_id", "VARCHAR(64)"),
        ("product_id", "VARCHAR(64)"),
        ("product_version", "VARCHAR(200)"),
        ("terms_digest", "VARCHAR(64)"),
        ("action_digest", "VARCHAR(64)"),
    ),
}


def _is_old(connection: Connection, step: _Rebuild, columns: set[str]) -> bool:
    if step.marker is not None and step.marker not in columns:
        return True
    definition = _table_sql(connection, step.table)
    return any(fragment not in definition for fragment in step.marker_sql)


def _set_aside(connection: Connection, step: _Rebuild) -> None:
    """Rebuild, part 1: move an old-schema table out of the way (if there is one)."""
    columns = _columns(connection, step.table)
    if not columns or _columns(connection, step.legacy) or not _is_old(connection, step, columns):
        return
    for trigger in step.triggers:  # else CREATE TRIGGER IF NOT EXISTS would skip the new table
        connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
    for index in step.indexes:  # else create_all could not create the new table's indexes
        connection.execute(text(f"DROP INDEX IF EXISTS {index}"))
    connection.execute(text(f"ALTER TABLE {step.table} RENAME TO {step.legacy}"))


def _carry_over(connection: Connection, step: _Rebuild) -> None:
    """Rebuild, part 2 (after create_all): copy the rows, verify, drop the old table."""
    if not _columns(connection, step.legacy):
        return
    connection.execute(
        text(
            f"INSERT OR IGNORE INTO {step.table} ({step.columns}) "  # noqa: S608 - fixed names
            f"SELECT {step.select} FROM {step.legacy}"
        )
    )
    missing = connection.execute(
        text(
            f"SELECT COUNT(*) FROM {step.legacy} "  # noqa: S608 - fixed names
            f"WHERE {step.key} NOT IN (SELECT {step.key} FROM {step.table})"
        )
    ).scalar_one()
    if missing:
        raise SchemaMigrationError(f"{missing} rows of {step.table} could not be carried over")
    connection.execute(text(f"DROP TABLE {step.legacy}"))


def _add_missing_columns(connection: Connection) -> None:
    for table, additions in _ADDED_COLUMNS.items():
        existing = _columns(connection, table)
        for name, sql_type in additions:
            if existing and name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def _begin_atomic(connection: Connection) -> None:
    """Open the transaction explicitly, so that DDL is part of it and rolls back with it.

    pysqlite only begins a transaction before data changes; without this, every CREATE, ALTER and
    DROP of an upgrade would commit at once. IMMEDIATE also keeps a second starting process out
    until the upgrade is done.
    """
    raw = connection.connection.dbapi_connection
    if connection.dialect.name == "sqlite" and not getattr(raw, "in_transaction", True):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def init_database(engine: Engine) -> None:
    """Create tables, indexes and triggers if missing, upgrade older schemas, stamp the version.

    All or nothing: a failure rolls the whole upgrade back (see the module docstring).
    """
    from app.repositories import orm, trust_orm  # noqa: F401 (registers the models on Base.metadata)

    with engine.begin() as connection:
        _begin_atomic(connection)
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}."
            )
        for step in _REBUILDS:
            _set_aside(connection, step)
        Base.metadata.create_all(connection)
        for step in _REBUILDS:
            _carry_over(connection, step)
        _add_missing_columns(connection)
        # Triggers on tables that already existed (create_all only adds them to new tables).
        connection.execute(text(orm.APPLICATION_IDENTITY_TRIGGER))
        if version < SCHEMA_VERSION:
            connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))  # noqa: S608
