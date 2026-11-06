"""One integrated journey across separate processes, plus the negative journeys.

    python -m scripts.demo_trust_journey

Starts a synthetic trust authority, a local OpenID Connect provider, a synthetic signing and
proofing
provider, the bank and, as its own process, an independent agent that imports nothing from the bank.
Everything stays on 127.0.0.1: offline, no model, no API key, no paid call.

What it shows: the mechanics work end to end across real process boundaries, with the bank's
controls
in place. What it does not show: no regulated identification and no qualified signature took place,
the authority vetted nobody, and no real institution is involved. The script plays the customer's
browser, on the bank's real pages.

Prints no token, one-time link, key or password. Exit code 0 only if every check holds.
"""

import contextlib
import html
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from joserfc import jwt

from app.integrations.provider_callback import build_signed_confirmation
from local_services.common import generate_key, public_jwks
from scripts import customer_steps as steps
from scripts.trust_stack import ROOT, TrustStack, _clean_environment, start_stack

SHOWN_STAGES = {
    "discovered_origin",
    "provider_verified",
    "card_verified",
    "profile_verified",
    "catalog_read",
    "verified_fact",
    "delegation_requested",
    "delegation_granted",
    "application_prepared",
    "referral_carried",
    "customer_confirmation_requested",
    "submitted",
    "final_status",
    "provider_refused",
    "revoked_delegation_refused",
    "submit_refused",
}
_HIDDEN_FIELDS = {"customer_link"}


class JourneyFailed(Exception):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise JourneyFailed(message)


def say(actor: str, text: str) -> None:
    print(f"  {actor:<13} {text}", flush=True)


class AgentProcess:
    """The independent agent, as a child process. One JSON line per stage."""

    def __init__(self, command: str, config_path: Path) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - our own interpreter and module
            [sys.executable, "-m", "external_agent.cli", command, "--config", str(config_path)],
            cwd=ROOT,
            env=_clean_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def read_until(self, *stages: str) -> dict[str, Any]:
        """The next line whose stage is one of `stages`. A stop or an exit raises."""
        for line in self.process.stdout or ():
            event = json.loads(line)
            stage = event["stage"]
            if stage in SHOWN_STAGES:
                shown = {k: v for k, v in event.items() if k not in _HIDDEN_FIELDS | {"stage"}}
                say("AGENT PROCESS", f"{stage}: {json.dumps(shown)[:150]}")
            if stage in stages:
                return event
            if stage == "stopped":
                raise JourneyFailed(f"the agent stopped: {event.get('reason')}")
        raise JourneyFailed(f"the agent ended before {stages}")

    def tell_customer_done(self) -> None:
        if self.process.stdin is None:
            raise JourneyFailed("the agent has no input")
        self.process.stdin.write("done\n")
        self.process.stdin.flush()

    def finish(self) -> int:
        with contextlib.suppress(subprocess.TimeoutExpired):
            return self.process.wait(timeout=30)
        self.process.kill()
        return -1


