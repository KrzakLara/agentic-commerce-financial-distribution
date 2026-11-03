"""Schema version 3 (Phase 03) -> 4 (Phase 04): existing data must survive, nothing silently.

`tests/fixtures/schema_v3.sql` is the exact DDL the Phase 03 code produced (captured from commit
85aaa2a). Each test builds a real v3 database with Phase 03 rows - including an application the
customer approved under the Phase 03 action format (no terms digest, no partner) - and runs the
CURRENT `init_database` on it.
"""

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import Environment, Settings
from app.domain.mandate import AGENT_API_AUDIENCE, AUTHORIZATION_ISSUER
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.main import create_app
from app.repositories.database import (
    SCHEMA_VERSION,
    create_db_engine,
    create_session_factory,
    init_database,
)
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import AuthorizationService
from tests.flow import human_csrf, sign_in_browser

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema_v3.sql"


def ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def phase03_action(app_id: str) -> tuple[dict[str, str], str]:
    """The action exactly as Phase 03 stored and digested it (no terms, no partner)."""
    product = FixtureProviderDiscovery().list_products("fixture-bank")[0]
    action = {
        "action_type": "OPEN_DEPOT_ACCOUNT", "application_id": app_id,
        "provider_id": "fixture-bank", "product_id": "depot-basic", "channel_id": "direct",
        "customer_ref": "cust-1", "catalog_version": product.catalog_version,
        "document_id": product.risk_document_id,
        "document_version": product.risk_document_version,
        "document_digest": product.risk_document_digest,
    }  # fmt: skip
    canonical = json.dumps(action, sort_keys=True, separators=(",", ":"))
    return action, hashlib.sha256(canonical.encode()).hexdigest()


def build_v3_database(path: Path) -> dict[str, str]:
    """A Phase 03 database: one consented application waiting for submit, one parked in
    MANUAL_REVIEW by a compliance result (Phase 03 had no review cases)."""
    now = datetime.now(UTC)
    ready, parked = f"app-{uuid.uuid4().hex}", f"app-{uuid.uuid4().hex}"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    for app_id in (ready, parked):
        con.execute(
            "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
            "customer_ref, agent_actor_id, status, resume_status, version, created_at, "
            "updated_at) VALUES (?, 'fixture-bank', 'depot-basic', 'direct', 'cust-1', "
            "'agent-a', 'DRAFT', NULL, 1, ?, ?)",
            (app_id, ts(now), ts(now)),
        )
        for kind, scopes in (
            ("onboarding", '["request_handoff", "submit_after_consent"]'),
            ("status", '["read_status"]'),
        ):
            con.execute(
                "INSERT INTO mandates (mandate_id, jti, kind, application_id, agent_actor_id, "
                "customer_id, issuer, audience, scopes, issued_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, 'agent-a', 'cust-1', ?, ?, ?, ?, ?, NULL)",
                (
                    f"mdt-{kind}-{app_id}", f"jti-{kind}-{app_id}", kind, app_id,
                    AUTHORIZATION_ISSUER, AGENT_API_AUDIENCE, scopes,
                    ts(now - timedelta(minutes=5)), ts(now + timedelta(hours=1)),
                ),
            )  # fmt: skip
        action, digest = phase03_action(app_id)
        con.execute(
            "INSERT INTO handoffs (handoff_id, application_id, nonce_hash, action, action_digest, "
            "created_at, expires_at, customer_id, consumed_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'cust-1', ?, NULL)",
            (f"hnd-{app_id}", app_id, "f" * 64, json.dumps(action), digest,
             ts(now), ts(now + timedelta(minutes=10)), ts(now)),
        )  # fmt: skip
        con.execute(
            "INSERT INTO consent_receipts VALUES (?, ?, 'cust-1', ?, ?, ?)",
            (f"cns-{app_id}", app_id, f"hnd-{app_id}", digest, ts(now)),
        )
        con.execute(
            "INSERT INTO disclosure_receipts VALUES (?, ?, 'cust-1', ?, ?, ?, ?)",
            (f"dsc-{app_id}", app_id, action["document_id"], action["document_version"],
             action["document_digest"], ts(now)),
        )  # fmt: skip
    results = [
        (ready, "KYC", "PASS"), (ready, "COMPLIANCE", "PASS"), (ready, "CONTRACT", "PASS"),
        (parked, "KYC", "PASS"), (parked, "COMPLIANCE", "REVIEW_REQUIRED"),
    ]  # fmt: skip
    for app_id, check, outcome in results:
        con.execute(
            "INSERT INTO check_results (application_id, check_type, outcome, reference, mock, "
            "created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (app_id, check, outcome, f"MOCK-{check}-{app_id}", ts(now)),
        )
    con.execute(
        "UPDATE applications SET status = 'CONTRACT_PENDING', version = 6 WHERE application_id = ?",
        (ready,),
    )
    con.execute(
        "UPDATE applications SET status = 'MANUAL_REVIEW', resume_status = 'COMPLIANCE_PENDING', "
        "version = 5 WHERE application_id = ?",
        (parked,),
    )
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    con.close()
    return {"ready": ready, "parked": parked}


