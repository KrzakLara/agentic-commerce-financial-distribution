"""Test harness for the Phase 02 end-to-end flow.

Three separate HTTP clients - agent, customer, depot provider - against ONE app instance, so no
cookie, header or credential can leak from one actor to another. The clock is controllable.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.container import AppContainer
from app.domain.audit import AuditEvent
from app.integrations.provider_callback import SignedCallback, build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.onboarding_service import OnboardingService

PROVIDER_SECRET = "test-only-mock-provider-secret"  # noqa: S105 - a MOCK credential for tests
CALLBACK_PATH = "/internal/depot-provider/confirmations"
INTENT = OpenDepotIntent(
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="cust-1",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def customer_headers(customer_id: str) -> dict[str, str]:
    return {"X-Dev-Customer-Id": customer_id}


def agent_headers(actor_id: str = "agent-a") -> dict[str, str]:
    return {"X-Dev-Actor-Id": actor_id}


@dataclass
class Started:
    """What the agent got back after open_depot - exactly what a real agent would hold."""

    application_id: str
    handoff_id: str
    nonce: str
    action_digest: str
    url: str


@dataclass
class Flow:
    app: FastAPI
    agent_http: TestClient
    customer_http: TestClient
    provider_http: TestClient
    clock: FakeClock
    container: AppContainer
    engine_getter: Callable[[], object]
    _services: list[ApplicationService] = field(default_factory=list)

    # --- actors ------------------------------------------------------------

    def agent(self, actor_id: str = "agent-a") -> ScriptedCustomerAgent:
        gateway = RestAgentToolGateway(self.agent_http, auth_headers=agent_headers(actor_id))
        return ScriptedCustomerAgent(gateway)

    def start(self, intent: OpenDepotIntent = INTENT, actor_id: str = "agent-a") -> Started:
        result = self.agent(actor_id).open_depot(intent)
        assert result.stopped_at is None, result
        url = result.confirmation_url or ""
        match = re.search(r"/customer/handoffs/([^?]+)\?token=(.+)$", url)
        assert match, url
        handoff_step = next(s for s in result.steps if s.tool.value == "request_handoff")
        return Started(
            application_id=result.application_id or "",
            handoff_id=match.group(1),
            nonce=match.group(2),
            action_digest=handoff_step.data["action_digest"],
            url=url,
        )

    def customer_confirm(
        self,
        started: Started,
        customer_id: str = "cust-1",
        *,
        nonce: str | None = None,
        action_digest: str | None = None,
        client: TestClient | None = None,
    ):  # noqa: ANN201
        return (client or self.customer_http).post(
            f"/v1/customer/handoffs/{started.handoff_id}/confirm",
            json={
                "nonce": nonce or started.nonce,
                "action_digest": action_digest or started.action_digest,
            },
            headers=customer_headers(customer_id),
        )

    def provider_callback(
        self,
        application_id: str,
        *,
        provider_ref: str | None = None,
        confirmation_id: str | None = None,
        secret: str = PROVIDER_SECRET,
    ) -> SignedCallback:
        return build_signed_confirmation(
            secret.encode(),
            application_id=application_id,
            provider_ref=provider_ref or f"MOCK-DEPOT-{application_id}",
            confirmation_id=confirmation_id,
            now=self.clock().timestamp(),
        )

    def send_callback(self, callback: SignedCallback):  # noqa: ANN201
        return self.provider_http.post(
            CALLBACK_PATH, content=callback.body, headers=callback.headers
        )

    def provider_confirms(self, application_id: str, **kwargs: str):  # noqa: ANN003, ANN201
        return self.send_callback(self.provider_callback(application_id, **kwargs))

    def run_to_opened(self, actor_id: str = "agent-a") -> Started:
        started = self.start(actor_id=actor_id)
        assert self.customer_confirm(started).status_code == 200
        resumed = self.agent(actor_id).continue_after_customer(started.application_id)
        assert resumed.stopped_at is None, resumed
        assert self.provider_confirms(started.application_id).status_code == 200
        return started

    # --- inspection (read-only; tests may look at internals, actors never can) ---------------

    def service(self) -> ApplicationService:
        factory = self.app.state.session_factory
        return ApplicationService(lambda: SqlAlchemyUnitOfWork(factory), clock=self.clock)

    def onboarding(self) -> OnboardingService:
        """The trusted orchestration, for crash-recovery scenarios only (no actor can reach it)."""
        factory = self.app.state.session_factory
        return OnboardingService(
            lambda: SqlAlchemyUnitOfWork(factory),
            self.service(),
            catalog=self.container.catalog,
            kyc=self.container.kyc,
            compliance=self.container.compliance,
            contract=self.container.contract,
            depot=self.container.depot,
            clock=self.clock,
        )

    def audit(self, application_id: str) -> list[AuditEvent]:
        return self.service().audit_trail(application_id)

    def event_types(self, application_id: str) -> list[str]:
        return [e.event_type for e in self.audit(application_id)]

    def sql(self, statement: str, **params: object) -> list[tuple]:  # noqa: ANN401
        with self.app.state.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(statement), params)]

    def execute(self, statement: str, **params: object) -> None:
        """Run a WRITE statement as a database administrator would (tests only)."""
        with self.app.state.engine.begin() as connection:
            connection.execute(text(statement), params)

    def status_of(self, application_id: str) -> str:
        return self.sql(
            "SELECT status FROM applications WHERE application_id = :i", i=application_id
        )[0][0]

    def agent_state(self, application_id: str, actor_id: str = "agent-a") -> str:
        response = self.agent_http.get(
            f"/v1/applications/{application_id}", headers=agent_headers(actor_id)
        )
        return response.json()["state"]
