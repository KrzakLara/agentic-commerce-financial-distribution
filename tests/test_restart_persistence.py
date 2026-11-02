"""State restoration after an application restart (new FastAPI app, same SQLite file)."""

import uuid

from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.domain.enums import ApplicationStatus as S
from app.domain.workflow import TransitionAuthority as A
from app.main import create_app
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from tests.conftest import actor

BODY = {
    "provider_id": "fixture-bank",
    "product_id": "depot-basic",
    "channel_id": "direct",
    "customer_ref": "opaque-id",
}


def start(db_url: str) -> TestClient:
    settings = Settings(_env_file=None, environment=Environment.TEST, database_url=db_url)
    return TestClient(create_app(settings))


def test_applications_state_and_audit_trail_survive_a_restart(db_url: str) -> None:
    key = str(uuid.uuid4())
    headers = {**actor("agent-a"), "Idempotency-Key": key}

    with start(db_url) as first_run:
        created = first_run.post("/v1/applications", json=BODY, headers=headers).json()
        factory = first_run.app.state.session_factory  # type: ignore[attr-defined]
        ApplicationService(lambda: SqlAlchemyUnitOfWork(factory)).transition(
            created["application_id"],
            S.AWAITING_AUTHORIZATION,
            authority=A.SYSTEM_POLICY,
            reason_code="handoff_created",
            correlation_id="corr-1",
        )
    # The first app and its engine are now disposed ("process stopped").

    with start(db_url) as second_run:
        restored = second_run.get(
            f"/v1/applications/{created['application_id']}", headers=actor("agent-a")
        )
        assert restored.status_code == 200
        assert restored.json()["state"] == "AWAITING_CUSTOMER"  # workflow state was restored
        assert restored.json()["created_at"] == created["created_at"]

        replay = second_run.post("/v1/applications", json=BODY, headers=headers)  # idempotency too
        assert replay.status_code == 200
        assert replay.json()["application_id"] == created["application_id"]

        factory = second_run.app.state.session_factory  # type: ignore[attr-defined]
        trail = ApplicationService(lambda: SqlAlchemyUnitOfWork(factory)).audit_trail(
            created["application_id"]
        )
        assert [e.event_type for e in trail] == ["application.created", "application.transition"]

        # And the state machine keeps enforcing its rules on the restored data.
        other_actor = second_run.get(
            f"/v1/applications/{created['application_id']}", headers=actor("agent-b")
        )
        assert other_actor.status_code == 404
