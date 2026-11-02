"""POST /v1/applications and GET /v1/applications/{id} over HTTP (ASGI)."""

import uuid

import pytest
from fastapi import FastAPI
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
PUBLIC_FIELDS = {
    "application_id", "provider_id", "product_id", "channel_id",
    "state", "created_at", "updated_at",
}  # fmt: skip


def headers(actor_id: str = "agent-a", key: str | None = None) -> dict[str, str]:
    return {**actor(actor_id), "Idempotency-Key": key or str(uuid.uuid4())}


def service_for(client: TestClient) -> ApplicationService:
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    return ApplicationService(lambda: SqlAlchemyUnitOfWork(factory))


def trusted_move(client: TestClient, application_id: str, *path: tuple[S, A]) -> None:
    service = service_for(client)
    for target, authority in path:
        service.transition(
            application_id, target, authority=authority, reason_code="test", correlation_id="corr-t"
        )


# --- create ------------------------------------------------------------


def test_post_creates_a_draft_application(client: TestClient) -> None:
    response = client.post("/v1/applications", json=BODY, headers=headers())
    assert response.status_code == 201
    body = response.json()
    assert set(body) == PUBLIC_FIELDS
    assert body["state"] == "DRAFT"
    assert body["application_id"].startswith("app-")
    assert (body["provider_id"], body["channel_id"]) == ("fixture-bank", "direct")
    assert response.headers["x-correlation-id"]
    assert "idempotent-replayed" not in response.headers


def test_created_application_can_be_read_back_by_its_owner(client: TestClient) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers()).json()
    response = client.get(f"/v1/applications/{created['application_id']}", headers=actor("agent-a"))
    assert response.status_code == 200
    assert response.json() == created


def test_response_never_exposes_internal_fields(client: TestClient) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers()).json()
    text = client.get(
        f"/v1/applications/{created['application_id']}", headers=actor("agent-a")
    ).text
    for internal in (
        "customer_ref",
        "opaque-id",
        "agent_actor_id",
        "agent-a",
        "version",
        "resume",
        "audit",
    ):
        assert internal not in text


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "OPENED"},
        {"state": "OPENED"},
        {"application_id": "app-x"},
        {"agent_actor_id": "other"},
    ],
)
def test_agent_cannot_smuggle_state_or_identity_fields_into_the_request(
    client: TestClient, extra: dict[str, str]
) -> None:
    response = client.post("/v1/applications", json={**BODY, **extra}, headers=headers())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize(
    "bad",
    [
        {"provider_id": ""},
        {"product_id": "has space"},
        {"channel_id": "x" * 65},
        {"customer_ref": "a/b"},
    ],
)
def test_invalid_field_values_are_rejected(client: TestClient, bad: dict[str, str]) -> None:
    assert (
        client.post("/v1/applications", json={**BODY, **bad}, headers=headers()).status_code == 422
    )


def test_missing_field_is_rejected(client: TestClient) -> None:
    body = {k: v for k, v in BODY.items() if k != "customer_ref"}
    assert client.post("/v1/applications", json=body, headers=headers()).status_code == 422


def test_idempotency_key_is_required_and_must_be_a_uuid(client: TestClient) -> None:
    assert client.post("/v1/applications", json=BODY, headers=actor("agent-a")).status_code == 422
    bad = {**actor("agent-a"), "Idempotency-Key": "not-a-uuid"}
    assert client.post("/v1/applications", json=BODY, headers=bad).status_code == 422


# --- idempotency ------------------------------------------------------------


def test_same_actor_key_and_body_returns_the_same_application(client: TestClient) -> None:
    h = headers(key=str(uuid.uuid4()))
    first = client.post("/v1/applications", json=BODY, headers=h)
    second = client.post("/v1/applications", json=BODY, headers=h)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()


def test_same_key_with_a_different_body_returns_409(client: TestClient) -> None:
    h = headers()
    client.post("/v1/applications", json=BODY, headers=h)
    response = client.post("/v1/applications", json={**BODY, "channel_id": "partner-1"}, headers=h)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_replay_reports_current_generic_state_not_a_stale_one(client: TestClient) -> None:
    h = headers()
    created = client.post("/v1/applications", json=BODY, headers=h).json()
    trusted_move(client, created["application_id"], (S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY))
    replay = client.post("/v1/applications", json=BODY, headers=h).json()
    assert replay["application_id"] == created["application_id"]
    assert replay["state"] == "AWAITING_CUSTOMER"


def test_actors_cannot_reuse_each_others_key_or_application(client: TestClient) -> None:
    key = str(uuid.uuid4())
    a = client.post("/v1/applications", json=BODY, headers=headers("agent-a", key))
    b = client.post("/v1/applications", json=BODY, headers=headers("agent-b", key))
    assert (a.status_code, b.status_code) == (201, 201)  # b did NOT get a's application
    assert a.json()["application_id"] != b.json()["application_id"]


# --- actor scoping ------------------------------------------------------------


