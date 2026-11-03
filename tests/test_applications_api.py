"""POST /v1/applications and GET /v1/applications/{id} over HTTP (ASGI)."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.exceptions import ServiceDisabledError
from app.domain.enums import ApplicationStatus as S
from app.domain.workflow import TransitionAuthority as A
from app.main import create_app
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.authorization_service import AuthorizationService
from tests.conftest import bootstrap_headers
from tests.flow import FORGED_TOKEN, bearer

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


def headers(client: TestClient, actor_id: str = "agent-a", key: str | None = None) -> dict:
    """A fresh bootstrap grant (create ONE draft for "opaque-id") + an Idempotency-Key."""
    return {**bootstrap_headers(client, actor_id), "Idempotency-Key": key or str(uuid.uuid4())}


def status_auth(created: dict) -> dict[str, str]:
    return bearer(created["authorization"]["status"]["access_token"])


def public(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "authorization"}


def service_for(client: TestClient) -> ApplicationService:
    factory = client.app.state.session_factory  # type: ignore[attr-defined]
    return ApplicationService(lambda: SqlAlchemyUnitOfWork(factory))


def trusted_move(client: TestClient, application_id: str, *path: tuple[S, A]) -> None:
    service = service_for(client)
    for target, authority in path:
        service.transition(
            application_id, target, authority=authority, reason_code="test", correlation_id="corr-t"
        )


def create(client: TestClient, actor_id: str = "agent-a") -> dict:
    response = client.post("/v1/applications", json=BODY, headers=headers(client, actor_id))
    assert response.status_code == 201, response.text
    return response.json()


# --- create ------------------------------------------------------------


def test_post_creates_a_draft_application(client: TestClient) -> None:
    response = client.post("/v1/applications", json=BODY, headers=headers(client))
    assert response.status_code == 201
    body = response.json()
    assert set(body) == PUBLIC_FIELDS | {"authorization"}
    assert body["state"] == "DRAFT"
    assert body["application_id"].startswith("app-")
    assert (body["provider_id"], body["channel_id"]) == ("fixture-bank", "direct")
    assert response.headers["x-correlation-id"]
    assert "idempotent-replayed" not in response.headers
    assert response.headers["cache-control"] == "no-store"  # the body carries credentials


def test_creation_returns_two_narrow_application_bound_tokens(client: TestClient) -> None:
    auth = create(client)["authorization"]
    assert set(auth) == {"onboarding", "status"}
    assert auth["onboarding"]["scope"] == "request_handoff submit_after_consent"
    assert auth["status"]["scope"] == "read_status"
    for token in auth.values():
        assert token["token_type"] == "Bearer"  # noqa: S105 - the OAuth token type name
        assert token["access_token"].startswith("tqa_")
        assert token["expires_at"]
    assert auth["onboarding"]["access_token"] != auth["status"]["access_token"]


def test_created_application_can_be_read_back_by_its_owner(client: TestClient) -> None:
    created = create(client)
    response = client.get(
        f"/v1/applications/{created['application_id']}", headers=status_auth(created)
    )
    assert response.status_code == 200
    assert response.json() == public(created)


def test_response_never_exposes_internal_fields(client: TestClient) -> None:
    created = create(client)
    text = client.get(
        f"/v1/applications/{created['application_id']}", headers=status_auth(created)
    ).text
    for internal in (
        "customer_ref",
        "opaque-id",
        "agent_actor_id",
        "agent-a",
        "version",
        "resume",
        "audit",
        "mandate",
        "jti",
        "tqa_",  # a read never echoes a credential
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
        {"scopes": ["submit_after_consent", "create_draft"]},  # no self-granted scopes
        {"authorization": {"scope": "read_status"}},
    ],
)
def test_agent_cannot_smuggle_state_identity_or_scope_fields_into_the_request(
    client: TestClient, extra: dict[str, object]
) -> None:
    response = client.post("/v1/applications", json={**BODY, **extra}, headers=headers(client))
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
        client.post("/v1/applications", json={**BODY, **bad}, headers=headers(client)).status_code
        == 422
    )


def test_missing_field_is_rejected(client: TestClient) -> None:
    body = {k: v for k, v in BODY.items() if k != "customer_ref"}
    assert client.post("/v1/applications", json=body, headers=headers(client)).status_code == 422


def test_idempotency_key_is_required_and_must_be_a_uuid(client: TestClient) -> None:
    auth = bootstrap_headers(client)
    assert client.post("/v1/applications", json=BODY, headers=auth).status_code == 422
    bad = {**auth, "Idempotency-Key": "not-a-uuid"}
    assert client.post("/v1/applications", json=BODY, headers=bad).status_code == 422


# --- idempotency ------------------------------------------------------------


def test_same_actor_key_and_body_returns_the_same_application(client: TestClient) -> None:
    h = headers(client, key=str(uuid.uuid4()))
    first = client.post("/v1/applications", json=BODY, headers=h)
    second = client.post("/v1/applications", json=BODY, headers=h)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["idempotent-replayed"] == "true"
    assert public(second.json()) == public(first.json())
    assert second.json()["authorization"] is None  # a replay never mints credentials


def test_same_key_with_a_different_body_returns_409(client: TestClient) -> None:
    h = headers(client)
    client.post("/v1/applications", json=BODY, headers=h)
    response = client.post("/v1/applications", json={**BODY, "channel_id": "partner-1"}, headers=h)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_replay_reports_current_generic_state_not_a_stale_one(client: TestClient) -> None:
    h = headers(client)
    created = client.post("/v1/applications", json=BODY, headers=h).json()
    trusted_move(client, created["application_id"], (S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY))
    replay = client.post("/v1/applications", json=BODY, headers=h).json()
    assert replay["application_id"] == created["application_id"]
    assert replay["state"] == "AWAITING_CUSTOMER"


def test_actors_cannot_reuse_each_others_key_or_application(client: TestClient) -> None:
    key = str(uuid.uuid4())
    a = client.post("/v1/applications", json=BODY, headers=headers(client, "agent-a", key))
    b = client.post("/v1/applications", json=BODY, headers=headers(client, "agent-b", key))
    assert (a.status_code, b.status_code) == (201, 201)  # b did NOT get a's application
    assert a.json()["application_id"] != b.json()["application_id"]


# --- actor scoping ------------------------------------------------------------


def test_another_actor_cannot_read_the_application_and_cannot_tell_it_exists(
    client: TestClient,
) -> None:
    created = create(client, "agent-a")
    intruder = status_auth(create(client, "agent-b"))  # a VALID read token - for its own app
    foreign = client.get(f"/v1/applications/{created['application_id']}", headers=intruder)
    missing = client.get("/v1/applications/app-does-not-exist", headers=intruder)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"] == "not_found"
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_cross_actor_read_attempt_is_audited(client: TestClient) -> None:
    created = create(client, "agent-a")
    intruder = status_auth(create(client, "agent-b"))
    client.get(f"/v1/applications/{created['application_id']}", headers=intruder)
    events = service_for(client).audit_trail(created["application_id"])
    assert [(e.event_type, e.actor_id) for e in events] == [
        ("application.created", "agent-a"),
        ("application.access_denied", "agent-b"),
    ]
    assert events[1].details["reason"] == "not_owner"


def test_audit_correlation_id_matches_the_http_response_header(client: TestClient) -> None:
    response = client.post(
        "/v1/applications", json=BODY, headers={**headers(client), "X-Correlation-ID": "req-42"}
    )
    events = service_for(client).audit_trail(response.json()["application_id"])
    assert response.headers["x-correlation-id"] == events[0].correlation_id == "req-42"


def test_internal_states_are_reported_coarsely(client: TestClient) -> None:
    created = create(client)
    app_id = created["application_id"]

    def state() -> str:
        return client.get(f"/v1/applications/{app_id}", headers=status_auth(created)).json()[
            "state"
        ]

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
        not in client.get(f"/v1/applications/{app_id}", headers=status_auth(created)).text
    )


# --- no valid bearer token, no access -----------------------------------------------------


@pytest.mark.parametrize("method", ["post", "get"])
def test_requests_without_a_bearer_token_get_401(client: TestClient, method: str) -> None:
    if method == "post":
        response = client.post(
            "/v1/applications", json=BODY, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
    else:
        response = client.get("/v1/applications/app-1")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == 'Bearer realm="trusteq-agent-api"'


@pytest.mark.parametrize(
    "auth",
    [
        {
            "X-Dev-Actor-Id": "agent-a"
        },  # the Phase 02 dev header is gone: an agent cannot name itself
        {"Authorization": "Basic YWdlbnQtYTpzZWNyZXQ="},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not a token; DROP TABLE"},
    ],
)
def test_self_asserted_or_malformed_credentials_get_401(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post(
        "/v1/applications", json=BODY, headers={**auth, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"authentication_required", "invalid_token"}
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM applications").scalar_one() == 0


def test_a_forged_well_formed_token_gets_invalid_token(client: TestClient) -> None:
    response = client.get("/v1/applications/app-1", headers=bearer(FORGED_TOKEN))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_in_production_no_agent_can_get_in(db_url: str) -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION, database_url=db_url)
    assert settings.dev_agent_bootstrap_enabled and not settings.dev_agent_bootstrap_active
    with TestClient(create_app(settings)) as prod_client:
        factory = prod_client.app.state.session_factory  # type: ignore[attr-defined]
        issuer = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory),
            bootstrap_enabled=settings.dev_agent_bootstrap_active,
        )
        with pytest.raises(ServiceDisabledError):  # the trusted bootstrap refuses in production
            issuer.issue_bootstrap(agent_actor_id="agent-a", customer_id="c", correlation_id="c")
        for auth in ({"X-Dev-Actor-Id": "agent-a"}, bearer(FORGED_TOKEN), {}):
            post = prod_client.post(
                "/v1/applications",
                json=BODY,
                headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
            )
            assert post.status_code == 401
            assert prod_client.get("/v1/applications/app-1", headers=auth).status_code == 401
        assert prod_client.get("/health").status_code == 200  # health stays public
        assert prod_client.get("/v1/products").status_code == 200  # so does discovery


def test_dev_bootstrap_can_be_switched_off(db_url: str) -> None:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=db_url,
        dev_agent_bootstrap_enabled=False,
    )
    assert not settings.dev_agent_bootstrap_active
    with TestClient(create_app(settings)) as off_client:
        factory = off_client.app.state.session_factory  # type: ignore[attr-defined]
        issuer = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory),
            bootstrap_enabled=settings.dev_agent_bootstrap_active,
        )
        with pytest.raises(ServiceDisabledError):
            issuer.issue_bootstrap(agent_actor_id="agent-a", customer_id="c", correlation_id="c")


# --- the agent has no way to change state or mint authority through the API ---------------


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
    # ... and there is no token, mandate or scope endpoint of any kind.
    assert not any(word in p for p in paths for word in ("token", "mandate", "scope", "auth"))


@pytest.mark.parametrize(
    "path",
    ["/v1/token", "/v1/auth/token", "/oauth/token", "/v1/mandates", "/v1/applications/token"],
)
def test_there_is_no_token_issuance_endpoint(client: TestClient, path: str) -> None:
    created = create(client)
    for auth in (status_auth(created), bootstrap_headers(client), {}):
        response = client.post(
            path, json={"scope": "submit_after_consent create_draft"}, headers=auth
        )
        assert response.status_code in {401, 404, 405}, (path, response.status_code)
        assert "tqa_" not in response.text


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_application_resource_cannot_be_modified_or_deleted(
    client: TestClient, method: str
) -> None:
    created = create(client)
    response = client.request(
        method,
        f"/v1/applications/{created['application_id']}",
        json={"status": "OPENED"},
        headers=status_auth(created),
    )
    assert response.status_code == 405


@pytest.mark.parametrize("suffix", ["status", "transition", "state", "open", "opened"])
def test_there_is_no_state_changing_sub_resource(client: TestClient, suffix: str) -> None:
    created = create(client)
    onboarding = bearer(created["authorization"]["onboarding"]["access_token"])
    for method in ("post", "put", "patch"):
        response = client.request(
            method,
            f"/v1/applications/{created['application_id']}/{suffix}",
            json={"status": "OPENED"},
            headers=onboarding,
        )
        assert response.status_code in {404, 405}
    after = client.get(
        f"/v1/applications/{created['application_id']}", headers=status_auth(created)
    ).json()
    assert after["state"] == "DRAFT"
