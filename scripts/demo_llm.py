"""Phase 05 demo: a REAL model completes a simulated onboarding through the existing bank API.

    python -m scripts.demo_llm

Needs ANTHROPIC_API_KEY (environment or the local, git-ignored .env). Without one it prints BLOCKED
and exits 3 before anything starts. The three offline demos need no key and are not affected.

The model only decides on tool calls and writes the answers. Everything else stays with the actors
of `scripts.demo_happy`: the real server (uvicorn) on a throw-away database, the bank's trusted
bootstrap, a separate customer who confirms, and the MOCK provider's signed confirmation, which is
the only path to OPENED. Every check below reads the SERVER's state, never the model's text. Never
prints a key, a bearer token or the one-time challenge. Exit code 0 only if every check holds.
"""

import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from app.agents.llm import (
    PINNED_MODEL,
    LlmCustomerAgent,
    LlmOutcome,
    LlmRunResult,
    LlmUsage,
    estimated_cost_usd,
)
from app.core.config import AIExecutionMode, Settings
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.agent_tools import AgentToolName
from app.integrations.provider_callback import build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway
from scripts.demo_happy import (
    CALLBACK_PATH,
    MIN_FREE_MB,
    Bank,
    DemoFailure,
    Server,
    Timeline,
    expect,
    free_port,
    mask,
    remove_tree,
)

AGENT_ID = "demo-llm-agent"
CUSTOMER_ID = "demo-llm-customer"
EXIT_BLOCKED = 3


def show(tl: Timeline, result: LlmRunResult) -> None:
    for call in result.calls:
        answer = call.status.value if call.status else f"rejected: {call.rejection}"
        tl.say("MODEL", f"proposed {call.tool} -> bank answered {answer}")
    print(f"           model's answer: {mask(result.final_text)}", flush=True)
    expect(result.outcome is LlmOutcome.COMPLETED, f"the run ended with {result.outcome.value}")


def run(settings: Settings) -> None:
    tl = Timeline()
    tmp = Path(tempfile.mkdtemp(prefix="trusteq_demo_"))
    free_mb = shutil.disk_usage(tmp).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        remove_tree(tmp)
        raise DemoFailure(f"only {free_mb} MB free on the temp drive; need {MIN_FREE_MB} MB")
    db_path = tmp / "demo.db"
    callback_key = secrets.token_hex(24)  # per-run MOCK callback key, never printed
    os.environ.pop("ANTHROPIC_API_KEY", None)  # the bank's server does not get the agent's key
    server = Server(tmp, db_path, callback_key, free_port())
    bank: Bank | None = None
    model = settings.llm_model or PINNED_MODEL
    print(f"TRUSTEQ - Phase 05 real LLM agent, model {model} (SYNTHETIC / MOCK data)")
    try:
        tl.heading("0. Start the real server; the bank grants the agent one narrow token")
        server.start()
        bank = Bank(db_path)
        boot = bank.bootstrap(AGENT_ID, CUSTOMER_ID)
        agent_http = httpx.Client(base_url=server.base_url, timeout=15)
        customer_http = httpx.Client(base_url=server.base_url, timeout=15)
        provider_http = httpx.Client(base_url=server.base_url, timeout=15)
        gateway = RestAgentToolGateway(agent_http, credentials=AgentCredentials(boot.token))
        agent = LlmCustomerAgent.from_settings(settings, gateway)
        tl.say("BANK", f"server up at {server.base_url}; scope create_draft for {CUSTOMER_ID}")

        tl.heading("1. Customer asks the agent to start; the model selects the tools")
        started = agent.run(
            "Please start opening the Basic Securities Account at Example Depot Bank for me, "
            "through direct sales.",
            customer_ref=CUSTOMER_ID,
        )
        show(tl, started)
        result = started.agent_result
        expect(result.handoff_requested, "the model did not reach the customer handoff")
        expect(result.final_state == "AWAITING_CUSTOMER", f"state is {result.final_state}")
        application_id = result.application_id or ""
        tl.say(
            "BANK", f"application {application_id} waits for the customer ({result.final_state})"
        )

        tl.heading("2. Customer confirms (SIMULATED sign-in); the model is not involved")
        match = re.search(r"/customer/handoffs/([^?]+)\?token=(.+)$", result.confirmation_url or "")
        expect(match is not None, "no confirmation link from the server")
        handoff = next(s for s in result.steps if s.tool is AgentToolName.REQUEST_HANDOFF)
        confirmed = customer_http.post(
            f"/v1/customer/handoffs/{match.group(1)}/confirm",  # type: ignore[union-attr]
            json={
                "nonce": match.group(2),  # type: ignore[union-attr]
                "action_digest": handoff.data["action_digest"],
            },
            headers={"X-Dev-Customer-Id": CUSTOMER_ID},
        )
        expect(confirmed.status_code == 200, f"customer confirmation: {confirmed.status_code}")
        tl.say("CUSTOMER", "confirmed the exact action; MOCK KYC, compliance and contract ran")

        tl.heading("3. Customer tells the agent; the bank's policy gate decides on the submit")
        resumed = agent.run(
            f"I have confirmed on the bank's page. Please continue application {application_id}."
        )
        show(tl, resumed)
        expect(
            resumed.agent_result.final_state == "IN_PROGRESS",
            f"state after the submit is {resumed.agent_result.final_state}",
        )
        tl.say("BANK", "handed over to the MOCK depot provider; the account is NOT open yet")

        tl.heading("4. MOCK depot provider: signed confirmation (the ONLY path to OPENED)")
        callback = build_signed_confirmation(
            callback_key.encode(),
            application_id=application_id,
            provider_ref=f"MOCK-DEPOT-{application_id}",
        )
        answer = provider_http.post(CALLBACK_PATH, content=callback.body, headers=callback.headers)
        expect(answer.status_code == 200, f"provider callback: {answer.status_code}")
        tl.say("MOCK PROVIDER", "signed 'account opened' confirmation accepted by the bank")

        tl.heading("5. The model reads the final status from the server")
        final = agent.run(f"Is my account open now? My application is {application_id}.")
        show(tl, final)
        expect(final.agent_result.final_state == "OPENED", "the server does not report OPENED")

        usage = LlmUsage(
            **{
                name: sum(getattr(r.usage, name) for r in (started, resumed, final))
                for name in LlmUsage.model_fields
            }
        )
        print(f"\nserved by: {started.served_model}; usage: {usage.model_dump()}")
        print(f"estimated cost: {estimated_cost_usd(usage, model)} USD (list prices)")
        print("\nRESULT: PASS - a real model used the existing tools; the bank decided every step.")
        print("Reminder: KYC, compliance, contract and the depot provider are MOCK; the customer")
        print("sign-in is simulated; agent authorization is a controlled DEV implementation.")
    finally:
        server.stop()
        if bank is not None:
            bank.close()
        remove_tree(tmp)


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    settings = Settings(
        ai_execution_mode=AIExecutionMode.LLM, enable_llm=True, llm_provider="anthropic"
    )
    if settings.llm_api_key is None:
        print("BLOCKED: no ANTHROPIC_API_KEY in the environment or .env; no call was made.")
        return EXIT_BLOCKED
    try:
        run(settings)
    except DemoFailure as failure:
        print(f"\nRESULT: FAIL - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
