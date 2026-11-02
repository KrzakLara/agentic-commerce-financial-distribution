"""Agent-facing contract. An LLM-backed agent can later replace the scripted one unchanged."""

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.agent_tools import AgentToolName, AgentToolResult, AgentVisibleStatus


class OpenDepotIntent(BaseModel):
    """What the customer asked for. Opaque ids only; no personal data.

    The agent checks these against what discovery actually offers, and the bank side
    re-validates every value when the tool call arrives.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1, max_length=64)
    product_id: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    customer_ref: str = Field(min_length=1, max_length=64)


class AgentRunResult(BaseModel):
    """What happened. Every status/state comes from the server; the agent never invents success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[AgentToolResult, ...]
    stopped_at: AgentToolName | None = None
    last_status: AgentVisibleStatus | None = None
    # Set when the agent itself decided to stop (not a server refusal), e.g. "channel_not_offered".
    stop_reason: str | None = None

    def _data(self, tool: AgentToolName, key: str) -> Any:
        for step in self.steps:
            if step.tool is tool and key in step.data:
                return step.data[key]
        return None

    @property
    def application_id(self) -> str | None:
        return self._data(AgentToolName.CREATE_DRAFT, "application_id")

    @property
    def confirmation_url(self) -> str | None:
        """The pointer the agent hands to the customer. It authorizes nothing by itself."""
        return self._data(AgentToolName.REQUEST_HANDOFF, "confirmation_url")

    @property
    def final_state(self) -> str | None:
        """The last coarse state the SERVER reported (e.g. IN_PROGRESS, OPENED), if any."""
        for step in reversed(self.steps):
            if "state" in step.data:
                return str(step.data["state"])
        return None

    @property
    def handoff_requested(self) -> bool:
        """True only if the script ran without being stopped and the handoff step succeeded."""
        return self.stopped_at is None and any(
            step.tool is AgentToolName.REQUEST_HANDOFF for step in self.steps
        )


class CustomerAgent(Protocol):
    mode: str

    def draft_application(self, intent: OpenDepotIntent) -> AgentRunResult: ...

    def open_depot(self, intent: OpenDepotIntent) -> AgentRunResult: ...

    def continue_after_customer(self, application_id: str) -> AgentRunResult: ...

    def read_status(self, application_id: str) -> AgentRunResult: ...
