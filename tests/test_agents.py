"""Scripted agent foundation and the T0b boundary (agent must not reach trusted internals)."""

import ast
from pathlib import Path

import pytest

from app.agents import (
    LlmAgentNotAvailableError,
    OpenDepotIntent,
    ScriptedCustomerAgent,
    build_customer_agent,
)
from app.core.config import Settings
from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolName,
    AgentToolResult,
    AgentVisibleStatus,
)

AGENTS_DIR = Path(__file__).resolve().parent.parent / "app" / "agents"
INTENT = OpenDepotIntent(
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="opaque-id",
)


class FakeGateway:
    """Stands in for the trusted bank side; records every call it receives.

    Answers like the verified catalog (discovery data) so the agent's own validation of
    discovered values is exercised too. `state` is what generic-status reports.
    """

    def __init__(
        self,
        fail_on: AgentToolName | None = None,
        *,
        state: str = "AWAITING_CUSTOMER",
        offered_channels: tuple[str, ...] = ("direct", "partner-1"),
        offered_products: tuple[str, ...] = ("depot-basic",),
    ) -> None:
        self.calls: list[AgentToolCall] = []
        self._fail_on = fail_on
        self.state = state
        self._channels = list(offered_channels)
        self._products = list(offered_products)

    def _data(self, call: AgentToolCall) -> dict[str, object]:
        match call.tool:
            case AgentToolName.DISCOVER_PROVIDER:
                return {
                    "provider_id": call.arguments["provider_id"],
                    "product_ids": self._products,
                    "channel_ids": self._channels,
                }
            case AgentToolName.GET_OFFICIAL_PRODUCT:
                return {
                    "product_id": call.arguments["product_id"],
                    "channel_ids": self._channels,
                    "document_version": "1.0",
                }
            case AgentToolName.CREATE_DRAFT:
                return {"application_id": "app-1", "state": "DRAFT"}
            case AgentToolName.REQUEST_HANDOFF:
                return {"confirmation_url": "http://bank.test/customer/handoffs/h-1?token=x"}
            case _:
                return {"application_id": "app-1", "state": self.state}

    def execute(self, call: AgentToolCall) -> AgentToolResult:
        self.calls.append(call)
        if call.tool is self._fail_on:
            return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.DENIED)
        status = {
            "AWAITING_CUSTOMER": AgentVisibleStatus.ACTION_REQUIRED,
            "IN_PROGRESS": AgentVisibleStatus.IN_PROGRESS,
            "OPENED": AgentVisibleStatus.OK,
        }.get(self.state, AgentVisibleStatus.OK)
        if call.tool not in {AgentToolName.GET_GENERIC_STATUS, AgentToolName.SUBMIT_AFTER_CONSENT}:
            status = AgentVisibleStatus.OK
        return AgentToolResult(tool=call.tool, status=status, data=self._data(call))


def test_scripted_agent_runs_the_fixed_tool_sequence_without_any_api_key() -> None:
    gateway = FakeGateway()
    result = ScriptedCustomerAgent(gateway).open_depot(INTENT)

    assert [c.tool for c in gateway.calls] == [
        AgentToolName.DISCOVER_PROVIDER,
        AgentToolName.GET_OFFICIAL_PRODUCT,
        AgentToolName.CREATE_DRAFT,
        AgentToolName.REQUEST_HANDOFF,
        AgentToolName.GET_GENERIC_STATUS,
    ]
    assert gateway.calls[3].arguments == {"application_id": "app-1"}
    # The agent reports the server's status verbatim - it never claims a depot was opened.
    assert result.last_status is AgentVisibleStatus.ACTION_REQUIRED
    assert result.handoff_requested is True


def test_idempotency_key_is_stable_for_a_repeated_intent() -> None:
    first, second = FakeGateway(), FakeGateway()
    ScriptedCustomerAgent(first).open_depot(INTENT)
    ScriptedCustomerAgent(second).open_depot(INTENT)
    assert (
        first.calls[2].arguments["idempotency_key"] == second.calls[2].arguments["idempotency_key"]
    )


@pytest.mark.parametrize(
    "failing_tool",
    [
        AgentToolName.DISCOVER_PROVIDER,
        AgentToolName.GET_OFFICIAL_PRODUCT,
        AgentToolName.CREATE_DRAFT,
        AgentToolName.REQUEST_HANDOFF,
    ],
)
def test_agent_stops_at_the_first_failed_step(failing_tool: AgentToolName) -> None:
    gateway = FakeGateway(fail_on=failing_tool)
    result = ScriptedCustomerAgent(gateway).open_depot(INTENT)

    assert result.stopped_at is failing_tool
    assert result.last_status is AgentVisibleStatus.DENIED
    assert result.handoff_requested is False
    assert gateway.calls[-1].tool is failing_tool  # nothing was attempted afterwards


