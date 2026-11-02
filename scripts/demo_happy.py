"""Phase 02 demo: a complete SIMULATED onboarding through the real REST API.

    python -m scripts.demo_happy [--keep] [--no-restart]

What it does
------------
* Creates a brand-new temporary SQLite database (never your project data) and starts the real
  FastAPI server (uvicorn) as a subprocess on a free local port.
* Plays four SEPARATE actors, each with its own HTTP client and credentials:
    AGENT      the scripted customer agent, through the public API only
    CUSTOMER   the simulated customer on the bank-controlled confirmation page (DEV sign-in)
    BANK       the trusted backend (visible only through audit/DB verification, read-only)
    MOCK PROVIDER   the MOCK depot provider sending its signed confirmation
* Shows three things the agent tries that MUST fail, then the happy path to OPENED.
* Stops and restarts the server to prove the result is persisted.

Everything is synthetic and MOCK. The demo prints application ids and states, but never the
one-time challenge, the callback secret or any credential. Exit code 0 only if every check holds.
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
from pathlib import Path

import httpx

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.integrations.agent_tools import AgentToolName
from app.integrations.provider_callback import build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway

ROOT = Path(__file__).resolve().parent.parent
CALLBACK_PATH = "/internal/depot-provider/confirmations"
AGENT_ID = "demo-agent-1"
CUSTOMER_ID = "demo-customer-1"
MIN_FREE_MB = 25


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

    def __init__(self, workdir: Path, db_path: Path, secret: str, port: int) -> None:
        self.base_url = f"http://127.0.0.1:{port}"
        self._port = port
        self._workdir = workdir
        self._env = {k: v for k, v in os.environ.items() if not k.startswith("TRUSTEQ_")}
        self._env.update(
            {
                "TRUSTEQ_ENVIRONMENT": "development",
                "TRUSTEQ_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
                "TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET": secret,
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


def mask(url: str) -> str:
    return re.sub(r"token=[^&\s]+", "token=***", url)


def read_db(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def run(keep: bool, restart: bool) -> None:
    tl = Timeline()
    tmp = Path(tempfile.mkdtemp(prefix="trusteq_demo_"))
    free_mb = shutil.disk_usage(tmp).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        remove_tree(tmp)
        raise DemoFailure(f"only {free_mb} MB free on the temp drive; need {MIN_FREE_MB} MB")
    db_path = tmp / "demo.db"
    secret = secrets.token_hex(24)  # per-run MOCK callback secret, never printed
    server = Server(tmp, db_path, secret, free_port())

    print("TRUSTEQ - Phase 02 simulated end-to-end onboarding (ALL DATA SYNTHETIC / MOCK)")
    print(f"isolated demo database: {db_path}  ({free_mb} MB free)")
    try:
        tl.heading("0. Start the real server on a clean database")
        server.start()
        tl.say("BANK", f"server up at {server.base_url} (fresh schema, no data)")

        agent_http = httpx.Client(base_url=server.base_url, timeout=15)
        customer_http = httpx.Client(base_url=server.base_url, timeout=15)
        provider_http = httpx.Client(base_url=server.base_url, timeout=15)
        agent_headers = {"X-Dev-Actor-Id": AGENT_ID}
        agent = ScriptedCustomerAgent(RestAgentToolGateway(agent_http, auth_headers=agent_headers))
        intent = OpenDepotIntent(
            provider_id="fixture-bank", product_id="depot-basic", channel_id="direct",
            customer_ref=CUSTOMER_ID,
        )  # fmt: skip

        # --- 1-2. discovery + draft + handoff request (agent) ---------------------------------
        tl.heading(
            "1. Agent: discover the provider, read the product, create the draft, ask for approval"
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
            f"channels offered: {manifest['channel_ids']}, verification=fixture digest pinned",
        )
        tl.say(
            "AGENT",
            f"product {product['product_id']} '{product['name']}', disclosure "
            f"{product['document_id']} v{product['document_version']}",
        )
        app_id = started.application_id or ""
        handoff_step = next(s for s in started.steps if s.tool is AgentToolName.REQUEST_HANDOFF)
        tl.say("AGENT", f"created application {app_id} (state DRAFT) via POST /v1/applications")
        tl.say(
            "AGENT",
            f"requested customer handoff {handoff_step.data['handoff_id']} (expires {handoff_step.data['expires_at']})",
        )
        tl.say("AGENT", f"link handed to the customer: {mask(started.confirmation_url or '')}")
        tl.say("AGENT", f"server reports generic state: {started.final_state}")
        expect(started.final_state == "AWAITING_CUSTOMER", "agent should now wait for the customer")

        # --- negative probes: what the agent must NOT be able to do ---------------------------
        tl.heading("2. Things the agent tries that must be refused")
        early = agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers)
        tl.say(
            "AGENT",
            f"submit before the customer confirmed -> HTTP {early.status_code} {early.json()['error']['code']}",
        )
        expect(early.status_code == 409, "early submit must be refused")
        match = re.search(
            r"/customer/handoffs/([^?]+)\?token=(.+)$", started.confirmation_url or ""
        )
        expect(match is not None, "no confirmation link")
        handoff_id, nonce = match.group(1), match.group(2)  # type: ignore[union-attr]
        own = agent_http.post(
            f"/v1/customer/handoffs/{handoff_id}/confirm",
            json={"nonce": nonce, "action_digest": handoff_step.data["action_digest"]},
            headers=agent_headers,
        )
        tl.say(
            "AGENT",
            f"confirm its OWN handoff (it even holds the nonce) -> HTTP {own.status_code} {own.json()['error']['code']}",
        )
        expect(own.status_code == 401, "the agent must not be able to confirm")
        forged = provider_http.post(
            CALLBACK_PATH,
            content=b'{"outcome":"OPENED"}',
            headers={"Content-Type": "application/json"},
        )
        tl.say(
            "AGENT",
            f"forge an 'opened' provider callback without the signature -> HTTP {forged.status_code}",
        )
        expect(forged.status_code == 401, "an unsigned callback must be refused")
        tl.say(
            "BANK",
            f"application is still {agent_state(agent_http, agent_headers, app_id)} (nothing advanced)",
        )

        # --- 3. customer confirms on the bank-controlled page -----------------------------------
        tl.heading("3. Customer (separate actor): sign in (SIMULATED) and confirm the exact action")
        link = f"/customer/handoffs/{handoff_id}"
        anonymous = customer_http.get(link, params={"token": nonce})
        expect(
            "Sign in" in anonymous.text and "Beispielbank" not in anonymous.text,
            "link alone must show no details",
        )
        tl.say(
            "CUSTOMER",
            "opened the link: the page asks for sign-in and reveals nothing (a link is not authentication)",
        )
        login = customer_http.post(
            "/customer/dev-login",
            data={"customer_id": CUSTOMER_ID, "handoff_id": handoff_id, "token": nonce},
            follow_redirects=False,
        )
        expect(login.status_code == 303, f"dev sign-in failed: {login.status_code}")
        tl.say("CUSTOMER", "DEV-ONLY simulated sign-in (not real identity verification)")
        page = customer_http.get(link, params={"token": nonce})
        expect(page.status_code == 200, f"review page: {page.status_code}")
        shown = {
            "provider": "Beispielbank Depot AG (MOCK)",
            "product": "Wertpapierdepot Basis (MOCK)",
            "channel": "Direktvertrieb (MOCK)",
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
        expect(
            digest is not None and digest.group(1) == handoff_step.data["action_digest"],
            "digest on the page differs from the agent's",
        )
        wrong = customer_http.post(
            f"{link}/confirm", data={"token": nonce, "action_digest": "0" * 64}
        )
        tl.say(
            "CUSTOMER", f"(probe) confirm with a CHANGED action digest -> HTTP {wrong.status_code}"
        )
        expect(wrong.status_code == 409, "a changed digest must be refused")
        done = customer_http.post(
            f"{link}/confirm",
            data={"token": nonce, "action_digest": digest.group(1)},  # type: ignore[union-attr]
        )
        expect(
            done.status_code == 200 and "Confirmation recorded" in done.text,
            "customer confirmation failed",
        )
        tl.say(
            "CUSTOMER", "pressed 'Confirm and authorize' -> consent + disclosure receipts recorded"
        )
        replay = customer_http.post(
            f"{link}/confirm", data={"token": nonce, "action_digest": digest.group(1)}
        )  # type: ignore[union-attr]
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
            f"generic state is now: {agent_state(agent_http, agent_headers, app_id)} (no detail about checks)",
        )

        # --- 4. agent resumes and submits -----------------------------------------------------------
        tl.heading("4. Agent resumes after the customer and submits")
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
        again = agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers)
        tl.say(
            "AGENT", f"(probe) duplicate submit -> HTTP {again.status_code}, no second hand-over"
        )
        expect(again.status_code == 200, "duplicate submit must be idempotent")

        # --- 5. the MOCK provider confirms --------------------------------------------------------------
        tl.heading("5. MOCK depot provider: signed confirmation (the ONLY path to OPENED)")
        callback = build_signed_confirmation(
            secret.encode(), application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}"
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

        # --- 6. the agent reads the final status --------------------------------------------------------------
        tl.heading("6. Agent reads the final status reported by the backend")
        final = agent.read_status(app_id)
        tl.say("AGENT", f"server reports: {final.final_state}")
        expect(final.final_state == "OPENED", "final state must be OPENED")
        after = agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers)
        tl.say(
            "AGENT",
            f"(probe) act on the application after opening -> HTTP {after.status_code} {after.json()['error']['code']}",
        )
        expect(after.status_code == 403, "the mandate must be revoked after opening")

        # --- 7. verification of persistence, audit and mandate ------------------------------------------------
        tl.heading(
            "7. Verify audit trail, evidence and mandate revocation (read-only database check)"
        )
        audit_count = verify_database(tl, db_path, app_id)

        if restart:
            tl.heading("8. Restart the server: is the result persisted?")
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

        print(
            "\nRESULT: PASS - a scripted agent completed a simulated onboarding through the real REST API."
        )
        print(
            "Reminder: KYC, compliance, contract and the depot provider are MOCK; the customer sign-in is simulated."
        )
    finally:
        server.stop()
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


def agent_state(client: httpx.Client, headers: dict[str, str], app_id: str) -> str:
    return str(client.get(f"/v1/applications/{app_id}", headers=headers).json()["state"])


def verify_database(tl: Timeline, db_path: Path, app_id: str) -> int:
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
                or e["event_type"].endswith("_denied")
                or e["event_type"].endswith("rejected")
                or e["event_type"] == "application.access_denied"
                for e in events
            ),
            "unexpected failure event",
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

        mandate = db.execute(
            "SELECT revoked_at FROM mandates WHERE application_id = ?", (app_id,)
        ).fetchone()
        expect(mandate["revoked_at"] is not None, "mandate was not revoked")
        tl.say("BANK", f"onboarding mandate revoked at {mandate['revoked_at']}")

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