def test_another_actor_cannot_read_the_application_and_cannot_tell_it_exists(
    client: TestClient,
) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers("agent-a")).json()
    foreign = client.get(f"/v1/applications/{created['application_id']}", headers=actor("agent-b"))
    missing = client.get("/v1/applications/app-does-not-exist", headers=actor("agent-b"))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_cross_actor_read_attempt_is_audited(client: TestClient) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers("agent-a")).json()
    client.get(f"/v1/applications/{created['application_id']}", headers=actor("agent-b"))
    events = service_for(client).audit_trail(created["application_id"])
    assert [(e.event_type, e.actor_id) for e in events] == [
        ("application.created", "agent-a"),
        ("application.access_denied", "agent-b"),
    ]


def test_audit_correlation_id_matches_the_http_response_header(client: TestClient) -> None:
    response = client.post(
        "/v1/applications", json=BODY, headers={**headers(), "X-Correlation-ID": "req-42"}
    )
    events = service_for(client).audit_trail(response.json()["application_id"])
    assert response.headers["x-correlation-id"] == events[0].correlation_id == "req-42"


def test_internal_states_are_reported_coarsely(client: TestClient) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers()).json()
    app_id = created["application_id"]

    def state() -> str:
        return client.get(f"/v1/applications/{app_id}", headers=actor("agent-a")).json()["state"]

    seen = {state()}
    for target, authority in [
        (S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY),
        (S.AUTHORIZED, A.CUSTOMER_CHANNEL),
        (S.KYC_PENDING, A.SYSTEM_POLICY),
        (S.COMPLIANCE_PENDING, A.SYSTEM_POLICY),
    ]:
        trusted_move(client, app_id, (target, authority))
        seen.add(state())
    assert seen == {"DRAFT", "AWAITING_CUSTOMER", "IN_PROGRESS"}  # KYC vs compliance is invisible

    trusted_move(client, app_id, (S.MANUAL_REVIEW, A.SYSTEM_POLICY))
    assert state() == "ACTION_REQUIRED"  # manual review is shown as a generic action status
    assert (
        "MANUAL_REVIEW"
        not in client.get(f"/v1/applications/{app_id}", headers=actor("agent-a")).text
    )


# --- DEV/TEST actor identification is not authentication ------------------------------------


@pytest.mark.parametrize("method", ["post", "get"])
def test_requests_without_an_actor_get_401(client: TestClient, method: str) -> None:
    if method == "post":
        response = client.post(
            "/v1/applications", json=BODY, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
    else:
        response = client.get("/v1/applications/app-1")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_malformed_actor_id_gets_401(client: TestClient) -> None:
    bad = {"X-Dev-Actor-Id": "agent a; DROP TABLE", "Idempotency-Key": str(uuid.uuid4())}
    assert client.post("/v1/applications", json=BODY, headers=bad).status_code == 401


def test_dev_actor_header_is_never_honoured_in_production(db_url: str) -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION, database_url=db_url)
    assert settings.dev_actor_auth_enabled and not settings.dev_actor_auth_active
    with TestClient(create_app(settings)) as prod_client:
        response = prod_client.post("/v1/applications", json=BODY, headers=headers())
        assert response.status_code == 401
        assert (
            prod_client.get("/v1/applications/app-1", headers=actor("agent-a")).status_code == 401
        )
        assert prod_client.get("/health").status_code == 200  # health stays public


def test_dev_actor_auth_can_be_switched_off(db_url: str) -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=db_url,
        dev_actor_auth_enabled=False,
    )
    with TestClient(create_app(settings)) as off_client:
        assert off_client.post("/v1/applications", json=BODY, headers=headers()).status_code == 401


# --- the agent has no way to change state through the API -----------------------------------


def test_only_the_expected_endpoints_exist(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    assert {path: sorted(methods) for path, methods in paths.items()} == {
        "/health": ["get"],
        "/v1/providers/{provider_id}/manifest": ["get"],
        "/v1/products": ["get"],
        "/v1/applications": ["post"],
        "/v1/applications/{application_id}": ["get"],
        "/v1/applications/{application_id}/handoff": ["post"],
        "/v1/applications/{application_id}/submit": ["post"],
        "/v1/customer/handoffs/{handoff_id}/confirm": ["post"],
    }
    # The provider callback and the HTML pages exist but are not part of the public contract.
    assert not any(p.startswith(("/internal", "/customer")) for p in paths)


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_application_resource_cannot_be_modified_or_deleted(
    client: TestClient, method: str
) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers()).json()
    response = client.request(
        method,
        f"/v1/applications/{created['application_id']}",
        json={"status": "OPENED"},
        headers=actor("agent-a"),
    )
    assert response.status_code == 405


@pytest.mark.parametrize("suffix", ["status", "transition", "state", "open", "opened"])
def test_there_is_no_state_changing_sub_resource(client: TestClient, suffix: str) -> None:
    created = client.post("/v1/applications", json=BODY, headers=headers()).json()
    for method in ("post", "put", "patch"):
        response = client.request(
            method,
            f"/v1/applications/{created['application_id']}/{suffix}",
            json={"status": "OPENED"},
            headers=actor("agent-a"),
        )
        assert response.status_code in {404, 405}
    after = client.get(
        f"/v1/applications/{created['application_id']}", headers=actor("agent-a")
    ).json()
    assert after["state"] == "DRAFT"