def test_agent_prepares_the_draft_only_from_discovered_values() -> None:
    gateway = FakeGateway()
    ScriptedCustomerAgent(gateway).open_depot(INTENT)
    draft = gateway.calls[2].arguments
    assert (draft["provider_id"], draft["product_id"], draft["channel_id"]) == (
        "fixture-bank",
        "depot-basic",
        "direct",
    )


def test_agent_refuses_a_product_that_discovery_does_not_offer() -> None:
    gateway = FakeGateway(offered_products=("something-else",))
    result = ScriptedCustomerAgent(gateway).open_depot(INTENT)
    assert (result.stopped_at, result.stop_reason) == (
        AgentToolName.DISCOVER_PROVIDER,
        "product_not_offered",
    )
    assert [c.tool for c in gateway.calls] == [AgentToolName.DISCOVER_PROVIDER]


def test_agent_refuses_a_channel_that_the_product_does_not_offer() -> None:
    gateway = FakeGateway(offered_channels=("partner-1",))
    result = ScriptedCustomerAgent(gateway).open_depot(INTENT)
    assert (result.stopped_at, result.stop_reason) == (
        AgentToolName.GET_OFFICIAL_PRODUCT,
        "channel_not_offered",
    )
    assert AgentToolName.CREATE_DRAFT not in {c.tool for c in gateway.calls}


def test_agent_exposes_the_confirmation_pointer_but_not_a_way_to_use_it() -> None:
    result = ScriptedCustomerAgent(FakeGateway()).open_depot(INTENT)
    assert result.confirmation_url == "http://bank.test/customer/handoffs/h-1?token=x"
    assert result.application_id == "app-1"
    tools = {tool.value for tool in AgentToolName}
    assert not any(
        "confirm" in t or "approve" in t or "open" in t.replace("opening", "") for t in tools
    )


def test_agent_does_not_submit_while_the_customer_has_not_confirmed() -> None:
    gateway = FakeGateway(state="AWAITING_CUSTOMER")
    result = ScriptedCustomerAgent(gateway).continue_after_customer("app-1")
    assert (result.stopped_at, result.stop_reason) == (
        AgentToolName.GET_GENERIC_STATUS,
        "waiting_for_customer",
    )
    assert AgentToolName.SUBMIT_AFTER_CONSENT not in {c.tool for c in gateway.calls}


def test_agent_submits_after_the_customer_and_reports_the_servers_state() -> None:
    gateway = FakeGateway(state="IN_PROGRESS")
    result = ScriptedCustomerAgent(gateway).continue_after_customer("app-1")
    assert [c.tool for c in gateway.calls] == [
        AgentToolName.GET_GENERIC_STATUS,
        AgentToolName.SUBMIT_AFTER_CONSENT,
        AgentToolName.GET_GENERIC_STATUS,
    ]
    assert result.stopped_at is None and result.final_state == "IN_PROGRESS"


def test_agent_stops_when_the_backend_refuses_the_submit() -> None:
    gateway = FakeGateway(AgentToolName.SUBMIT_AFTER_CONSENT, state="IN_PROGRESS")
    result = ScriptedCustomerAgent(gateway).continue_after_customer("app-1")
    assert result.stopped_at is AgentToolName.SUBMIT_AFTER_CONSENT
    assert result.last_status is AgentVisibleStatus.DENIED


def test_agent_reports_opened_only_when_the_server_says_so() -> None:
    assert (
        ScriptedCustomerAgent(FakeGateway(state="IN_PROGRESS")).read_status("app-1").final_state
        == "IN_PROGRESS"
    )
    assert (
        ScriptedCustomerAgent(FakeGateway(state="OPENED")).read_status("app-1").final_state
        == "OPENED"
    )


def test_factory_returns_scripted_agent_by_default() -> None:
    agent = build_customer_agent(Settings(_env_file=None), FakeGateway())
    assert isinstance(agent, ScriptedCustomerAgent)
    assert agent.mode == "scripted"


def test_llm_mode_refuses_an_unimplemented_provider() -> None:
    settings = Settings(_env_file=None, ai_execution_mode="llm", enable_llm=True, llm_provider="x")  # type: ignore[arg-type]
    with pytest.raises(LlmAgentNotAvailableError, match="anthropic"):
        build_customer_agent(settings, FakeGateway())


ALLOWED_APP_IMPORTS = {"app.agents", "app.integrations.agent_tools", "app.core.config"}


def _imported_app_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names if a.name.startswith("app"))
    return modules


def test_agent_package_only_depends_on_the_tool_gateway_contract() -> None:
    files = list(AGENTS_DIR.glob("*.py"))
    assert files
    for path in files:
        for module in _imported_app_modules(path):
            allowed = module in ALLOWED_APP_IMPORTS or module.startswith("app.agents.")
            assert allowed, (
                f"{path.name} imports {module}: agent (T0b) must stay behind the gateway"
            )
