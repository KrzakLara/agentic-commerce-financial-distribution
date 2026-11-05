"""Phase 03 demo: a complete SIMULATED onboarding through the real REST API, with scoped
agent authorization.

    python -m scripts.demo_happy [--keep] [--no-restart]

What it does
------------
* Creates a brand-new temporary SQLite database (never your project data) and starts the real
  FastAPI server (uvicorn) as a subprocess on a free local port.
* Plays four SEPARATE actors, each with its own HTTP client and credentials:
    AGENT      the scripted customer agent, through the public API only, with bearer tokens
    CUSTOMER   the simulated customer on the bank-controlled confirmation page (DEV sign-in)
    BANK       the trusted backend: issues the agent's first narrow grant (dev bootstrap,
               in-process - there is no HTTP endpoint for it) and verifies the database
    MOCK PROVIDER   the MOCK depot provider sending its signed confirmation
* Shows what the agent tries that MUST fail, then the happy path to OPENED, then that the
  agent's onboarding privileges are gone.
* Stops and restarts the server to prove the result is persisted.

Everything is synthetic and MOCK. The demo prints ids, scopes and states, but never a bearer
token, the one-time challenge, the callback key or any other credential. Exit code 0 only if
every check holds.
"""

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.api.customer_auth import API_COOKIE_PATH, API_SESSION_COOKIE
from app.core.config import Environment, Settings
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.agent_tools import AgentToolName
from app.integrations.provider_callback import build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway
from app.repositories.database import create_db_engine, create_session_factory
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import AuthorizationService, IssuedToken
from app.services.customer_session_service import csrf_token

ROOT = Path(__file__).resolve().parent.parent
CALLBACK_PATH = "/internal/depot-provider/confirmations"
AGENT_ID = "demo-agent-1"
CUSTOMER_ID = "demo-customer-1"
OTHER_AGENT_ID = "demo-agent-2"
OTHER_CUSTOMER_ID = "demo-customer-2"
MIN_FREE_MB = 25
FORGED = "tqa_" + "x" * 43  # well-formed, never issued


class DemoFailure(Exception):
    """A check of the demo did not hold."""


class Timeline:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def say(self, actor: str, text: str) -> None:
        print(f"{time.perf_counter() - self._t0:6.2f}s  {actor:<14} {text}", flush=True)

    def heading(self, text: str) -> None:
        print(f"\n== {text}", flush=True)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise DemoFailure(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    """The real application, started exactly like a developer would (uvicorn)."""

    def __init__(self, workdir: Path, db_path: Path, callback_key: str, port: int) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self._port = port
        self._workdir = workdir
        self._env = {k: v for k, v in os.environ.items() if not k.startswith("TRUSTEQ_")}
        self._env.update(
            {
                "TRUSTEQ_ENVIRONMENT": "development",
                "TRUSTEQ_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
                "TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET": callback_key,
                "TRUSTEQ_LOG_LEVEL": "WARNING",
                "PYTHONPATH": str(ROOT),
            }
        )
        self._proc: subprocess.Popen[bytes] | None = None
        self._log = workdir / "server.log"

    def start(self) -> None:
        # The child inherits its own copy of the log handle; closing ours lets Windows delete
        # the folder afterwards.
        with self._log.open("ab") as log:
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self._port),
                    "--log-level",
                    "warning",
                ],  # fmt: skip
                cwd=self._workdir,  # a neutral folder: no project .env is picked up
                env=self._env,
                stdout=log,
                stderr=log,
            )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                if httpx.get(f"{self.base_url}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.25)
        tail = self._log.read_text(errors="replace")[-800:]
        raise DemoFailure(f"server did not start. Log tail: {tail!r}")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class Bank:
    """The TRUSTED side's dev bootstrap: issues an agent's first grant straight into the bank's
    database, in-process - exactly what `python -m scripts.dev_auth bootstrap` does. No HTTP
    endpoint can do this, so no caller can mint itself an identity."""

    def __init__(self, db_path: Path) -> None:
        settings = Settings(_env_file=None, environment=Environment.DEVELOPMENT)
        self._engine = create_db_engine(f"sqlite:///{db_path.as_posix()}")
        factory = create_session_factory(self._engine)
        self.authorization = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory),
            bootstrap_enabled=settings.dev_agent_bootstrap_active,
            bootstrap_ttl_seconds=settings.agent_bootstrap_ttl_seconds,
        )

    def bootstrap(self, agent_id: str, customer_id: str) -> IssuedToken:
        return self.authorization.issue_bootstrap(
            agent_actor_id=agent_id, customer_id=customer_id, correlation_id="demo-bootstrap"
        )

    def close(self) -> None:
        self._engine.dispose()


