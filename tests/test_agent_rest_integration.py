"""The scripted agent talks to the real REST application through the public API contract."""

import ast
import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolName,
    AgentVisibleStatus,
)
from app.integrations.rest_gateway import RestAgentToolGateway
from tests.conftest import bootstrap_token

INTENT = OpenDepotIntent(
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="opaque-id",
)
GATEWAY_FILE = Path(__file__).resolve().parent.parent / "app" / "integrations" / "rest_gateway.py"
FAKE_BOOTSTRAP = "tqa_" + "B" * 43  # noqa: S105 - placeholder for transport-level tests


def credentials_for(client: TestClient, actor_id: str | None = "agent-a") -> AgentCredentials:
    """The agent runtime's token store, primed by the TRUSTED bootstrap (None: no token)."""
    return AgentCredentials(bootstrap_token(client, actor_id) if actor_id else None)


def agent_for(
    client: TestClient,
    actor_id: str | None = "agent-a",
    credentials: AgentCredentials | None = None,
) -> ScriptedCustomerAgent:
    # A TestClient *is* an httpx.Client: requests really traverse the ASGI app, with routing,
    # validation, dependencies and error handlers - exactly as an external HTTP client would.
    store = credentials or credentials_for(client, actor_id)
    return ScriptedCustomerAgent(RestAgentToolGateway(client, credentials=store))


def application_events(client: TestClient, application_id: str) -> int:
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        return connection.execute(
            text("SELECT COUNT(*) FROM audit_events WHERE application_id = :a"),
            {"a": application_id},
        ).scalar_one()


def rows(client: TestClient, table: str) -> int:
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        return connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # noqa: S608


def test_agent_creates_a_draft_and_reads_its_generic_status_via_rest(client: TestClient) -> None:
    result = agent_for(client).draft_application(INTENT)

    assert result.stopped_at is None
    assert [s.tool for s in result.steps] == [
        AgentToolName.CREATE_DRAFT,
        AgentToolName.GET_GENERIC_STATUS,
    ]
    assert result.last_status is AgentVisibleStatus.OK
    assert result.steps[1].data["state"] == "DRAFT"
    # The application really exists in the database, owned by the agent's actor id.
    app_id = result.steps[0].data["application_id"]
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        owner, status = connection.execute(
            text("SELECT agent_actor_id, status FROM applications WHERE application_id = :id"),
            {"id": app_id},
        ).one()
    assert (owner, status) == ("agent-a", "DRAFT")
    assert application_events(client, app_id) == 1  # one business event: the creation


def test_retrying_the_same_intent_does_not_create_a_second_application(client: TestClient) -> None:
    agent = agent_for(client)
    first = agent.draft_application(INTENT)
    second = agent.draft_application(INTENT)  # same deterministic Idempotency-Key and body
    assert second.steps[0].data["application_id"] == first.steps[0].data["application_id"]
    assert second.stopped_at is None  # the runtime kept the tokens from the first response
    assert rows(client, "applications") == 1


def test_a_different_agent_actor_gets_its_own_application(client: TestClient) -> None:
    a = agent_for(client, "agent-a").draft_application(INTENT)
    b = agent_for(client, "agent-b").draft_application(INTENT)
    assert a.steps[0].data["application_id"] != b.steps[0].data["application_id"]
    assert rows(client, "applications") == 2


def test_agent_without_credentials_is_denied_and_stops(client: TestClient) -> None:
    result = agent_for(client, actor_id=None).draft_application(INTENT)
    assert result.stopped_at is AgentToolName.CREATE_DRAFT
    assert result.last_status is AgentVisibleStatus.DENIED
    assert rows(client, "applications") == 0


def test_agent_cannot_read_another_agents_application(client: TestClient) -> None:
    app_id = agent_for(client, "agent-a").draft_application(INTENT).steps[0].data["application_id"]
    store = credentials_for(client, "agent-b")
    own = agent_for(client, credentials=store).draft_application(INTENT).application_id or ""
    intruder = RestAgentToolGateway(client, credentials=store)
    for tokens in (None, store.for_application(own)):  # no token / its own VALID token
        if tokens is not None:
            store.remember(app_id, tokens)  # a malicious runtime swaps in its other token
        result = intruder.execute(
            AgentToolCall(
                tool=AgentToolName.GET_GENERIC_STATUS, arguments={"application_id": app_id}
            )
        )
        assert result.status is AgentVisibleStatus.DENIED
        assert result.data == {}  # nothing about the application leaks


def test_the_agent_cannot_express_a_state_change(client: TestClient) -> None:
    gateway = RestAgentToolGateway(client, credentials=credentials_for(client))
    # The tool schema rejects extra fields before any request is made...
    with pytest.raises(ValueError, match="extra"):
        AgentToolCall(
            tool=AgentToolName.GET_GENERIC_STATUS,
            arguments={"application_id": "app-1", "status": "OPENED"},
        )
    # ...and the "request" tools go through the real endpoints, which refuse unknown ids.
    for tool in (AgentToolName.REQUEST_HANDOFF, AgentToolName.SUBMIT_AFTER_CONSENT):
        result = gateway.execute(AgentToolCall(tool=tool, arguments={"application_id": "app-nope"}))
        assert result.status is AgentVisibleStatus.DENIED
        assert result.data == {}


