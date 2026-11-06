"""One end-to-end synthetic onboarding with everything that is actually integrated.

    python -m scripts.demo_integrated [--profile experienced|novice|incomplete]
                                      [--depot-scenario NAME] [--keep]

Offline and scripted: the real FastAPI server on a throw-away database, no model, no key, no network
beyond 127.0.0.1. The customer steps post what the real pages offer. It proves that the journey
works, not that anything here is regulated: customer sign-in, KYC, compliance, contract and the
depot provider are SIMULATED, tax data is checked locally only, and no tax authority, identity
provider or signing service is involved.

Prints ids, statuses and outcomes, never a bearer token, the one-time challenge, the callback key or
a tax identifier. Exit code 0 only if every check holds.
"""

import argparse
import contextlib
import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import httpx

from app.agents import OpenDepotIntent, ScriptedCustomerAgent
from app.agents.product_information import AnswerKind, ProductInformationAgent, ProductQuestion
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.provider_callback import build_signed_confirmation
from app.integrations.rest_gateway import RestAgentToolGateway
from scripts import customer_steps as steps
from scripts.demo_happy import (
    CALLBACK_PATH,
    MIN_FREE_MB,
    Bank,
    DemoFailure,
    Server,
    Timeline,
    code,
    expect,
    free_port,
    mask,
    read_db,
    remove_tree,
)

AGENT_ID = "demo-agent-1"
CUSTOMER_ID = "demo-customer-1"
PROVIDER_ID = "fixture-bank"
PRODUCT_ID = "depot-basic"


def _product_communication(
    tl: Timeline, http: httpx.Client, agent: ProductInformationAgent
) -> None:
    fee = agent.answer(PROVIDER_ID, PRODUCT_ID, ProductQuestion.FEE, "custody")
    expect(fee.kind is AnswerKind.APPROVED_FACT, "the custody fee is an approved fact")
    expect(fee.source is not None and fee.product_version is not None, "a fact needs its source")
    tl.say("AGENT", f"approved fact: {fee.statement}")
    tl.say(
        "AGENT",
        f"  product version {fee.product_version}, digest {fee.source['document_digest'][:16]}",
    )
    identity = agent.answer(PROVIDER_ID, PRODUCT_ID, ProductQuestion.PROVIDER_IDENTITY)
    tl.say("AGENT", f"approved fact: {identity.statement}")
    partner = agent.answer(PROVIDER_ID, PRODUCT_ID, ProductQuestion.PARTNER, "partner-1")
    tl.say("AGENT", f"approved fact: {partner.statement}")
    unknown = agent.answer(PROVIDER_ID, PRODUCT_ID, ProductQuestion.FEE, "account-closing")
    expect(unknown.kind is AnswerKind.NOT_IN_APPROVED_SOURCES, "an unknown fee must be declined")
    tl.say("AGENT", f"fee the source does not contain -> declined: {unknown.statement}")
    risk = agent.answer(PROVIDER_ID, PRODUCT_ID, ProductQuestion.GUARANTEE)
    expect(risk.kind is AnswerKind.ESCALATE_TO_HUMAN, "a guarantee question goes to a person")
    tl.say("AGENT", f"'is this guaranteed?' -> goes to a person: {risk.statement}")
    comparison = agent.compare_fees(PROVIDER_ID, (PRODUCT_ID,), "custody")
    expect(not comparison.comparable, "one product cannot be compared")
    tl.say("AGENT", f"comparison with only one verified product -> {comparison.note}")

    for text in (
        "This investment is guaranteed to be safe.",
        "This bank will definitely approve your account.",
    ):
        verdict = http.post(
            "/v1/claims/verify",
            json={"kind": "FREE_TEXT", "provider_id": PROVIDER_ID, "text": text},
        ).json()
        expect(verdict["decision"] != "APPROVED", f"must not be approved: {text}")
        tl.say("BANK", f"claim check '{text}' -> {verdict['decision']}")