def mask(url: str) -> str:
    return re.sub(r"token=[^&\s]+", "token=***", url)


def bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def code(response: httpx.Response) -> str:
    """'HTTP 403 insufficient_scope' - status and error code only, never a body detail."""
    try:
        error = response.json().get("error", {}).get("code", "")
    except ValueError:
        error = ""
    return f"HTTP {response.status_code} {error}".rstrip()


def sign_in(browser: httpx.Client, customer_id: str) -> None:
    """The DEV sign-in form, as a browser submits it: a server-side session in HttpOnly cookies."""
    handoff_id, token = "hnd-sign-in", "t" * 24
    form = browser.get(f"/customer/handoffs/{handoff_id}", params={"token": token})
    login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', form.text)
    if login_csrf is None:
        raise DemoFailure("the sign-in form has no CSRF token")
    response = browser.post(
        "/customer/dev-login",
        data={"customer_id": customer_id, "handoff_id": handoff_id, "token": token,
              "login_csrf": login_csrf.group(1)},
        follow_redirects=False,
    )  # fmt: skip
    expect(response.status_code == 303, f"dev sign-in failed: {response.status_code}")


def confirm_as_signed_in_customer(
    browser: httpx.Client, customer_id: str, handoff_id: str, nonce: str, action_digest: str
) -> httpx.Response:
    """The customer JSON ceremony: the customer's session plus its CSRF token for this handoff."""
    sign_in(browser, customer_id)
    secret = browser.cookies.get(API_SESSION_COOKIE, path=API_COOKIE_PATH) or ""
    return browser.post(
        f"/v1/customer/handoffs/{handoff_id}/confirm",
        json={"nonce": nonce, "action_digest": action_digest},
        headers={"X-CSRF-Token": csrf_token(secret, "confirm", handoff_id)},
    )


