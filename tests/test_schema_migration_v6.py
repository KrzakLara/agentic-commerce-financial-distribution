"""Schema version 6 on a REAL version 5 database: new tables, and `review_cases` rebuilt for the two
review reasons version 6 added to its CHECK list.

`tests/fixtures/schema_v5.sql` is the schema that commit 20b4a30's own `init_database` created, so
these tests start from what a version 5 database really holds. A "version 5" database made by
dropping the new tables from a fresh one would already carry the new CHECK list, which is how the
defect stayed hidden: a failed identification or signature then ended in HTTP 500, without the
review case or its audit.
"""

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.integrations.jose_tools import generate_key
from app.repositories.database import (
    SCHEMA_VERSION,
    SchemaMigrationError,
    create_db_engine,
    init_database,
)
from app.repositories.trust_orm import Base as _TrustBase  # noqa: F401
from scripts import customer_steps as steps
from tests.flow import INTENT
from tests.trust_harness import BANK, PROOFING_ID, TrustWorld, sign_event, trust_world

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema_v5.sql"
_NEW_TABLES = {
    "registered_agents",
    "delegations",
    "delegated_mandates",
    "used_jtis",
    "oidc_login_states",
    "identity_bindings",
    "authentication_events",
    "identity_proofing_cases",
    "contract_packages",
    "signature_sessions",
    "partner_relationships",
    "attribution_claims",
    "attribution_decisions",
    "policy_decisions",
    "a2a_tasks",
}
_NEW_REASONS = ("IDENTITY_PROOFING_NOT_COMPLETED", "SIGNATURE_NOT_COMPLETED")
_REVIEW_OBJECTS = {
    ("index", "ix_review_cases_application_id"),
    ("index", "uq_review_cases_one_active_per_application"),
    ("trigger", "trg_review_cases_closed_is_final"),
    ("trigger", "trg_review_cases_facts_are_immutable"),
    ("trigger", "trg_review_cases_no_delete"),
}
_AT = "2026-11-01 09:00:00.000000"
_PROOFING_CALLBACK = "/internal/identity-proofing/callbacks"
_SIGNATURE_CALLBACK = "/internal/signature-provider/callbacks"


def _application(con: sqlite3.Connection, application_id: str) -> None:
    con.execute(
        "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
        "customer_ref, agent_actor_id, status, version, created_at, updated_at) "
        "VALUES (:a, :p, :q, :c, :r, 'agent-a', 'DRAFT', 1, :t, :t)",
        {
            "a": application_id,
            "p": INTENT.provider_id,
            "q": INTENT.product_id,
            "c": INTENT.channel_id,
            "r": INTENT.customer_ref,
            "t": _AT,
        },
    )


def _case(con: sqlite3.Connection, case_id: str, reason: str, status: str, **extra: object) -> None:
    values = {
        "case_id": case_id,
        "application_id": "app-old",
        "reason": reason,
        "status": status,
        "check_type": None,
        "check_attempt": None,
        "request_category": None,
        "interrupted_step": "AWAITING_AUTHORIZATION",
        "opened_by": "SYSTEM",
        "requested_information": None,
        "evidence": "[]",
        "resolution": None,
        "resolved_by": None,
        "resolved_at": None,
        "created_at": _AT,
        "updated_at": _AT,
        "version": 1,
        **extra,
    }
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    con.execute(f"INSERT INTO review_cases ({columns}) VALUES ({placeholders})", values)  # noqa: S608


def _real_v5(db: Path) -> None:
    """A version 5 database as commit 20b4a30 created it, holding an application, a closed review
    case and an open one (version 5 reasons only)."""
    with sqlite3.connect(db) as con:
        con.executescript(FIXTURE.read_text(encoding="utf-8"))
        _application(con, "app-old")
        _application(con, "app-second")
        _case(
            con,
            "rvw-closed",
            "CHECK_FAILED",
            "CLOSED",
            check_type="KYC",
            check_attempt=1,
            interrupted_step="KYC_PENDING",
            evidence='[{"category": "IDENTITY_REVERIFICATION"}]',
            resolution="REJECT",
            resolved_by="op-1",
            resolved_at=_AT,
            version=2,
        )
        _case(con, "rvw-open", "CUSTOMER_REQUESTED_HUMAN", "OPEN", opened_by="CUSTOMER")
        con.execute(
            "INSERT INTO audit_events (event_id, occurred_at, correlation_id, event_type, "
            "actor_type, actor_id, application_id, outcome, details) VALUES ('evt-old', :t, "
            "'cid-old', 'review.case_opened', 'SYSTEM', NULL, 'app-old', 'SUCCESS', '{}')",
            {"t": _AT},
        )
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 5


def _upgrade(db: Path) -> None:
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    try:
        init_database(engine)
    finally:
        engine.dispose()