def _customer_information(tl: Timeline, browser: httpx.Client, link: str, profile: str) -> None:
    page = browser.get(link).text
    for expected in ("Custody fee (MOCK)", "3 of 7", "kid-depot-basic", "no minimum deposit"):
        expect(expected in page, f"the review page must show: {expected}")
    tl.say(
        "CUSTOMER", "reviewed the product, fees, conditions, risk and disclosure on the bank's page"
    )
    expect(steps.page_form(page, "/confirm") is None, "no Confirm before the information exists")
    tl.say("BANK", "the page offers no Confirm yet: tax information and answers are missing")

    refused = steps.post_tax(
        browser, link, {**steps.TAX_COMPLETE, "synthetic_tax_id": steps.REAL_LOOKING_TAX_ID}
    )
    expect(refused.status_code == 422, "a real-looking identifier must be refused")
    expect(steps.REAL_LOOKING_TAX_ID not in refused.text, "a refused value must not be echoed")
    tl.say("CUSTOMER", f"tax form with a real-looking ID -> {code(refused)}, field status Invalid")
    missing = steps.post_tax(browser, link, {"tax_residency_country": "DE"})
    expect(missing.status_code == 422 and "Missing" in missing.text, "missing fields are named")
    tl.say("CUSTOMER", f"tax form with fields left out -> {code(missing)}, field status Missing")
    saved = steps.post_tax(browser, link, steps.TAX_COMPLETE)
    expect(saved.status_code == 303, "complete synthetic tax information must be accepted")
    tl.say("BANK", "tax information: customer-provided, locally validated, NOT externally verified")

    answered = steps.post_answers(browser, link, steps.ASSESSMENT_PROFILES[profile])
    expect(answered.status_code == 303, "the answers must be accepted")
    page = browser.get(link).text
    needs_acknowledgement = steps.page_form(page, "/assessment", "acknowledge") is not None
    expect(
        needs_acknowledgement == (profile != "experienced"),
        "a notice is needed exactly for the warning and for missing answers",
    )
    outcome = {
        "experienced": "ASSESSMENT_OK",
        "novice": "WARNING_REQUIRED",
        "incomplete": "INSUFFICIENT_INFORMATION",
    }[profile]
    tl.say("BANK", f"deterministic knowledge-and-experience result ({profile}): {outcome}")
    if needs_acknowledgement:
        expect(steps.page_form(page, "/confirm") is None, "no Confirm before the acknowledgement")
        expect(steps.post_acknowledgement(browser, link).status_code == 303, "acknowledgement")
        tl.say("CUSTOMER", "read the notice and decided to continue (the account is not refused)")