def read_db(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def run(keep: bool, restart: bool) -> None:  # noqa: C901 - a linear, narrated script
    tl = Timeline()
    tmp = Path(tempfile.mkdtemp(prefix="trusteq_demo_"))
    free_mb = shutil.disk_usage(tmp).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        remove_tree(tmp)
        raise DemoFailure(f"only {free_mb} MB free on the temp drive; need {MIN_FREE_MB} MB")
    db_path = tmp / "demo.db"
    callback_key = secrets.token_hex(24)  # per-run MOCK callback key, never printed
    server = Server(tmp, db_path, callback_key, free_port())
    bank: Bank | None = None

    print("TRUSTEQ - Phase 03 scoped agent authorization, simulated onboarding (SYNTHETIC / MOCK)")
    print(f"isolated demo database: {db_path}  ({free_mb} MB free)")
    try:
        tl.heading("0. Start the real server on a clean database")
        server.start()
        tl.say("BANK", f"server up at {server.base_url} (fresh schema, no data)")
        bank = Bank(db_path)

        agent_http = httpx.Client(base_url=server.base_url, timeout=15)
        customer_http = httpx.Client(base_url=server.base_url, timeout=15)
        provider_http = httpx.Client(base_url=server.base_url, timeout=15)
        intent = OpenDepotIntent(
            provider_id="fixture-bank", product_id="depot-basic", channel_id="direct",
            customer_ref=CUSTOMER_ID,
        )  # fmt: skip

        # --- 1. the bank grants the agent a narrow, single-use starting authorization -----------
        tl.heading("1. BANK (trusted dev bootstrap): the agent's first, narrow authorization")
        boot = bank.bootstrap(AGENT_ID, CUSTOMER_ID)
        credentials = AgentCredentials(boot.token)  # held by the agent RUNTIME, not the model
        agent = ScriptedCustomerAgent(RestAgentToolGateway(agent_http, credentials=credentials))
        tl.say(
            "BANK",
            f"bootstrap grant for {AGENT_ID} acting for {CUSTOMER_ID}: scope=create_draft, "
            f"ONE application, expires {boot.expires_at:%H:%M:%S} UTC (jti {boot.jti})",
        )
        tl.say(
            "BANK", "only a hash of the bearer token is stored; the token itself is never printed"
        )

        # --- 2. discovery + draft + handoff request (agent) ---------------------------------
        tl.heading(
            "2. Agent: discover the provider, read the product, create the draft, ask for approval"
        )
        started = agent.open_depot(intent)
        expect(
            started.stopped_at is None,
            f"agent run stopped: {started.stopped_at} {started.stop_reason}",
        )
        manifest, product = started.steps[0].data, started.steps[1].data
        tl.say("AGENT", f"discovered provider {manifest['provider_id']} - {manifest['legal_name']}")
        tl.say(
            "AGENT",
            f"product {product['product_id']} '{product['name']}', disclosure "
            f"{product['document_id']} v{product['document_version']} (public, no token needed)",
        )
        app_id = started.application_id or ""
        handoff_step = next(s for s in started.steps if s.tool is AgentToolName.REQUEST_HANDOFF)
        tokens = credentials.for_application(app_id)
        if tokens is None:
            raise DemoFailure("the bank did not return application-bound tokens")
        tl.say("AGENT", f"created application {app_id} (state DRAFT) via POST /v1/applications")
        tl.say(
            "BANK",
            "returned 2 tokens bound to this application: onboarding (request_handoff, "
            "submit_after_consent) and status (read_status)",
        )
        tl.say("AGENT", "tokens stay in the agent runtime; no tool result ever contains one")
        expect("tqa_" not in started.model_dump_json(), "a token leaked into the agent's results")
        tl.say("AGENT", f"requested customer handoff {handoff_step.data['handoff_id']}")
        tl.say("AGENT", f"link handed to the customer: {mask(started.confirmation_url or '')}")
        tl.say("AGENT", f"server reports generic state: {started.final_state}")
        expect(started.final_state == "AWAITING_CUSTOMER", "agent should now wait for the customer")

        # --- negative probes: what the agent must NOT be able to do ---------------------------
        tl.heading("3. Things the agent tries that must be refused")
        submit_url = f"/v1/applications/{app_id}/submit"
        early = agent_http.post(submit_url, headers=bearer(tokens.onboarding))
        tl.say("AGENT", f"submit before the customer confirmed -> {code(early)}")
        expect(early.status_code == 409, "early submit must be refused")
        match = re.search(
            r"/customer/handoffs/([^?]+)\?token=(.+)$", started.confirmation_url or ""
        )
        expect(match is not None, "no confirmation link")
        handoff_id, nonce = match.group(1), match.group(2)  # type: ignore[union-attr]
        own = agent_http.post(
            f"/v1/customer/handoffs/{handoff_id}/confirm",
            json={"nonce": nonce, "action_digest": handoff_step.data["action_digest"]},
            headers=bearer(tokens.onboarding),
        )
        tl.say("AGENT", f"confirm its OWN handoff with its bearer token (+ nonce) -> {code(own)}")
        expect(own.status_code == 401, "the agent must not be able to confirm")
        read_only = agent_http.post(submit_url, headers=bearer(tokens.status))
        tl.say("AGENT", f"submit with its read-only status token -> {code(read_only)}")
        expect(read_only.status_code == 403, "a read-only token must not submit")
        second = agent_http.post(
            "/v1/applications",
            json={**intent.model_dump(), "channel_id": "partner-1"},
            headers={**bearer(boot.token), "Idempotency-Key": str(uuid.uuid4())},
        )
        tl.say("AGENT", f"create a 2nd application with its used bootstrap -> {code(second)}")
        expect(second.status_code == 403, "a bootstrap grant must be single use")
        other_boot = bank.bootstrap(OTHER_AGENT_ID, OTHER_CUSTOMER_ID)
        other_credentials = AgentCredentials(other_boot.token)
        other = ScriptedCustomerAgent(
            RestAgentToolGateway(agent_http, credentials=other_credentials)
        ).draft_application(intent.model_copy(update={"customer_ref": OTHER_CUSTOMER_ID}))
        other_id = other.application_id or ""
        expect(other.stopped_at is None and other_id != app_id, "second agent setup failed")
        tl.say("BANK", f"(setup) another agent, {OTHER_AGENT_ID}, owns application {other_id}")
        foreign = agent_http.get(f"/v1/applications/{other_id}", headers=bearer(tokens.status))
        tl.say("AGENT", f"read the OTHER agent's application with its own token -> {code(foreign)}")
        expect(foreign.status_code == 404, "another application's data must not be readable")
        forged = agent_http.get(f"/v1/applications/{app_id}", headers=bearer(FORGED))
        tl.say("AGENT", f"use a made-up bearer token -> {code(forged)}")
        expect(forged.status_code == 401, "a forged token must be refused")
        legacy = agent_http.get(f"/v1/applications/{app_id}", headers={"X-Dev-Actor-Id": AGENT_ID})
        tl.say("AGENT", f"name itself via the old X-Dev-Actor-Id header -> {code(legacy)}")
        expect(legacy.status_code == 401, "self-asserted identity must be refused")
        unsigned = agent_http.post(
            CALLBACK_PATH,
            content=b'{"outcome":"OPENED"}',
            headers={**bearer(tokens.onboarding), "Content-Type": "application/json"},
        )
        tl.say("AGENT", f"forge an 'opened' provider callback (no signature) -> {code(unsigned)}")
        expect(unsigned.status_code == 401, "an unsigned callback must be refused")
        tl.say(
            "BANK",
            f"application is still {agent_state(agent_http, tokens.status, app_id)} (nothing advanced)",
        )

        # --- 4. customer confirms on the bank-controlled page -----------------------------------
        tl.heading("4. Customer (separate actor): sign in (SIMULATED) and confirm the exact action")
        link = f"/customer/handoffs/{handoff_id}"
        anonymous = customer_http.get(link, params={"token": nonce})
        expect(
            "Sign in" in anonymous.text and "Example Depot Bank" not in anonymous.text,
            "link alone must show no details",
        )
        tl.say(
            "CUSTOMER",
            "opened the link: sign-in page, nothing revealed (a link is not authentication)",
        )
        login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', anonymous.text)
        expect(login_csrf is not None, "the sign-in form has no CSRF token")
        login = customer_http.post(
            "/customer/dev-login",
            data={
                "customer_id": CUSTOMER_ID,
                "handoff_id": handoff_id,
                "token": nonce,
                "login_csrf": login_csrf.group(1),  # type: ignore[union-attr]
            },
            follow_redirects=False,
        )
        expect(login.status_code == 303, f"dev sign-in failed: {login.status_code}")
        cookie = login.headers.get("set-cookie", "").lower()
        expect("httponly" in cookie and "samesite=strict" in cookie, "weak session cookie")
        tl.say(
            "CUSTOMER",
            "DEV-ONLY simulated sign-in -> server-side session, HttpOnly + SameSite=Strict cookie",
        )
        page = customer_http.get(link, params={"token": nonce})
        expect(page.status_code == 200, f"review page: {page.status_code}")
        shown = {
            "provider": "Example Depot Bank AG (MOCK)",
            "product": "Basic Securities Account (MOCK)",
            "channel": "Direct sales (MOCK)",
            "application": app_id,
            "disclosure version": "<strong>1.0</strong>",
            "confirm button": "Confirm and authorize",
        }
        for label, needle in shown.items():
            expect(needle in page.text, f"the page does not show the {label}")
        tl.say(
            "CUSTOMER",
            "page shows: "
            + "; ".join(f"{k}={v}" for k, v in list(shown.items())[:4] if k != "application"),
        )
        tl.say(
            "CUSTOMER", f"page shows application reference {app_id} and disclosure v1.0 + risk text"
        )
        digest = re.search(r'name="action_digest" value="([0-9a-f]{64})"', page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        expect(
            digest is not None and digest.group(1) == handoff_step.data["action_digest"],
            "digest on the page differs from the agent's",
        )
        expect(csrf is not None, "the confirmation form has no CSRF token")
        form = {"token": nonce, "action_digest": digest.group(1), "csrf_token": csrf.group(1)}  # type: ignore[union-attr]
        no_csrf = customer_http.post(f"{link}/confirm", data={**form, "csrf_token": ""})
        tl.say(
            "CUSTOMER",
            f"(probe) confirm WITHOUT the page's CSRF token -> HTTP {no_csrf.status_code}",
        )
        expect(no_csrf.status_code == 403, "a form post without CSRF token must be refused")
        wrong = customer_http.post(f"{link}/confirm", data={**form, "action_digest": "0" * 64})
        tl.say("CUSTOMER", f"(probe) confirm a CHANGED action digest -> HTTP {wrong.status_code}")
        expect(wrong.status_code == 409, "a changed digest must be refused")
        done = customer_http.post(f"{link}/confirm", data=form)
        expect(
            done.status_code == 200 and "Confirmation recorded" in done.text,
            "customer confirmation failed",
        )
        tl.say(
            "CUSTOMER", "pressed 'Confirm and authorize' -> consent + disclosure receipts recorded"
        )
        replay = customer_http.post(f"{link}/confirm", data=form)
        tl.say(
            "CUSTOMER",
            f"(probe) press confirm again with the used challenge -> HTTP {replay.status_code}",
        )
        expect(replay.status_code == 409, "a used handoff must be refused")
        tl.say(
            "BANK",
            "deterministic checks ran: MOCK KYC=PASS, MOCK compliance=PASS, MOCK contract confirmation recorded",
        )
        tl.say(
            "AGENT",
            f"generic state is now: {agent_state(agent_http, tokens.status, app_id)} (no detail about checks)",
        )

        # --- 5. agent resumes and submits -----------------------------------------------------------
        tl.heading("5. Agent resumes after the customer and submits (scope submit_after_consent)")
        resumed = agent.continue_after_customer(app_id)
        expect(resumed.stopped_at is None, f"agent could not continue: {resumed.stopped_at}")
        tl.say(
            "AGENT",
            "POST /v1/applications/{id}/submit -> accepted; handed to the MOCK depot provider",
        )
        tl.say(
            "AGENT",
            f"server reports: {resumed.final_state} - the agent does NOT claim the account is open",
        )
        expect(resumed.final_state == "IN_PROGRESS", "submitted application must be in progress")
        again = agent_http.post(submit_url, headers=bearer(tokens.onboarding))
        tl.say(
            "AGENT", f"(probe) duplicate submit -> HTTP {again.status_code}, no second hand-over"
        )
        expect(again.status_code == 200, "duplicate submit must be idempotent")

        # --- 6. the MOCK provider confirms --------------------------------------------------------------
        tl.heading("6. MOCK depot provider: signed confirmation (the ONLY path to OPENED)")
        callback = build_signed_confirmation(
            callback_key.encode(), application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}"
        )
        result = provider_http.post(CALLBACK_PATH, content=callback.body, headers=callback.headers)
        expect(
            result.status_code == 200 and result.json() == {"result": "accepted"},
            f"callback: {result.text}",
        )
        tl.say(
            "MOCK PROVIDER",
            "sent HMAC-signed 'account opened' confirmation -> accepted by the bank",
        )
        duplicate = provider_http.post(
            CALLBACK_PATH, content=callback.body, headers=callback.headers
        )
        tl.say("MOCK PROVIDER", f"(probe) same message again -> {duplicate.json()['result']}")
        expect(
            duplicate.json() == {"result": "duplicate_ignored"},
            "duplicate confirmation must be ignored",
        )

        # --- 7. final status + revoked privileges --------------------------------------------------------
        tl.heading("7. Agent reads the final status; its onboarding privileges are revoked")
        final = agent.read_status(app_id)
        tl.say("AGENT", f"server reports (read-only status token): {final.final_state}")
        expect(final.final_state == "OPENED", "final state must be OPENED")
        after = agent_http.post(submit_url, headers=bearer(tokens.onboarding))
        tl.say("AGENT", f"(probe) submit with its onboarding token after opening -> {code(after)}")
        expect(after.status_code == 401, "the onboarding token must be revoked at OPENED")
        handoff_after = agent_http.post(
            f"/v1/applications/{app_id}/handoff", headers=bearer(tokens.onboarding)
        )
        tl.say("AGENT", f"(probe) request a new handoff after opening -> {code(handoff_after)}")
        expect(handoff_after.status_code == 401, "no handoff after opening")
        boot_after = agent_http.post(
            "/v1/applications",
            json=intent.model_dump(),
            headers={**bearer(boot.token), "Idempotency-Key": str(uuid.uuid4())},
        )
        tl.say("AGENT", f"(probe) use the bootstrap token after opening -> {code(boot_after)}")
        expect(boot_after.status_code == 401, "the bootstrap token must be revoked at OPENED")

        # --- 8. verification of persistence, audit and credentials ------------------------------------
        tl.heading(
            "8. Verify audit trail, evidence and credential revocation (read-only database check)"
        )
        audit_count = verify_database(tl, db_path, app_id, boot.token, tokens.onboarding)

        if restart:
            tl.heading("9. Restart the server: is the result persisted?")
            server.stop()
            tl.say("BANK", "server stopped")
            server.start()
            tl.say("BANK", "server started again on the same database file")
            restored = agent.read_status(app_id)
            expect(restored.final_state == "OPENED", "OPENED did not survive the restart")
            tl.say("AGENT", f"after restart the server still reports: {restored.final_state}")
            with contextlib.closing(read_db(db_path)) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE application_id = ?", (app_id,)
                ).fetchone()[0]
            expect(count == audit_count, "audit trail changed across the restart")
            tl.say("BANK", f"audit trail intact after restart ({count} events)")
            still = agent_http.post(submit_url, headers=bearer(tokens.onboarding))
            expect(still.status_code == 401, "revocation did not survive the restart")
            tl.say("AGENT", f"(probe) revoked onboarding token after restart -> {code(still)}")

        print(
            "\nRESULT: PASS - a scripted agent completed a simulated onboarding through the real REST API"
            " with scoped, revocable authorization."
        )
        print(
            "Reminder: KYC, compliance, contract and the depot provider are MOCK; the customer sign-in is"
            " simulated; agent authorization is a controlled DEV implementation, not OAuth/FAPI."
        )
    finally:
        server.stop()
        if bank is not None:
            bank.close()
        if keep:
            print(f"\n(kept demo data in {tmp})")
        else:
            remove_tree(tmp)


