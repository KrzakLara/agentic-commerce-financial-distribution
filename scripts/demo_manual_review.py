"""Phase 04 demo: MANUAL REVIEW - a synthetic compliance review, a customer who asks for a
person, and a bank operator who re-evaluates WITH evidence (never overrides a check).

    python -m scripts.demo_manual_review [--keep] [--no-restart]

Actors (separate clients, as in ``scripts.demo_happy``):
    AGENT          the scripted customer agent: public API + bearer tokens only
    CUSTOMER       a browser on the bank-controlled pages: SIMULATED sign-in, server-side
                   session, CSRF-protected forms, the "Mit einer Person sprechen" button
    BANK           the trusted backend: dev bootstrap, read-only database checks
    OPERATOR       bank staff through the trusted dev console (``scripts.dev_operator``)
    MOCK PROVIDER  the depot provider's signed confirmation

Scenario switch: the MOCK compliance provider answers REVIEW_REQUIRED for customer references
that start with ``synthetic-review-`` (like a sandbox test card number). Everything is
synthetic; nothing is real KYC, AML, signing or banking. Offline, no LLM key, deterministic.
Never prints a token, the one-time challenge or the callback key. Exit code 0 only if every
check holds.
"""

import argparse
import contextlib
import io
import json
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.api.pages import HUMAN_REQUEST_BUTTON
from app.core.config import Environment, Settings
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.agent_tools import AgentToolName
from app.integrations.provider_callback import build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway
from scripts import dev_operator
from scripts.demo_happy import (
    CALLBACK_PATH,
    MIN_FREE_MB,
    Bank,
    DemoFailure,
    Server,
    Timeline,
    agent_state,
    bearer,
    code,
    expect,
    free_port,
    mask,
    read_db,
    remove_tree,
)

AGENT_ID = "demo-agent-review"
CUSTOMER_ID = "synthetic-review-demo-1"  # selects the MOCK REVIEW_REQUIRED scenario
REVIEWER = "op-reviewer-1"
INDENT = " " * 24


