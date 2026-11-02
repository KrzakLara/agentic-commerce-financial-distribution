"""Selects the agent implementation from configuration (keeps the LLM replaceable)."""

from app.agents.base import CustomerAgent
from app.agents.scripted import ScriptedCustomerAgent
from app.core.config import AIExecutionMode, Settings
from app.integrations.agent_tools import AgentToolGateway


class LlmAgentNotAvailableError(NotImplementedError):
    """The optional LLM-backed agent is planned for Phase 05; Phase 00 has no LLM calls."""


def build_customer_agent(settings: Settings, gateway: AgentToolGateway) -> CustomerAgent:
    if settings.ai_execution_mode is AIExecutionMode.SCRIPTED:
        return ScriptedCustomerAgent(gateway)
    raise LlmAgentNotAvailableError(
        "LLM agent mode is not implemented yet (planned for Phase 05); use 'scripted'."
    )