def _verify_records(tl: Timeline, db_path: Path, app_id: str) -> None:
    with contextlib.closing(read_db(db_path)) as db:
        transitions = db.execute(
            "SELECT details FROM audit_events WHERE application_id = ? "
            "AND event_type = 'application.transition' ORDER BY rowid",
            (app_id,),
        ).fetchall()
        kinds = [
            row[0]
            for row in db.execute(
                "SELECT event_type FROM audit_events WHERE application_id = ? ORDER BY rowid",
                (app_id,),
            ).fetchall()
        ]
        tax = db.execute(
            "SELECT tax_residency_country, external_verification FROM tax_information "
            "WHERE application_id = ?",
            (app_id,),
        ).fetchone()
        everything = repr(
            [
                db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - names from the schema
                for (table,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            ]
        )
    expect(len(transitions) >= 7, "the audit trail must hold the transitions")
    for required in (
        "customer_information.tax_validated",
        "customer_information.assessment_recorded",
        "handoff.confirmed",
        "application.submitted",
        "provider.confirmation_accepted",
    ):
        expect(required in kinds, f"the audit trail must hold {required}")
    tl.say("BANK", f"audit trail: {len(kinds)} events, {len(transitions)} state transitions")
    expect(tax is not None and tax["external_verification"] == "NOT_IMPLEMENTED", "tax record")
    for secret in (steps.SYNTHETIC_TAX_ID, steps.REAL_LOOKING_TAX_ID):
        expect(secret not in everything, "a tax identifier reached the database")
    tl.say("BANK", "no tax identifier in any table, audit event or response above")


def run(profile: str, depot_scenario: str, keep: bool) -> None:
    tl = Timeline()
    tmp = Path(tempfile.mkdtemp(prefix="trusteq_integrated_"))
    free_mb = shutil.disk_usage(tmp).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        remove_tree(tmp)
        raise DemoFailure(f"only {free_mb} MB free on the temp drive; need {MIN_FREE_MB} MB")
    db_path = tmp / "demo.db"
    callback_key = secrets.token_hex(24)
    server = Server(
        tmp,
        db_path,
        callback_key,
        free_port(),
        customer_information_required=True,
        depot_scenario=depot_scenario,
    )
    bank: Bank | None = None
    print("TRUSTEQ - integrated synthetic onboarding (SYNTHETIC / MOCK, scripted, offline)")
    try:
        server.start()
        bank = Bank(db_path)
        with (
            httpx.Client(base_url=server.base_url, timeout=15) as agent_http,
            httpx.Client(base_url=server.base_url, timeout=15) as browser,
            httpx.Client(base_url=server.base_url, timeout=15) as provider_http,
        ):
            credentials = AgentCredentials(bank.bootstrap(AGENT_ID, CUSTOMER_ID).token)
            gateway = RestAgentToolGateway(agent_http, credentials=credentials)
            agent = ScriptedCustomerAgent(gateway)

            tl.heading("1-2. Agent: verified provider and approved product facts, no website visit")
            _product_communication(tl, agent_http, ProductInformationAgent(gateway))

            tl.heading("3. Agent: prepare the application and ask for the customer's approval")
            started = agent.open_depot(
                OpenDepotIntent(
                    provider_id=PROVIDER_ID,
                    product_id=PRODUCT_ID,
                    channel_id="direct",
                    customer_ref=CUSTOMER_ID,
                )
            )
            expect(started.stopped_at is None, f"the agent stopped: {started.stopped_at}")
            app_id, url = started.application_id or "", started.confirmation_url or ""
            link = url[url.index("/customer/") :]
            tl.say("AGENT", f"application {app_id}, link for the customer: {mask(url)}")
            tl.say("AGENT", f"server reports generic state: {started.final_state}")

            tl.heading("4. Customer: sign in (SIMULATED DEV identity)")
            page = steps.sign_in(browser, link, CUSTOMER_ID)
            expect(page.status_code == 200, "the review page must open after the sign-in")
            tl.say("CUSTOMER", "signed in with a synthetic ID: not identity verification")

            tl.heading("5-8. Customer: review, tax information, knowledge and experience")
            _customer_information(tl, browser, link, profile)
            agent_view = agent.read_status(app_id)
            expect(
                agent_view.final_state == "AWAITING_CUSTOMER", "the agent sees only a generic state"
            )
            dumped = started.model_dump_json() + agent_view.model_dump_json()
            for word in (steps.SYNTHETIC_TAX_ID, "WARNING_REQUIRED", "tax_residency", "knows_"):
                expect(word not in dumped, f"the agent must not receive {word}")
            tl.say(
                "AGENT", f"still sees only: {agent_view.final_state}, no tax data, no assessment"
            )

            tl.heading("9-10. Customer authorizes; mock KYC, compliance and contract run")
            confirmed = steps.post_confirmation(browser, link)
            expect(confirmed.status_code == 200, f"confirmation failed: {code(confirmed)}")
            expect("Further processing is subject to" in confirmed.text, "the neutral wording")
            tl.say(
                "CUSTOMER", "pressed 'Confirm and authorize' (session, CSRF, one-time challenge)"
            )
            tl.say(
                "BANK",
                "MOCK KYC=PASS, MOCK compliance=PASS, MOCK contract recorded: no legal effect",
            )

            tl.heading("11. Agent submits; the versioned mock provider adapter answers")
            resumed = agent.continue_after_customer(app_id)
            if depot_scenario in {"current_v2", "v1_only", "v2_deprecated_fallback"}:
                expect(resumed.stopped_at is None, f"submit stopped: {resumed.stopped_at}")
            else:
                expect(resumed.stopped_at is not None, "a failed provider must stop the submission")
            with contextlib.closing(read_db(db_path)) as db:
                detail = json.loads(
                    db.execute(
                        "SELECT details FROM audit_events WHERE application_id = ? AND event_type "
                        "IN ('application.submitted', 'application.submit_denied') "
                        "ORDER BY rowid DESC LIMIT 1",
                        (app_id,),
                    ).fetchone()[0]
                )
            shown = {
                k: detail[k]
                for k in ("contract_version", "fallback_from", "refusal")
                if k in detail
            }
            tl.say(
                "BANK", f"provider adapter audit record: {shown}, mock={detail.get('mock', True)}"
            )

            tl.heading("12. MOCK provider: signed confirmation (the only path to OPENED)")
            callback = build_signed_confirmation(
                callback_key.encode(),
                application_id=app_id,
                provider_ref=f"MOCK-DEPOT-{app_id}",
            )
            answer = provider_http.post(
                CALLBACK_PATH, content=callback.body, headers=callback.headers
            )
            tl.say("MOCK PROVIDER", f"signed callback -> {code(answer)}")
            final = agent.read_status(app_id).final_state
            tl.say("AGENT", f"server reports: {final}")
            if resumed.stopped_at is not None:
                expect(answer.status_code == 403, "no submission: the callback must be refused")
                expect(final != "OPENED", "no OPENED after a failed integration")
                print(
                    "\nRESULT: PASS - the provider failed and no unauthorized transition happened "
                    f"(scenario {depot_scenario}, state {final})."
                )
                return

            expect(answer.status_code == 200 and final == "OPENED", "the account must be OPENED")
            tl.heading("13. Verify the audit trail and the records")
            _verify_records(tl, db_path, app_id)

        print(
            "\nRESULT: PASS - one synthetic onboarding from discovery to OPENED through the "
            "integrated customer steps and the versioned provider adapter."
        )
        print(
            "Reminder: the sign-in is a DEV simulation; KYC, compliance, contract and the depot "
            "provider are MOCK; tax data is locally validated only; the assessment is a synthetic "
            "demonstration, not legal compliance."
        )
    finally:
        if bank is not None:
            bank.close()
        server.stop()
        if keep:
            print(f"kept: {tmp}")
        else:
            remove_tree(tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--profile", choices=sorted(steps.ASSESSMENT_PROFILES), default="experienced"
    )
    parser.add_argument("--depot-scenario", default="current_v2")
    parser.add_argument("--keep", action="store_true", help="keep the temporary demo database")
    args = parser.parse_args()
    try:
        run(args.profile, args.depot_scenario, args.keep)
    except DemoFailure as failure:
        print(f"\nRESULT: FAIL - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
