"""Bearer tokens held by the agent's tool RUNTIME (trust zone T0b) - never by the model.

The scripted agent (or, later, an LLM) only decides WHICH tool to call with which arguments;
the gateway attaches the matching bearer token. Tokens never appear in tool results or agent
run results, so a model - or a prompt injection inside a catalog text - can neither read, leak
nor swap them. The bank side verifies every token anyway; this store just keeps the agent's
own credentials apart from what it "thinks" with.

* ``bootstrap``: the narrow starting token from the trusted bootstrap (``create_draft``).
* per application: the ``onboarding`` token (``request_handoff``, ``submit_after_consent``)
  and the read-only ``status`` token, both returned by ``POST /v1/applications``.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ApplicationTokens:
    onboarding: str
    status: str

    def __repr__(self) -> str:
        return "ApplicationTokens(<hidden>)"


class AgentCredentials:
    def __init__(self, bootstrap_token: str | None = None) -> None:
        self._bootstrap = bootstrap_token
        self._applications: dict[str, ApplicationTokens] = {}

    @property
    def bootstrap(self) -> str | None:
        return self._bootstrap

    def use_bootstrap(self, token: str | None) -> None:
        """Install the starting token handed over by the trusted bootstrap."""
        self._bootstrap = token

    def remember(self, application_id: str, tokens: ApplicationTokens) -> None:
        self._applications[application_id] = tokens

    def for_application(self, application_id: str) -> ApplicationTokens | None:
        return self._applications.get(application_id)

    def __repr__(self) -> str:
        return f"AgentCredentials(applications={sorted(self._applications)}, tokens=<hidden>)"