def objects(path: Path, kind: str) -> set[str]:
    with sqlite3.connect(path) as con:
        return {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def upgrade(path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_database(engine)
    finally:
        engine.dispose()


def test_a_phase03_database_is_upgraded_in_place_without_losing_data(tmp_path: Path) -> None:
    db = tmp_path / "v3.db"
    build_v3_database(db)
    with sqlite3.connect(db) as con:
        before = {
            table: con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in ("applications", "mandates", "handoffs", "consent_receipts")
        }
        old_checks = con.execute("SELECT * FROM check_results ORDER BY id").fetchall()
        old_disclosures = con.execute("SELECT * FROM disclosure_receipts ORDER BY 1").fetchall()

    upgrade(db)

    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        for table, rows in before.items():
            assert con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows  # noqa: S608
        checks = con.execute(
            "SELECT id, application_id, check_type, outcome, reference, mock, created_at, attempt "
            "FROM check_results ORDER BY id"
        ).fetchall()
        disclosures = con.execute(
            "SELECT receipt_id, application_id, customer_id, document_id, document_version, "
            "document_digest, acknowledged_at, provider_id, product_id, product_version, "
            "terms_digest, action_digest FROM disclosure_receipts ORDER BY 1"
        ).fetchall()
        assert con.execute("SELECT COUNT(*) FROM review_cases").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM channel_attributions").fetchone() == (0,)
    assert [row[:7] for row in checks] == old_checks  # every row, unchanged ...
    assert {row[7] for row in checks} == {1}  # ... as attempt 1
    assert [row[:7] for row in disclosures] == old_disclosures
    assert {row[7:] for row in disclosures} == {(None,) * 5}  # Phase 04 binding unknown: NULL
    assert "_check_results_v3" not in objects(db, "table")
    assert {
        "trg_check_results_no_update",
        "trg_check_results_no_delete",
        "trg_applications_identity_is_final",
        "trg_review_cases_closed_is_final",
        "trg_channel_attributions_no_update",
    } <= objects(db, "trigger")


def test_the_upgraded_tables_enforce_the_phase04_rules(tmp_path: Path) -> None:
    db = tmp_path / "v3.db"
    ids = build_v3_database(db)
    upgrade(db)
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.begin() as con, pytest.raises(IntegrityError, match="append-only"):
            con.exec_driver_sql("UPDATE check_results SET outcome = 'PASS'")
        with engine.begin() as con, pytest.raises(IntegrityError, match="UNIQUE"):
            con.exec_driver_sql(  # attempt 1 of the same check, a second time
                "INSERT INTO check_results (application_id, check_type, outcome, reference, "
                "mock, created_at, attempt) SELECT application_id, check_type, 'PASS', reference, "
                "mock, created_at, 1 FROM check_results WHERE application_id = ? LIMIT 1",
                (ids["parked"],),
            )
        with engine.begin() as con, pytest.raises(IntegrityError, match="final"):
            con.exec_driver_sql(
                "UPDATE applications SET channel_id = 'partner-1' WHERE application_id = ?",
                (ids["ready"],),
            )
    finally:
        engine.dispose()


def test_upgrading_twice_is_harmless(tmp_path: Path) -> None:
    db = tmp_path / "v3.db"
    build_v3_database(db)
    upgrade(db)
    with sqlite3.connect(db) as con:
        first = con.execute("SELECT * FROM check_results ORDER BY id").fetchall()
    upgrade(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT * FROM check_results ORDER BY id").fetchall() == first
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_an_upgrade_interrupted_after_the_first_step_resumes(tmp_path: Path) -> None:
    db = tmp_path / "v3.db"
    build_v3_database(db)
    with sqlite3.connect(db) as con:  # what a crash right after setting the old table aside leaves
        con.execute("DROP TRIGGER trg_check_results_no_update")
        con.execute("DROP TRIGGER trg_check_results_no_delete")
        con.execute("ALTER TABLE check_results RENAME TO _check_results_v3")
        con.commit()
    upgrade(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*), MIN(attempt) FROM check_results").fetchone() == (5, 1)
    assert "_check_results_v3" not in objects(db, "table")
    assert "trg_check_results_no_update" in objects(db, "trigger")


def test_phase03_work_in_flight_continues_after_the_upgrade(tmp_path: Path) -> None:
    """A Phase 03 approval (no terms/partner binding) still submits; a Phase 03 application
    parked in MANUAL_REVIEW without a case can be picked up through a review case."""
    db = tmp_path / "v3.db"
    ids = build_v3_database(db)
    settings = Settings(
        _env_file=None, environment=Environment.TEST, database_url=f"sqlite:///{db.as_posix()}"
    )
    with TestClient(create_app(settings)) as client:  # startup runs the upgrade
        factory = create_session_factory(client.app.state.engine)  # type: ignore[attr-defined]
        operator = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory), bootstrap_enabled=True
        )
        credentials = operator.reissue_for_application(
            application_id=ids["ready"], correlation_id="corr-op"
        )
        submitted = client.post(
            f"/v1/applications/{ids['ready']}/submit",
            headers={"Authorization": f"Bearer {credentials.onboarding.token}"},
        )
        assert submitted.status_code == 202, submitted.text
        sign_in_browser(client, "cust-1")  # the customer's (simulated) bank session
        asked = client.post(
            f"/v1/customer/applications/{ids['parked']}/request-human",
            json={"category": "HELP_WITH_PROCESS"},
            headers={"X-CSRF-Token": human_csrf(client, ids["parked"])},
        )
        assert asked.status_code == 202
    with sqlite3.connect(db) as con:
        assert con.execute(
            "SELECT status FROM applications WHERE application_id = ?", (ids["ready"],)
        ).fetchone() == ("SUBMITTED_TO_DEPOT_PROVIDER",)
        assert con.execute(
            "SELECT reason, interrupted_step FROM review_cases WHERE application_id = ?",
            (ids["parked"],),
        ).fetchall() == [("CUSTOMER_REQUESTED_HUMAN", "COMPLIANCE_PENDING")]