def _snapshot(db: Path) -> dict[str, object]:
    with sqlite3.connect(db) as con:
        return {
            "schema": con.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall(),
            "review_cases": con.execute("SELECT * FROM review_cases ORDER BY case_id").fetchall(),
            "applications": con.execute(
                "SELECT * FROM applications ORDER BY application_id"
            ).fetchall(),
            "audit_events": con.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall(),
            "version": con.execute("PRAGMA user_version").fetchone()[0],
        }


def _review_table_sql(con: sqlite3.Connection) -> str:
    return con.execute("SELECT sql FROM sqlite_master WHERE name = 'review_cases'").fetchone()[0]


# --- the upgrade itself -------------------------------------------------------------------------


def test_a_version_5_database_gains_the_tables_and_keeps_its_rows(tmp_path: Path) -> None:
    db = tmp_path / "upgrade.db"
    _real_v5(db)
    before = _snapshot(db)
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    init_database(engine)
    assert set(inspect(engine).get_table_names()) >= _NEW_TABLES
    engine.dispose()
    after = _snapshot(db)
    for table in ("applications", "review_cases", "audit_events"):
        assert after[table] == before[table], table
    assert after["version"] == SCHEMA_VERSION == 6


def test_the_review_table_is_rebuilt_with_its_rows_indexes_and_triggers(tmp_path: Path) -> None:
    db = tmp_path / "rebuild.db"
    _real_v5(db)
    with sqlite3.connect(db) as con:
        old_sql = _review_table_sql(con)
    assert not any(reason in old_sql for reason in _NEW_REASONS)  # a real version 5 table
    _upgrade(db)
    with sqlite3.connect(db) as con:
        new_sql = _review_table_sql(con)
        objects = set(
            con.execute(
                "SELECT type, name FROM sqlite_master WHERE tbl_name = 'review_cases'"
            ).fetchall()
        )
        legacy = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '_review_cases_%'"
        ).fetchone()[0]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        foreign = con.execute("PRAGMA foreign_key_list(review_cases)").fetchall()
    assert all(f"'{reason}'" in new_sql for reason in _NEW_REASONS)
    assert objects >= _REVIEW_OBJECTS
    assert legacy == 0
    assert [(row[2], row[3], row[4], row[6]) for row in foreign] == [
        ("applications", "application_id", "application_id", "RESTRICT")
    ]


def test_the_upgraded_review_table_takes_the_new_reasons_and_keeps_its_rules(
    tmp_path: Path,
) -> None:
    db = tmp_path / "rules.db"
    _real_v5(db)
    _upgrade(db)
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA foreign_keys = ON")  # as every connection of the application has it
        _case(con, "rvw-identity", _NEW_REASONS[0], "OPEN", application_id="app-second")
        con.commit()
        with pytest.raises(sqlite3.IntegrityError):  # still one active case per application
            _case(con, "rvw-signature", _NEW_REASONS[1], "OPEN", application_id="app-second")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            con.execute("DELETE FROM review_cases WHERE case_id = 'rvw-open'")
        with pytest.raises(sqlite3.IntegrityError, match="closed review case never changes"):
            con.execute("UPDATE review_cases SET status = 'OPEN' WHERE case_id = 'rvw-closed'")
        with pytest.raises(sqlite3.IntegrityError, match="ck_review_cases_reason"):
            _case(con, "rvw-unknown", "SOMETHING_ELSE", "CLOSED", resolution="REJECT")
        with pytest.raises(sqlite3.IntegrityError):  # the foreign key still holds
            _case(con, "rvw-orphan", _NEW_REASONS[1], "OPEN", application_id="app-missing")
        reasons = con.execute(
            "SELECT case_id, reason FROM review_cases ORDER BY case_id"
        ).fetchall()
    assert reasons == [
        ("rvw-closed", "CHECK_FAILED"),
        ("rvw-identity", "IDENTITY_PROOFING_NOT_COMPLETED"),
        ("rvw-open", "CUSTOMER_REQUESTED_HUMAN"),
    ]


def test_a_failed_upgrade_leaves_the_version_5_database_exactly_as_it_was(tmp_path: Path) -> None:
    """Two active cases for one application cannot enter the rebuilt table. The upgrade refuses,
    and everything it did before, the renamed table and the new tables included, is undone."""
    db = tmp_path / "failed.db"
    _real_v5(db)
    with sqlite3.connect(db) as con:  # damage only this copy: lift the one-active-case rule
        con.execute("DROP INDEX uq_review_cases_one_active_per_application")
        _case(con, "rvw-open-twice", "KYC_REVIEW_REQUIRED", "OPEN")
    before = _snapshot(db)
    with pytest.raises(SchemaMigrationError, match="1 rows of review_cases"):
        _upgrade(db)
    assert _snapshot(db) == before
    with pytest.raises(SchemaMigrationError):  # the next start refuses again and changes nothing
        _upgrade(db)
    assert _snapshot(db) == before