class CustomerBrowser:
    """What a browser does on the real pages. It knows the customer's synthetic account only."""

    def __init__(self, stack: TrustStack, username: str) -> None:
        self.stack = stack
        self.username = username
        self.client = httpx.Client(timeout=15)

    def close(self) -> None:
        self.client.close()

    def _login_if_asked(self, url: str) -> httpx.Response:
        """Follows the bank's redirect to the provider's sign-in form and signs in there."""
        response = self.client.get(url, follow_redirects=True)
        if str(response.url).startswith(self.stack.idp_url):
            signed_in = self.client.post(
                str(response.url),
                data={
                    "username": self.username,
                    "password": self.stack.users[self.username],
                },
                follow_redirects=True,
            )
            expect(signed_in.status_code == 200, "the customer's sign-in failed")
            expect("Signed in" in signed_in.text, "the bank did not accept the login")
            response = self.client.get(url, follow_redirects=True)
        return response

    def approve_delegation(self, link: str, *, approve: bool = True) -> None:
        page = self._login_if_asked(link)
        form = steps.page_form(page.text, "/customer/delegations/authorize")
        expect(form is not None, "no delegation form")
        answer = self.client.post(
            urljoin(link, form["_action"]),
            data={
                "request": form["request"],
                "csrf_token": form["csrf_token"],
                "decision": "approve" if approve else "deny",
            },
        )
        expect(answer.status_code == 200, f"delegation decision: {answer.status_code}")

    def revoke_delegation(self, delegation_id: str) -> None:
        page = self.client.get(f"{self.stack.bank_url}/customer/delegations").text
        action = f"/customer/delegations/{delegation_id}/revoke"
        form = steps.page_form(page, action)
        expect(form is not None, "no withdraw form")
        answer = self.client.post(
            f"{self.stack.bank_url}{action}", data={"csrf_token": form["csrf_token"]}
        )
        expect(answer.status_code == 200, "the customer could not withdraw")

    def identity_proofing(self, link: str, outcome: str) -> None:
        page = self._login_if_asked(link).text
        form = steps.page_form(page, "/identity-proofing")
        expect(form is not None, "no identity-verification form on the page")
        handover = self.client.post(
            urljoin(link, form["_action"]),
            data={"token": form["token"], "csrf_token": form["csrf_token"]},
        )
        provider_page = self.client.get(_provider_link(handover.text))
        expect("not eID" in provider_page.text, "the test provider must say what it is not")
        done = self.client.post(f"{provider_page.url}/complete", data={"outcome": outcome})
        expect(done.status_code == 200, "the test provider failed")

    def information_and_confirmation(self, link: str, profile: str) -> None:
        relative = link[link.index("/customer/") :]
        session = self.client
        session_base = self.stack.bank_url
        with httpx.Client(base_url=session_base, cookies=session.cookies, timeout=15) as browser:
            steps.complete_customer_information(browser, relative, profile)
            confirmed = steps.post_confirmation(browser, relative)
            expect(confirmed.status_code == 200, f"confirmation: {confirmed.status_code}")
            expect("Your account is not open yet" in confirmed.text, "the neutral wording")

    def sign_contract(self, application_id: str, outcome: str = "SIGNED") -> str:
        url = f"{self.stack.bank_url}/customer/applications/{application_id}/contract"
        page = self.client.get(url)
        expect(page.status_code == 200, f"the contract page: {page.status_code}")
        form = steps.page_form(page.text, "/contract/authorize")
        expect(form is not None, "no authorization form")
        handover = self.client.post(
            urljoin(url, form["_action"]),
            data={"csrf_token": form["csrf_token"], "package_hash": form["package_hash"]},
        )
        signer = self.client.get(_provider_link(handover.text))
        expect("No qualified" in signer.text, "the test signer must say it creates no QES")
        done = self.client.post(f"{signer.url}/complete", data={"outcome": outcome})
        expect(done.status_code == 200, "the test signer failed")
        return form["package_hash"]


def _provider_link(page: str) -> str:
    match = re.search(r'id="provider-link" href="([^"]+)"', page)
    expect(match, "the bank's page has no link to the provider")
    return html.unescape(match.group(1))  # type: ignore[union-attr]


def _db(stack: TrustStack) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{stack.db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def _application_of(stack: TrustStack, delegation_id: str) -> str:
    with contextlib.closing(_db(stack)) as db:
        rows = db.execute(
            "SELECT application_id FROM mandates WHERE mandate_id IN "
            "(SELECT mandate_id FROM delegated_mandates WHERE delegation_id = ?) "
            "AND application_id IS NOT NULL",
            (delegation_id,),
        ).fetchall()
    expect(rows, "no application is linked to the delegation")
    return rows[0]["application_id"]


def _status(stack: TrustStack, application_id: str) -> str:
    with contextlib.closing(_db(stack)) as db:
        return db.execute(
            "SELECT status FROM applications WHERE application_id = ?", (application_id,)
        ).fetchone()[0]


def provider_opens(stack: TrustStack, application_id: str) -> int:
    callback = build_signed_confirmation(
        stack.callback_secret.encode(),
        application_id=application_id,
        provider_ref=f"MOCK-DEPOT-{application_id}",
    )
    return httpx.post(
        f"{stack.bank_url}/internal/depot-provider/confirmations",
        content=callback.body,
        headers=callback.headers,
        timeout=10,
    ).status_code


def referral_for(stack: TrustStack, partner: str = "sampletown-coop-bank") -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "alg": "ES256",
            "typ": "partner-referral+jwt",
            "kid": stack.partner_key.as_dict()["kid"],
        },
        {
            "iss": partner,
            "aud": stack.bank_url,
            "product_id": "depot-basic",
            "terms_version": "sample-2026.11",
            "iat": now,
            "exp": now + 600,
            "jti": uuid.uuid4().hex,
        },
        stack.partner_key,
        algorithms=["ES256"],
    )