def test_agent_run_to_handoff_over_rest_stops_at_the_customer(client: TestClient) -> None:
    result = agent_for(client).open_depot(INTENT)
    assert result.stopped_at is None and result.handoff_requested is True
    assert result.final_state == "AWAITING_CUSTOMER"
    assert result.last_status is AgentVisibleStatus.ACTION_REQUIRED
    assert result.confirmation_url and "/customer/handoffs/" in result.confirmation_url
    assert rows(client, "applications") == 1 and rows(client, "handoffs") == 1


def test_agent_cannot_get_past_the_customer_by_submitting_early(client: TestClient) -> None:
    store = credentials_for(client)
    agent = agent_for(client, credentials=store)
    started = agent.open_depot(INTENT)
    resumed = agent.continue_after_customer(started.application_id)
    assert resumed.stop_reason == "waiting_for_customer"  # it does not even try
    # Even a forced submit WITH the right onboarding token is refused with a generic denial.
    forced = RestAgentToolGateway(client, credentials=store).execute(
        AgentToolCall(
            tool=AgentToolName.SUBMIT_AFTER_CONSENT,
            arguments={"application_id": started.application_id},
        )
    )
    assert forced.status is AgentVisibleStatus.DENIED and forced.data == {}
    assert agent.read_status(started.application_id).final_state == "AWAITING_CUSTOMER"


def test_agent_refuses_an_unverified_provider_before_creating_anything(client: TestClient) -> None:
    rogue = INTENT.model_copy(update={"provider_id": "rogue-bank"})
    result = agent_for(client).open_depot(rogue)
    assert result.stopped_at is AgentToolName.DISCOVER_PROVIDER
    assert result.last_status is AgentVisibleStatus.DENIED
    assert rows(client, "applications") == 0


# --- transport-level behaviour (httpx.MockTransport) ----------------------------------------


def mock_gateway(handler, credentials: AgentCredentials | None = None) -> RestAgentToolGateway:  # noqa: ANN001
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://bank.test")
    return RestAgentToolGateway(client, credentials=credentials or AgentCredentials(FAKE_BOOTSTRAP))


def test_server_errors_and_outages_become_unavailable_without_details() -> None:
    call = AgentToolCall(
        tool=AgentToolName.GET_GENERIC_STATUS, arguments={"application_id": "app-1"}
    )
    boom = mock_gateway(lambda request: httpx.Response(500, text="Traceback: db password=hunter2"))
    result = boom.execute(call)
    assert result.status is AgentVisibleStatus.UNAVAILABLE
    assert "hunter2" not in result.model_dump_json()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert mock_gateway(down).execute(call).status is AgentVisibleStatus.UNAVAILABLE


def test_garbage_success_responses_are_not_trusted() -> None:
    call = AgentToolCall(
        tool=AgentToolName.GET_GENERIC_STATUS, arguments={"application_id": "app-1"}
    )
    for body in ({"state": "OPENED"}, {"application_id": "app-1", "state": "NOT_A_STATE"}, []):
        gateway = mock_gateway(lambda request, b=body: httpx.Response(200, json=b))
        assert gateway.execute(call).status is AgentVisibleStatus.UNAVAILABLE


def test_error_bodies_are_never_forwarded_to_the_agent() -> None:
    gateway = mock_gateway(
        lambda request: httpx.Response(
            409, json={"error": {"message": "internal compliance detail"}}
        )
    )
    result = gateway.execute(
        AgentToolCall(tool=AgentToolName.GET_GENERIC_STATUS, arguments={"application_id": "app-1"})
    )
    assert result.status is AgentVisibleStatus.DENIED
    assert "compliance" not in result.model_dump_json()


def test_gateway_sends_the_idempotency_key_and_the_right_bearer_token() -> None:
    seen: list[httpx.Request] = []
    onboarding, status = "tqa_" + "O" * 43, "tqa_" + "S" * 43

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "application_id": "app-1",
                "state": "DRAFT",
                "authorization": {
                    "onboarding": {"access_token": onboarding, "token_type": "Bearer"},
                    "status": {"access_token": status, "token_type": "Bearer"},
                },
            },
        )

    result = ScriptedCustomerAgent(mock_gateway(handler)).draft_application(INTENT)

    post, get = seen
    assert (post.method, post.url.path) == ("POST", "/v1/applications")
    assert uuid.UUID(post.headers["idempotency-key"])  # a real UUID, as the API requires
    assert json.loads(post.content) == {
        "provider_id": "fixture-bank",
        "product_id": "depot-basic",
        "channel_id": "direct",
        "customer_ref": "opaque-id",
    }  # the key travels in the header, not in the body
    assert (get.method, get.url.path) == ("GET", "/v1/applications/app-1")
    assert post.headers["authorization"] == f"Bearer {FAKE_BOOTSTRAP}"  # create: bootstrap
    assert get.headers["authorization"] == f"Bearer {status}"  # read: least privilege
    assert "x-dev-actor-id" not in post.headers  # no self-asserted identity any more
    # The tokens stay in the runtime: no tool result (what a model would see) contains one.
    assert "tqa_" not in result.model_dump_json()


# --- boundary: the gateway is a pure HTTP client --------------------------------------------


def test_rest_gateway_cannot_reach_repositories_services_or_the_app() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(GATEWAY_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = ("app.repositories", "app.services", "app.api", "app.main", "sqlalchemy", "fastapi")
    offenders = {m for m in imported if m.startswith(forbidden)}
    assert not offenders, f"rest_gateway must stay a plain HTTP client, found {offenders}"