def operator_console(tl: Timeline, db_path: Path, *argv: str, show_audit: bool = False) -> int:
    """Run the trusted dev console in-process on the demo database and print its answer."""
    settings = Settings(
        _env_file=None,
        environment=Environment.DEVELOPMENT,
        database_url=f"sqlite:///{db_path.as_posix()}",
    )
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = dev_operator.main(list(argv), settings=settings)
    shown = " ".join(a for a in argv if not a.startswith("rvw-"))
    tl.say("OPERATOR", f"dev_operator {shown} -> exit {exit_code}")
    for line in out.getvalue().splitlines():
        if line.startswith(("DEV OPERATOR CONSOLE", "operator op-")):
            continue
        if line.strip() == "audit trail:" and not show_audit:
            break
        print(f"{INDENT}{line}")
    for line in err.getvalue().splitlines():
        print(f"{INDENT}{line}")
    return exit_code


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

    print("TRUSTEQ - Phase 04 MANUAL REVIEW demo: human review without bypass (SYNTHETIC / MOCK)")
    print(f"isolated demo database: {db_path}")
    try:
        tl.heading("0. Start the real server on a clean database")
        server.start()
        bank = Bank(db_path)
        tl.say("BANK", f"server up at {server.base_url}")
        agent_http = httpx.Client(base_url=server.base_url, timeout=15)
        customer_http = httpx.Client(base_url=server.base_url, timeout=15)
        provider_http = httpx.Client(base_url=server.base_url, timeout=15)

        # --- 1. as in the happy path: draft + handoff --------------------------------------------
        tl.heading("1. Agent: discover, draft (partner channel), ask the customer for approval")
        boot = bank.bootstrap(AGENT_ID, CUSTOMER_ID)
        credentials = AgentCredentials(boot.token)
        agent = ScriptedCustomerAgent(RestAgentToolGateway(agent_http, credentials=credentials))
        intent = OpenDepotIntent(
            provider_id="fixture-bank", product_id="depot-basic", channel_id="partner-1",
            customer_ref=CUSTOMER_ID,
        )  # fmt: skip
        started = agent.open_depot(intent)
        expect(started.stopped_at is None, f"agent stopped at {started.stopped_at}")
        app_id = started.application_id or ""
        tokens = credentials.for_application(app_id)
        if tokens is None:
            raise DemoFailure("no application-bound tokens")
        step = next(s for s in started.steps if s.tool is AgentToolName.REQUEST_HANDOFF)
        match = re.search(
            r"/customer/handoffs/([^?]+)\?token=(.+)$", started.confirmation_url or ""
        )
        if match is None:
            raise DemoFailure("no confirmation link")
        handoff_id, nonce = match.group(1), match.group(2)
        tl.say("AGENT", f"created application {app_id} for customer {CUSTOMER_ID} (SYNTHETIC)")
        tl.say("AGENT", f"link for the customer: {mask(started.confirmation_url or '')}")

        # --- 2. customer signs in, confirms; compliance asks for a review --------------------
        tl.heading("2. Customer signs in on the bank page (SIMULATED) and confirms; checks run")
        link = f"/customer/handoffs/{handoff_id}"
        anonymous = customer_http.get(link, params={"token": nonce})
        expect(
            "Sign in" in anonymous.text and HUMAN_REQUEST_BUTTON not in anonymous.text,
            "the link alone must show nothing",
        )
        login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', anonymous.text)
        if login_csrf is None:
            raise DemoFailure("the sign-in form has no CSRF token")
        login = customer_http.post(
            "/customer/dev-login",
            data={"customer_id": CUSTOMER_ID, "handoff_id": handoff_id, "token": nonce,
                  "login_csrf": login_csrf.group(1)},
            follow_redirects=False,
        )  # fmt: skip
        expect(login.status_code == 303, f"dev sign-in failed: {login.status_code}")
        tl.say("CUSTOMER", "DEV-ONLY simulated sign-in -> server-side session (HttpOnly cookies)")
        page = customer_http.get(link, params={"token": nonce})
        expect(HUMAN_REQUEST_BUTTON in page.text, "the review page must offer a person")
        tl.say(
            "CUSTOMER",
            f"review page: exact action, terms, partner - and the button '{HUMAN_REQUEST_BUTTON}'",
        )
        digest = re.search(r'name="action_digest" value="([0-9a-f]{64})"', page.text)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        if digest is None or csrf is None:
            raise DemoFailure("the confirmation form is incomplete")
        expect(digest.group(1) == step.data["action_digest"], "the page shows another action")
        confirmed = customer_http.post(
            f"{link}/confirm",
            data={"token": nonce, "action_digest": digest.group(1), "csrf_token": csrf.group(1)},
        )
        expect(
            confirmed.status_code == 200 and "Confirmation recorded" in confirmed.text,
            f"confirmation failed: HTTP {confirmed.status_code}",
        )
        tl.say("CUSTOMER", "pressed 'Confirm and authorize' (CSRF-protected form)")
        with contextlib.closing(read_db(db_path)) as db:
            row = db.execute(
                "SELECT status, resume_status FROM applications WHERE application_id = ?",
                (app_id,),
            ).fetchone()
            cases = db.execute(
                "SELECT case_id, reason, status, interrupted_step FROM review_cases "
                "WHERE application_id = ?",
                (app_id,),
            ).fetchall()
        expect(row["status"] == "MANUAL_REVIEW", f"expected MANUAL_REVIEW, got {row['status']}")
        expect(
            len(cases) == 1 and cases[0]["reason"] == "COMPLIANCE_REVIEW_REQUIRED",
            "expected one compliance review case",
        )
        case_id = cases[0]["case_id"]
        tl.say(
            "BANK",
            "MOCK KYC=PASS; MOCK compliance=REVIEW_REQUIRED (synthetic scenario) -> automatic "
            "onboarding stopped",
        )
        tl.say(
            "BANK",
            f"persisted: state MANUAL_REVIEW, interrupted step {row['resume_status']}, review "
            f"case {case_id} OPEN (reason internal: {cases[0]['reason']})",
        )
        state = agent_state(agent_http, tokens.status, app_id)
        expect(state == "ACTION_REQUIRED", f"agent sees {state}")
        tl.say("AGENT", f"generic state: {state} - no reason, no check result, no case id")

        # --- 3. nothing automatic gets past the review --------------------------------------------
        tl.heading("3. Nothing automatic gets past the review")
        submit_url = f"/v1/applications/{app_id}/submit"
        early = agent_http.post(submit_url, headers=bearer(tokens.onboarding))
        tl.say("AGENT", f"submit while in review -> {code(early)}")
        expect(early.status_code == 409, "submit must be refused during the review")
        status_text = agent_http.get(
            f"/v1/applications/{app_id}", headers=bearer(tokens.status)
        ).text.lower()
        expect(
            not any(word in status_text for word in ("compliance", "review", "aml", "case")),
            "the agent's status view leaks review detail",
        )
        self_service = agent_http.post(
            f"/v1/customer/applications/{app_id}/request-human",
            json={"category": "HELP_WITH_PROCESS"},
            headers=bearer(tokens.onboarding),
        )
        tl.say("AGENT", f"request-human with its own bearer token -> {code(self_service)}")
        expect(self_service.status_code == 401, "an agent token must never act as the customer")
        claimed = agent_http.post(
            f"/v1/customer/applications/{app_id}/request-human",
            json={"category": "HELP_WITH_PROCESS"},
            headers={"X-Dev-Customer-Id": CUSTOMER_ID},
        )
        tl.say("AGENT", f"request-human claiming to BE the customer (header) -> {code(claimed)}")
        expect(claimed.status_code == 401, "a self-asserted customer id is not a session")
        early_callback = build_signed_confirmation(
            callback_key.encode(), application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}"
        )
        refused = provider_http.post(
            CALLBACK_PATH, content=early_callback.body, headers=early_callback.headers
        )
        tl.say(
            "MOCK PROVIDER",
            f"a correctly SIGNED 'opened' message for this unsubmitted application -> "
            f"{code(refused)}",
        )
        expect(refused.status_code == 403, "OPENED must not be reachable from a review")

        # --- 4. the customer presses the button ---------------------------------------------
        tl.heading(f"4. Customer presses '{HUMAN_REQUEST_BUTTON}' (bank page, session + CSRF)")
        form = re.search(
            rf'action="/customer/applications/{re.escape(app_id)}/request-human">'
            r'<input type="hidden" name="csrf_token" value="([^"]+)">',
            confirmed.text,
        )
        if form is None:
            raise DemoFailure("the confirmation page must offer a person, too")
        button_url = f"/customer/applications/{app_id}/request-human"
        forged = customer_http.post(
            button_url, data={"csrf_token": "x" * 43, "category": "QUESTION_ABOUT_PRODUCT"}
        )
        tl.say(
            "CUSTOMER", f"(probe) the button with a forged CSRF token -> HTTP {forged.status_code}"
        )
        expect(forged.status_code == 403, "a forged CSRF token must be refused")
        asked = customer_http.post(
            button_url, data={"csrf_token": form.group(1), "category": "QUESTION_ABOUT_PRODUCT"}
        )
        expect(
            asked.status_code == 200 and "Anfrage erhalten" in asked.text,
            f"request-human failed: HTTP {asked.status_code}",
        )
        lowered = asked.text.lower()
        expect(
            not any(word in lowered for word in ("compliance", "review", "aml", "verdacht")),
            "the acknowledgement must be neutral",
        )
        tl.say(
            "CUSTOMER",
            f"pressed '{HUMAN_REQUEST_BUTTON}' -> 'Anfrage erhalten': staff will get in touch",
        )
        tl.say(
            "BANK",
            "the request joined the existing case (one active case per application) - the "
            "answer does not reveal that a compliance review exists",
        )

        # --- 5. operator: evidence + deterministic re-evaluation ----------------------------------
        tl.heading("5. OPERATOR (trusted dev console): review with evidence, never an override")
        expect(operator_console(tl, db_path, "--operator", REVIEWER, "list") == 0, "list failed")
        expect(
            operator_console(tl, db_path, "--operator", REVIEWER, "show", "--case", case_id) == 0,
            "show failed",
        )
        expect(
            operator_console(
                tl, db_path, "--operator", "agent:" + AGENT_ID, "list"
            ) == 2,
            "an agent identity must not operate the console",
        )  # fmt: skip
        expect(
            operator_console(
                tl, db_path, "--operator", REVIEWER, "request-info", "--case", case_id,
                "--category", "SOURCE_OF_FUNDS_CONFIRMATION",
            ) == 0,
            "request-info failed",
        )  # fmt: skip
        expect(
            operator_console(
                tl, db_path, "--operator", REVIEWER, "decide", "--case", case_id,
                "--decision", "RE_EVALUATE",
            ) == 1,
            "re-evaluation must wait for the requested information",
        )  # fmt: skip
        expect(
            operator_console(
                tl, db_path, "--operator", REVIEWER, "decide", "--case", case_id,
                "--decision", "REJECT",
            ) == 1,
            "a reviewer must not reject (supervisor only)",
        )  # fmt: skip
        expect(
            operator_console(
                tl, db_path, "--operator", REVIEWER, "add-evidence", "--case", case_id,
                "--category", "SOURCE_OF_FUNDS_CONFIRMATION", "--reference", "SYN-SOF-0001",
            ) == 0,
            "add-evidence failed",
        )  # fmt: skip
        expect(
            operator_console(
                tl, db_path, "--operator", REVIEWER, "decide", "--case", case_id,
                "--decision", "RE_EVALUATE",
            ) == 0,
            "re-evaluation failed",
        )  # fmt: skip
        tl.say(
            "BANK",
            "compliance re-ran as attempt 2 WITH the evidence: MOCK result PASS -> contract "
            "recorded; attempt 1 is kept unchanged",
        )

        # --- 6. the agent continues; only the provider opens --------------------------------------
        tl.heading("6. Agent continues; MOCK provider confirms (the ONLY path to OPENED)")
        resumed = agent.continue_after_customer(app_id)
        expect(resumed.stopped_at is None, f"agent could not continue: {resumed.stopped_at}")
        tl.say("AGENT", f"submit after consent -> accepted; server reports {resumed.final_state}")
        callback = build_signed_confirmation(
            callback_key.encode(), application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}"
        )
        result = provider_http.post(CALLBACK_PATH, content=callback.body, headers=callback.headers)
        expect(result.status_code == 200, f"callback: {code(result)}")
        final = agent.read_status(app_id)
        expect(final.final_state == "OPENED", f"final state {final.final_state}")
        tl.say("MOCK PROVIDER", "signed confirmation accepted -> OPENED")
        tl.say("AGENT", f"server reports: {final.final_state}")

        # --- 7. read-only verification ------------------------------------------------------------
        tl.heading("7. Verify the review trail (read-only database check)")
        audit_count = verify_review_trail(tl, db_path, app_id, case_id)

        if restart:
            tl.heading("8. Restart the server: is everything persisted?")
            server.stop()
            server.start()
            restored = agent.read_status(app_id)
            expect(restored.final_state == "OPENED", "OPENED did not survive the restart")
            with contextlib.closing(read_db(db_path)) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE application_id = ?", (app_id,)
                ).fetchone()[0]
                closed = db.execute(
                    "SELECT status FROM review_cases WHERE case_id = ?", (case_id,)
                ).fetchone()[0]
            expect(count == audit_count and closed == "CLOSED", "history changed across restart")
            tl.say("BANK", f"after restart: OPENED, case CLOSED, {count} audit events intact")

        print(
            "\nRESULT: PASS - REVIEW_REQUIRED created a durable case; a human re-evaluated with "
            "evidence; the deterministic check and the provider - not the human - decided."
        )
        print(
            "Reminder: KYC, compliance, contract and the depot provider are MOCK; customer and "
            "operator identities are simulated DEV identities."
        )
    finally:
        server.stop()
        if bank is not None:
            bank.close()
        if keep:
            print(f"\n(kept demo data in {tmp})")
        else:
            remove_tree(tmp)