def remove_tree(path: Path) -> None:
    """Delete the demo folder. Windows releases a killed process's file handles a moment late."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.3)
    print(f"WARNING: could not remove the temporary demo folder {path}", file=sys.stderr)


def agent_state(client: httpx.Client, status_token: str, app_id: str) -> str:
    return str(
        client.get(f"/v1/applications/{app_id}", headers=bearer(status_token)).json()["state"]
    )


_DENIALS = {
    "application.submit_denied",
    "handoff.rejected",
    "authorization.denied",
    "application.access_denied",
}


def verify_database(
    tl: Timeline, db_path: Path, app_id: str, bootstrap_token: str, onboarding_token: str
) -> int:
    with contextlib.closing(read_db(db_path)) as db:
        events = db.execute(
            "SELECT event_type, actor_type, outcome, details FROM audit_events "
            "WHERE application_id = ? ORDER BY seq",
            (app_id,),
        ).fetchall()
        tl.say("BANK", f"audit trail for {app_id} ({len(events)} events):")
        for event in events:
            details = json.loads(event["details"])
            if event["event_type"] == "application.transition":
                extra = f"{details['from']} -> {details['to']} by {details['authority']} ({details['reason_code']})"
                extra += " [mandate revoked]" if details.get("mandate_revoked") else ""
            elif "reason" in details:
                extra = f"reason={details['reason']}"
            else:
                extra = ""
            print(
                f"           {event['event_type']:<34} {event['actor_type']:<9} {event['outcome']:<8} {extra}"
            )
        row = db.execute(
            "SELECT status, version FROM applications WHERE application_id = ?", (app_id,)
        ).fetchone()
        expect(row["status"] == "OPENED", f"database status is {row['status']}")
        tl.say("BANK", f"application row: status={row['status']} version={row['version']}")

        transitions = [
            json.loads(e["details"])["to"]
            for e in events
            if e["event_type"] == "application.transition"
        ]
        expect(
            transitions == [
                "AWAITING_AUTHORIZATION", "AUTHORIZED", "KYC_PENDING", "COMPLIANCE_PENDING",
                "CONTRACT_PENDING", "SUBMITTED_TO_DEPOT_PROVIDER", "OPENED",
            ],
            f"unexpected transitions: {transitions}",
        )  # fmt: skip
        opened = next(e for e in events if json.loads(e["details"]).get("to") == "OPENED")
        expect(opened["actor_type"] == "provider", "OPENED must be attributed to the provider")
        expect(
            sum(1 for e in events if json.loads(e["details"]).get("to") == "OPENED") == 1,
            "OPENED recorded more than once",
        )
        expect(
            all(
                e["outcome"] == "success"
                or (e["outcome"] == "denied" and e["event_type"] in _DENIALS)
                for e in events
            ),
            "unexpected failure event",
        )
        reasons = {json.loads(e["details"]).get("reason") for e in events}
        expect(
            {"agent_credentials_presented", "insufficient_scope", "csrf_token_invalid"} <= reasons,
            f"refusals were not audited: {reasons}",
        )

        checks = db.execute(
            "SELECT check_type, outcome, reference, mock FROM check_results WHERE application_id = ?",
            (app_id,),
        ).fetchall()
        expect(
            {c["check_type"] for c in checks} == {"KYC", "COMPLIANCE", "CONTRACT"},
            "missing check evidence",
        )
        expect(
            all(c["mock"] == 1 and c["reference"].startswith("MOCK-") for c in checks),
            "check evidence is not labelled MOCK",
        )
        tl.say(
            "BANK",
            "check evidence: "
            + ", ".join(f"{c['check_type']}={c['outcome']} (MOCK)" for c in checks),
        )

        receipts = db.execute(
            "SELECT COUNT(*) FROM consent_receipts WHERE application_id = ?", (app_id,)
        ).fetchone()[0]
        disclosures = db.execute(
            "SELECT document_version FROM disclosure_receipts WHERE application_id = ?", (app_id,)
        ).fetchall()
        expect(
            receipts == 1 and len(disclosures) == 1,
            "expected exactly one consent and one disclosure receipt",
        )
        tl.say(
            "BANK",
            f"consent receipts: {receipts}; disclosure acknowledged for version {disclosures[0]['document_version']}",
        )

        grants = {
            r["kind"]: r
            for r in db.execute(
                "SELECT kind, scopes, revoked_at FROM mandates WHERE application_id = ?", (app_id,)
            ).fetchall()
        }
        expect(set(grants) == {"bootstrap", "onboarding", "status"}, f"grants: {set(grants)}")
        expect(
            grants["bootstrap"]["revoked_at"] is not None
            and grants["onboarding"]["revoked_at"] is not None,
            "onboarding credentials were not revoked",
        )
        expect(grants["status"]["revoked_at"] is None, "the read-only status grant should remain")
        tl.say(
            "BANK",
            "grants: bootstrap=REVOKED, onboarding=REVOKED, status (read_status only)=active until expiry",
        )
        dump = repr([tuple(r) for r in db.execute("SELECT * FROM agent_access_tokens").fetchall()])
        expect(
            bootstrap_token not in dump and onboarding_token not in dump, "a raw token was stored"
        )
        revoked_tokens = db.execute(
            "SELECT COUNT(*) FROM agent_access_tokens t JOIN mandates m USING (mandate_id) "
            "WHERE m.application_id = ? AND m.kind != 'status' AND t.revoked_at IS NOT NULL",
            (app_id,),
        ).fetchone()[0]
        expect(revoked_tokens == 2, f"expected 2 revoked onboarding tokens, got {revoked_tokens}")
        tl.say(
            "BANK", f"tokens: {revoked_tokens} revoked at OPENED; only SHA-256 hashes are stored"
        )

        submissions = db.execute(
            "SELECT COUNT(*), MAX(confirmation_id IS NOT NULL) FROM depot_submissions WHERE application_id = ?",
            (app_id,),
        ).fetchone()
        expect(tuple(submissions) == (1, 1), "expected exactly one confirmed depot submission")
        tl.say(
            "BANK",
            "depot submission: 1 hand-over, 1 trusted confirmation (duplicates had no effect)",
        )
        return len(events)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="keep the temporary demo database")
    parser.add_argument(
        "--no-restart", action="store_true", help="skip the restart/persistence step"
    )
    args = parser.parse_args()
    try:
        run(keep=args.keep, restart=not args.no_restart)
    except DemoFailure as failure:
        print(f"\nRESULT: FAIL - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