def test_upgrading_a_real_version_5_database_twice_changes_nothing_more(tmp_path: Path) -> None:
    db = tmp_path / "twice-v5.db"
    _real_v5(db)
    _upgrade(db)
    once = _snapshot(db)
    _upgrade(db)
    assert _snapshot(db) == once
    assert once["version"] == SCHEMA_VERSION


def test_upgrading_twice_is_harmless(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    url = f"sqlite:///{db.as_posix()}"
    for _ in range(2):
        engine = create_db_engine(url)
        init_database(engine)
        engine.dispose()
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
        assert all(f"'{reason}'" in _review_table_sql(con) for reason in _NEW_REASONS)


# --- the journey on an upgraded database, over the real routes ------------------------------------


def _upgraded_world(tmp_path: Path) -> AbstractContextManager[TrustWorld]:
    """The bank opens a real version 5 database: its own start-up upgrades it, as in deployment."""
    db = tmp_path / "bank-v5.db"
    with sqlite3.connect(db) as con:
        con.executescript(FIXTURE.read_text(encoding="utf-8"))
    return trust_world(database_url=f"sqlite:///{db.as_posix()}")


def test_on_an_upgraded_database_a_rejected_identity_goes_to_a_person(tmp_path: Path) -> None:
    with _upgraded_world(tmp_path) as world:
        assert world.sql("PRAGMA user_version") == [(6,)]
        agent = world.agent()
        browser = world.login("alice")
        started = world.start_application(agent, "alice")
        link = world.request_link(agent, started)
        page = world.complete_proofing(browser, link, outcome="REJECTED")
        # The provider's page reports the HTTP status the bank answered its callback with.
        assert page.status_code == 200 and "Bank answered: 200" in page.text
        app_id = started["application_id"]
        assert world.status_of(app_id) == "MANUAL_REVIEW"
        assert world.sql(
            "SELECT reason, status FROM review_cases WHERE application_id = :a", a=app_id
        ) == [("IDENTITY_PROOFING_NOT_COMPLETED", "OPEN")]
        events = world.event_types(app_id)
        assert "identity_proofing.result" in events and "review.case_opened" in events
        assert "provider.confirmation_accepted" not in events


def test_on_an_upgraded_database_a_declined_signature_goes_to_a_person(tmp_path: Path) -> None:
    with _upgraded_world(tmp_path) as world:
        agent = world.agent()
        state = world.to_contract_pending(agent)
        app_id = state["application_id"]
        reference = world.authorize_signing(state["browser"], app_id)
        page = world.signer_outcome(reference, "DECLINED")
        assert page.status_code == 200 and "Bank answered: 200" in page.text
        assert world.status_of(app_id) == "MANUAL_REVIEW"
        assert world.sql(
            "SELECT reason, status FROM review_cases WHERE application_id = :a", a=app_id
        ) == [("SIGNATURE_NOT_COMPLETED", "OPEN")]
        assert world.sql("SELECT state FROM signature_sessions") == [("DECLINED",)]
        events = world.event_types(app_id)
        assert "contract.signature_result" in events and "review.case_opened" in events


def test_on_an_upgraded_database_invalid_callbacks_are_still_refused(tmp_path: Path) -> None:
    with _upgraded_world(tmp_path) as world:
        wire = TestClient(world.app, base_url=BANK, raise_server_exceptions=False)
        for path in (_PROOFING_CALLBACK, _SIGNATURE_CALLBACK):
            answer = wire.post(path, content="not-a-signed-event")
            assert answer.status_code == 403
            assert answer.json()["error"]["code"] == "callback_rejected"
        agent = world.agent()
        browser = world.login("alice")
        started = world.start_application(agent, "alice")
        link = world.request_link(agent, started)
        form = steps.page_form(browser.get(link).text, "/identity-proofing")
        browser.post(
            form["_action"], data={"token": form["token"], "csrf_token": form["csrf_token"]}
        )
        case = world.sql("SELECT case_id, provider_ref, customer_ref FROM identity_proofing_cases")
        forged = sign_event(
            generate_key("intruder-1"),
            "identity-proofing-event+jwt",
            {
                "iss": PROOFING_ID,
                "case_id": case[0][0],
                "provider_ref": case[0][1],
                "application_id": started["application_id"],
                "customer_ref": case[0][2],
                "status": "VERIFIED",
                "method": "SYNTHETIC_TEST",
                "assurance": "synthetic_no_assurance",
                "regulatory_evidence": False,
            },
            world.clock,
        )
        answer = wire.post(_PROOFING_CALLBACK, content=forged)
        assert answer.status_code == 403
        assert world.status_of(started["application_id"]) == "AWAITING_AUTHORIZATION"
        assert world.sql("SELECT status FROM identity_proofing_cases") == [("PENDING",)]
        assert world.sql("SELECT COUNT(*) FROM review_cases") == [(0,)]