def verify_review_trail(tl: Timeline, db_path: Path, app_id: str, case_id: str) -> int:
    with contextlib.closing(read_db(db_path)) as db:
        case = db.execute(
            "SELECT status, resolution, resolved_by FROM review_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        expect(
            tuple(case) == ("CLOSED", "RE_EVALUATE", REVIEWER),
            f"unexpected case resolution {tuple(case)}",
        )
        cases = db.execute(
            "SELECT COUNT(*) FROM review_cases WHERE application_id = ?", (app_id,)
        ).fetchone()[0]
        expect(cases == 1, f"expected exactly one review case, found {cases}")
        tl.say("BANK", f"review case: CLOSED, resolution RE_EVALUATE by {REVIEWER} (1 case total)")
        checks = [
            tuple(r)
            for r in db.execute(
                "SELECT check_type, attempt, outcome FROM check_results "
                "WHERE application_id = ? ORDER BY id",
                (app_id,),
            ).fetchall()
        ]
        expect(
            checks == [
                ("KYC", 1, "PASS"), ("COMPLIANCE", 1, "REVIEW_REQUIRED"),
                ("COMPLIANCE", 2, "PASS"), ("CONTRACT", 1, "PASS"),
            ],
            f"unexpected check attempts {checks}",
        )  # fmt: skip
        tl.say(
            "BANK",
            "check attempts (append-only): " + ", ".join(f"{t}#{a}={o}" for t, a, o in checks),
        )
        events = db.execute(
            "SELECT event_type, actor_type, outcome, details FROM audit_events "
            "WHERE application_id = ? ORDER BY seq",
            (app_id,),
        ).fetchall()
        transitions = [
            json.loads(e["details"]) for e in events if e["event_type"] == "application.transition"
        ]
        for t in transitions:
            print(f"{INDENT}{t['from']} -> {t['to']} by {t['authority']} ({t['reason_code']})")
        steps = [(t["from"], t["to"], t["authority"]) for t in transitions]
        expect(
            ("COMPLIANCE_PENDING", "MANUAL_REVIEW", "SYSTEM_POLICY") in steps
            and ("MANUAL_REVIEW", "COMPLIANCE_PENDING", "OPERATOR") in steps,
            "the review was not taken through the state machine",
        )
        operator_targets = {t["to"] for t in transitions if t["authority"] == "OPERATOR"}
        expect(operator_targets == {"COMPLIANCE_PENDING"}, f"operator moved to {operator_targets}")
        opened = [t for t in transitions if t["to"] == "OPENED"]
        expect(
            len(opened) == 1 and opened[0]["authority"] == "DEPOT_PROVIDER_CONFIRMATION",
            "OPENED must come from the provider confirmation only",
        )
        types = {e["event_type"] for e in events}
        for expected in (
            "review.case_opened", "review.human_requested", "review.information_requested",
            "review.decision_denied", "review.evidence_recorded", "review.decided",
            "check.re_evaluated",
        ):  # fmt: skip
            expect(expected in types, f"missing audit event {expected}")
        tl.say(
            "BANK",
            f"{len(events)} audit events, incl. case opened, human requested, information "
            "requested, 2 refused decisions, evidence, decision, re-evaluation",
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
