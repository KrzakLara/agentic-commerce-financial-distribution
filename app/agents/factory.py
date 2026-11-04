"""Selects the agent implementation from configuration (keeps the LLM replaceable)."""

from app.agents.base import CustomerAgent
from app.agents.scripted import ScriptedCustomerAgent
from app.core.config import AIExecutionMode, Settings
from app.integrations.agent_tools import AgentToolGateway


class LlmAgentNotAvailableError(RuntimeError):
    """LLM mode was selected but cannot run. Fails closed: never a silent switch to scripted."""


def build_customer_agent(settings: Settings, gateway: AgentToolGateway) -> CustomerAgent:
    if settings.ai_execution_mode is AIExecutionMode.SCRIPTED:
        return ScriptedCustomerAgent(gateway)
    if settings.llm_provider != "anthropic":
        raise LlmAgentNotAvailableError("Only the LLM provider 'anthropic' is implemented.")
    if settings.llm_api_key is None:
        raise LlmAgentNotAvailableError("LLM mode needs ANTHROPIC_API_KEY (environment or .env).")
    try:  # imported here on purpose: scripted mode must work without the optional SDK
        from app.agents.llm import LlmCustomerAgent
    except ImportError as error:
        raise LlmAgentNotAvailableError(
            'LLM mode needs the SDK: pip install -e ".[llm]"'
        ) from error
    return LlmCustomerAgent.from_settings(settings, gateway)
