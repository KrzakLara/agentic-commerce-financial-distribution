"""Customer-side AI agent (trust zone T0b - ALWAYS untrusted).

Boundary rule: code in this package may talk to the bank side ONLY through the
`AgentToolGateway` port in `app.integrations.agent_tools`. It must never import
repositories, services, the database layer or compliance logic; `tests/test_agents.py`
enforces this. The agent proposes tool calls; the backend independently verifies actor,
scope, policy, state and payload.
"""

from app.agents.base import AgentRunResult, CustomerAgent, OpenDepotIntent
from app.agents.factory import LlmAgentNotAvailableError, build_customer_agent
from app.agents.scripted import ScriptedCustomerAgent

__all__ = [
    "AgentRunResult",
    "CustomerAgent",
    "LlmAgentNotAvailableError",
    "OpenDepotIntent",
    "ScriptedCustomerAgent",
    "build_customer_agent",
]