def agent_config(stack: TrustStack, **extra: Any) -> Path:  # noqa: ANN401
    config = json.loads(stack.agent_config_path.read_text(encoding="utf-8"))
    config.update(extra)
    path = stack.workdir / f"agent-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


# --- the main journey -----------------------------------------------------------------------------


def main_journey(stack: TrustStack) -> str:
    print("\n== A. The integrated journey: discovery to a synthetic OPENED")
    agent = AgentProcess("journey", agent_config(stack, referral_token=referral_for(stack)))
    browser = CustomerBrowser(stack, "alice-synthetic")
    try:
        pending = agent.read_until("delegation_requested")
        say(
            "CUSTOMER",
            "opens the agent's link, signs in at the identity provider, sees the request",
        )
        browser.approve_delegation(pending["customer_link"])
        say("CUSTOMER", "allowed this agent: limited scopes, this product, this channel")
        agent.read_until("delegation_granted")
        link_event = agent.read_until("customer_confirmation_requested")
        application_id = _application_of(stack, pending["delegation_id"])
        link = link_event["customer_link"]
        browser.identity_proofing(link, "VERIFIED")
        say("TEST PROVIDER", "identity result VERIFIED: synthetic, regulatory_evidence=false")
        browser.information_and_confirmation(link, "experienced")
        say("CUSTOMER", "supplied tax information and answers, then confirmed the exact action")
        expect(
            _status(stack, application_id) == "CONTRACT_PENDING",
            "the application should await the contract",
        )
        package_hash = browser.sign_contract(application_id)
        say("CUSTOMER", f"authorized signing of package {package_hash[:16]}...")
        say("TEST SIGNER", "signed callback sent: no legal effect, not a QES")
        agent.tell_customer_done()
        agent.read_until("submitted")
        expect(provider_opens(stack, application_id) == 200, "the provider's confirmation")
        say("MOCK PROVIDER", "signed confirmation accepted: the only path to OPENED")
        agent.tell_customer_done()
        final = agent.read_until("final_status")
        expect(final["state"] == "OPENED", f"final state {final['state']}")
        expect(agent.finish() == 0, "the agent's exit code")
    finally:
        browser.close()
        if agent.process.poll() is None:
            agent.process.kill()
    check_evidence(stack, application_id, pending["delegation_id"], package_hash)
    return application_id


def check_evidence(
    stack: TrustStack, application_id: str, delegation_id: str, package_hash: str
) -> None:
    """Identities, delegation, policy version, product version, contract hash, attribution."""
    print("\n== B. The audit evidence of that application")
    with contextlib.closing(_db(stack)) as db:
        events = {}
        for row in db.execute(
            "SELECT event_type, details FROM audit_events WHERE application_id = ? ORDER BY rowid",
            (application_id,),
        ):
            events.setdefault(row["event_type"], []).append(json.loads(row["details"]))
        delegation = db.execute(
            "SELECT client_id, customer_id, identity_assurance, scopes, status "
            "FROM delegations WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        package = db.execute(
            "SELECT canonical_json, package_hash FROM contract_packages WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        signature = db.execute(
            "SELECT state, level, legal_effect FROM signature_sessions WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        everything = repr(
            [
                db.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608 - schema names
                for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            ]
        )
        dumped = "\n".join(
            str(tuple(row))
            for table in (
                "audit_events",
                "delegations",
                "identity_proofing_cases",
                "contract_packages",
                "signature_sessions",
                "attribution_decisions",
                "policy_decisions",
            )
            for row in db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        )
    confirmed = events["handoff.confirmed"][0]
    submitted = events["application.submitted"][0]
    document = json.loads(package["canonical_json"])
    expect(delegation["client_id"] == "personal-agent-demo", "the agent's identity")
    expect(delegation["customer_id"].startswith("cid-"), "the customer's pseudonymous reference")
    expect(delegation["identity_assurance"] == "oidc_authenticated_not_proofed", "assurance label")
    say(
        "BANK",
        f"agent {delegation['client_id']}, customer reference {delegation['customer_id'][:12]}...",
    )
    say("BANK", f"delegation {delegation_id[:16]}... assurance {delegation['identity_assurance']}")
    expect(confirmed["policy_version"] == submitted["policy_version"], "the policy version")
    say("BANK", f"policy version {submitted['policy_version']}")
    expect(document["product_version"] == "2026.11", "the product version")
    say("BANK", f"product version {document['product_version']}, terms digest in the package")
    expect(package["package_hash"] == package_hash == submitted["contract_package_hash"], "hash")
    say("BANK", f"contract hash {package_hash[:16]}... in the package, the signature and the audit")
    expect(signature["state"] == "SIGNED" and not signature["legal_effect"], "the signature row")
    expect(signature["level"] == "TEST_NO_LEGAL_EFFECT", "the signature level")
    say("BANK", f"signature {signature['state']}, level {signature['level']}, legal effect none")
    decision = events["attribution.decided"][-1]
    expect(decision["partner_id"] == "sampletown-coop-bank", "the attribution")
    say("BANK", f"attribution {decision['outcome']} to {decision['partner_id']}, by policy")
    say("BANK", f"commission status {decision['commission_status']}, not payable")
    proofing = events["identity_proofing.result"][0]
    expect(proofing["regulatory_evidence"] is False, "proofing has no regulatory value")
    expect(proofing["kyc_status_set"] is False, "a proofing event sets no KYC status")
    for secret in (steps.SYNTHETIC_TAX_ID, "tqa_", "tqs_"):
        expect(secret not in dumped, f"found {secret} in the evidence tables")
    expect(steps.SYNTHETIC_TAX_ID not in everything, "a tax identifier reached the database")
    say("BANK", "no tax identifier in any table, no raw token in the evidence tables")


