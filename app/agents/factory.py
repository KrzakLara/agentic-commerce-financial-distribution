"""Selects the agent implementation from configuration (keeps the LLM replaceable)."""

from typing import Any

from app.agents.answer_proposer import AnswerProposer, DeterministicProposer, UnavailableProposer
from app.agents.base import CustomerAgent
from app.agents.scripted import ScriptedCustomerAgent
from app.core.config import AIExecutionMode, Settings
from app.integrations.agent_tools import AgentToolGateway


class LlmAgentNotAvailableError(RuntimeError):
    """LLM mode was selected but cannot run. Fails closed: never a silent switch to scripted."""


def answer_model_client(settings: Settings) -> Any:  # noqa: ANN401 - the SDK's client
    """The SDK client of the question answering, or None if the SDK is not installed."""
    try:  # imported here on purpose: scripted mode must work without the optional SDK
        from app.agents.llm import build_client
    except ImportError:
        return None
    return build_client(settings)


def build_answer_proposer(
    settings: Settings,
    gateway: AgentToolGateway,
    proposal_schema: dict[str, Any],
    client: Any = None,  # noqa: ANN401 - an SDK client, or a fake one in tests
) -> AnswerProposer:
    """Who proposes answers to customers' questions. Scripted needs no key and no SDK.

    In LLM mode a missing key or SDK gives a proposer that reports itself unavailable, so a question
    gets a fixed answer and the rest of the bank keeps working. It never answers from the rules.
    """
    if settings.ai_execution_mode is AIExecutionMode.SCRIPTED:
        return DeterministicProposer(gateway)
    if settings.llm_provider != "anthropic":
        return UnavailableProposer("provider_not_implemented")
    if client is None and settings.llm_api_key is None:
        return UnavailableProposer("no_api_key")
    try:  # imported here on purpose: scripted mode must work without the optional SDK
        from app.agents.llm_answer_proposer import build_llm_answer_proposer
    except ImportError:
        return UnavailableProposer("sdk_not_installed")
    return build_llm_answer_proposer(settings, gateway, proposal_schema, client)


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