# --- the negative journeys ------------------------------------------------------------------------


def forged_provider(stack: TrustStack) -> None:
    print("\n== C. Negative: forged or untrusted provider identity")
    forged = generate_key("bank-2026-11")
    entity = "http://127.0.0.1:9"
    anchor_file = stack.workdir / "rogue-anchor.jwks.json"
    anchor_file.write_text(json.dumps(public_jwks(forged)), encoding="utf-8")
    config = agent_config(
        stack,
        trust_anchors=[{"entity_id": entity, "jwks_file": str(anchor_file)}],
        known_origin=stack.bank_url,
    )
    process = subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "external_agent.cli", "verify", "--config", str(config)],
        cwd=ROOT,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    event = json.loads(process.stdout.strip().splitlines()[-1])
    expect(process.returncode == 3, f"exit code {process.returncode}")
    expect(event["reason"] == "untrusted_anchor", event)
    say("AGENT PROCESS", f"an anchor it does not trust -> refused: {event['reason']}")

    good = json.loads(stack.agent_config_path.read_text(encoding="utf-8"))
    wrong_key = stack.workdir / "wrong-key.jwks.json"
    wrong_key.write_text(json.dumps(public_jwks(generate_key("authority-2026-11"))), "utf-8")
    config = agent_config(
        stack,
        trust_anchors=[{"entity_id": good["trust_anchors"][0]["entity_id"],
                        "jwks_file": str(wrong_key)}],
        known_origin=stack.bank_url,
    )  # fmt: skip
    process = subprocess.run(  # noqa: S603 - our own interpreter and module
        [sys.executable, "-m", "external_agent.cli", "verify", "--config", str(config)],
        cwd=ROOT,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    event = json.loads(process.stdout.strip().splitlines()[-1])
    expect(process.returncode == 3 and event["reason"] == "bad_signature", event)
    say("AGENT PROCESS", f"an authority signed with another key -> refused: {event['reason']}")


def revoked_delegation(stack: TrustStack) -> None:
    print("\n== D. Negative: a revoked delegation attempts an action")
    agent = AgentProcess("journey", agent_config(stack, expect_revoked=True, channel_id="direct"))
    browser = CustomerBrowser(stack, "bob-synthetic")
    try:
        pending = agent.read_until("delegation_requested")
        browser.approve_delegation(pending["customer_link"])
        agent.read_until("customer_confirmation_requested")
        application_id = _application_of(stack, pending["delegation_id"])
        browser.revoke_delegation(pending["delegation_id"])
        say("CUSTOMER", "withdrew the delegation on the bank's page")
        agent.tell_customer_done()
        refused = agent.read_until("revoked_delegation_refused")
        expect(refused["detail"]["code"] == -32041, refused)
        expect(agent.finish() == 0, "the agent's exit code")
        expect(_status(stack, application_id) != "OPENED", "still not opened")
        say("BANK", f"state {_status(stack, application_id)}: nothing advanced")
    finally:
        browser.close()
        if agent.process.poll() is None:
            agent.process.kill()


def failed_identification(stack: TrustStack) -> None:
    print("\n== E. Negative: failed identification needs a person")
    agent = AgentProcess(
        "journey", agent_config(stack, expect_submit_refused=True, channel_id="direct")
    )
    browser = CustomerBrowser(stack, "bob-synthetic")
    try:
        pending = agent.read_until("delegation_requested")
        browser.approve_delegation(pending["customer_link"])
        link = agent.read_until("customer_confirmation_requested")["customer_link"]
        application_id = _application_of(stack, pending["delegation_id"])
        browser.identity_proofing(link, "REJECTED")
        say("TEST PROVIDER", "identity result REJECTED")
        expect(_status(stack, application_id) == "MANUAL_REVIEW", "parked for a person")
        agent.tell_customer_done()
        refused = agent.read_until("submit_refused")
        expect(refused["detail"]["data"]["code"] == "prerequisites_not_met", refused)
        expect(agent.finish() == 0, "the agent's exit code")
        expect(provider_opens(stack, application_id) == 403, "no OPENED without a submission")
        expect(_status(stack, application_id) == "MANUAL_REVIEW", "still parked")
        say(
            "BANK", "MANUAL_REVIEW, a review case is open, and a signed 'opened' message is refused"
        )
    finally:
        browser.close()
        if agent.process.poll() is None:
            agent.process.kill()


def failed_signature(stack: TrustStack) -> None:
    print("\n== F. Negative: a declined signature needs a person")
    agent = AgentProcess(
        "journey", agent_config(stack, expect_submit_refused=True, channel_id="direct")
    )
    browser = CustomerBrowser(stack, "bob-synthetic")
    try:
        pending = agent.read_until("delegation_requested")
        browser.approve_delegation(pending["customer_link"])
        link = agent.read_until("customer_confirmation_requested")["customer_link"]
        application_id = _application_of(stack, pending["delegation_id"])
        browser.identity_proofing(link, "VERIFIED")
        browser.information_and_confirmation(link, "novice")
        say("CUSTOMER", "the answers gave a warning: acknowledged, and the account is not refused")
        browser.sign_contract(application_id, "DECLINED")
        say("TEST SIGNER", "declined")
        expect(_status(stack, application_id) == "MANUAL_REVIEW", "parked for a person")
        agent.tell_customer_done()
        refused = agent.read_until("submit_refused")
        expect(refused["detail"]["data"]["code"] == "prerequisites_not_met", refused)
        expect(agent.finish() == 0, "the agent's exit code")
        expect(provider_opens(stack, application_id) == 403, "no OPENED without a signature")
        say("BANK", "MANUAL_REVIEW, and no signature means no depot")
    finally:
        browser.close()
        if agent.process.poll() is None:
            agent.process.kill()


def check_logs(stack: TrustStack) -> None:
    print("\n== G. The services' logs")
    combined = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in stack.logs.values()
    )
    for label, marker in {
        "synthetic tax ID": steps.SYNTHETIC_TAX_ID,
        "agent token": "tqa_",
        "customer session": "tqs_",
        "one-time link": "token=",
        "private key member": '"d":',
    }.items():
        expect(marker not in combined, f"{label} in a log")
    say("BANK", f"checked {len(stack.logs)} local service logs at this run's level: none found")


def run() -> None:
    print(
        "TRUSTEQ - full-trust journey across separate processes "
        "(SYNTHETIC, local, no model, no real provider)"
    )
    stack = start_stack()
    try:
        say("BANK", f"services up: {len(stack.processes)} separate processes on 127.0.0.1")
        main_journey(stack)
        forged_provider(stack)
        revoked_delegation(stack)
        failed_identification(stack)
        failed_signature(stack)
        check_logs(stack)
    finally:
        stack.stop()
        stack.remove()
    print(
        "\nRESULT: PASS - the journey and its negative cases behaved as the bank's controls "
        "require."
    )
    print(
        "Reminder: local and synthetic. No regulated identification and no qualified signature "
        "took place. The trust authority vetted nobody, and no licence was established."
    )


def main() -> int:
    try:
        run()
    except JourneyFailed as failure:
        print(f"\nRESULT: FAIL - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
