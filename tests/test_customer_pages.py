"""The bank-controlled confirmation page (HTML) and its DEV-only simulated sign-in."""

import re

import pytest
from fastapi.testclient import TestClient

from app.api import pages
from app.api.customer_auth import LOGIN_COOKIE, SESSION_COOKIE
from app.core.config import Environment, Settings
from app.main import create_app
from app.services.customer_session_service import hash_secret
from app.services.onboarding_service import HandoffReview
from tests.flow import Flow, Started


def page_url(started: Started) -> str:
    return f"/customer/handoffs/{started.handoff_id}"


def sign_in(flow: Flow, started: Started, customer_id: str = "cust-1", **kwargs):  # noqa: ANN003, ANN201
    """Like a browser: open the link (sign-in form + its login-CSRF cookie), submit the form."""
    form = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    login_csrf = re.search(r'name="login_csrf" value="([^"]*)"', form.text)
    return flow.customer_http.post(
        "/customer/dev-login",
        data={
            "customer_id": customer_id,
            "handoff_id": started.handoff_id,
            "token": started.nonce,
            "login_csrf": login_csrf.group(1) if login_csrf else "",
        },
        follow_redirects=False,
        **kwargs,
    )


def digest_in(html: str) -> str:
    match = re.search(r'name="action_digest" value="([0-9a-f]{64})"', html)
    assert match, "no confirmation form on the page"
    return match.group(1)


def confirm_form(flow: Flow, started: Started, **overrides: str):  # noqa: ANN201
    form = {
        "token": started.nonce,
        "action_digest": started.action_digest,
        "csrf_token": flow.page_csrf(started),  # what the page embedded for THIS session
        **overrides,
    }
    return flow.customer_http.post(f"{page_url(started)}/confirm", data=form)


def session_cookie_line(response) -> str:  # noqa: ANN001
    lines = [c for c in response.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)]
    assert len(lines) == 1, response.headers.get_list("set-cookie")
    return lines[0].lower()


# --- what the customer sees --------------------------------------------------


def test_review_page_shows_everything_the_customer_is_approving(flow: Flow) -> None:
    started = flow.start()
    assert sign_in(flow, started).status_code == 303
    page = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    assert page.status_code == 200
    html = page.text
    for expected in (
        "Example Depot Bank AG (MOCK)",  # provider
        "Basic Securities Account (MOCK)",  # product
        "Direct sales (MOCK)",  # distribution channel
        started.application_id,  # application reference
        "Open a securities account",  # exact requested action
        "kid-depot-basic",  # disclosure document ...
        "<strong>1.0</strong>",  # ... and its version
        started.action_digest,  # the digest being approved
        "MOCK RISK DISCLOSURE",  # the disclosure text itself
        "Confirm and authorize",  # the confirmation action
        "DEVELOPMENT PROTOTYPE",  # honest labelling
        "cust-1",  # who is (simulated-)signed in
    ):
        assert expected in html, expected
    assert digest_in(html) == started.action_digest
    assert "cannot confirm it for you" in html  # tells the customer the agent has no say
    assert f'name="csrf_token" value="{flow.page_csrf(started)}"' in html  # session-bound


def test_pages_carry_strict_security_headers(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    page = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "form-action 'self'" in page.headers["content-security-policy"]
    assert "<script" not in page.text.lower()  # no JavaScript at all


def test_the_confirm_button_completes_the_ceremony_end_to_end(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    page = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    result = confirm_form(flow, started, action_digest=digest_in(page.text))
    assert result.status_code == 200 and "Confirmation recorded" in result.text
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT customer_id FROM consent_receipts") == [("cust-1",)]


# --- the simulated sign-in --------------------------------------------------


def test_the_session_cookie_is_httponly_samesite_strict_scoped_and_expiring(flow: Flow) -> None:
    started = flow.start()
    cookie = session_cookie_line(sign_in(flow, started))
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/customer" in cookie
    assert "max-age=900" in cookie  # the browser forgets it; the server enforces it anyway
    assert "secure" not in cookie.replace("samesite", "")  # plain http in tests: not Secure


def test_the_session_cookie_is_secure_when_served_over_https(flow: Flow) -> None:
    started = flow.start()
    browser = TestClient(flow.app, base_url="https://testserver")
    response = flow.page_sign_in(started, client=browser)
    assert response.status_code == 303
    assert "; secure" in session_cookie_line(response)


def test_a_forged_session_cookie_is_not_a_sign_in(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    real = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    for forged in (
        "cust-1",
        "cust-1.deadbeef",  # the Phase 02 cookie format
        "tqs_" + "A" * 43,  # well-formed, never issued
        hash_secret(real),  # the stored hash is not the secret
        real[:-1] + ("A" if real[-1] != "A" else "B"),  # one character changed
    ):
        client = TestClient(flow.app)
        client.cookies.set(SESSION_COOKIE, forged, path="/customer")
        page = client.get(page_url(started), params={"token": started.nonce})
        assert "Sign in" in page.text and started.application_id not in page.text, forged
        assert (
            client.post(
                f"{page_url(started)}/confirm",
                data={"token": started.nonce, "action_digest": started.action_digest},
            ).status_code
            == 401
        )
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_only_the_hash_of_the_session_secret_is_stored(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    assert secret.startswith("tqs_")
    ((stored_hash, customer),) = flow.sql("SELECT secret_hash, customer_id FROM customer_sessions")
    assert (stored_hash, customer) == (hash_secret(secret), "cust-1")
    for table in ("customer_sessions", "audit_events"):
        assert secret not in repr(flow.sql(f"SELECT * FROM {table}")), table  # noqa: S608


def test_sign_in_requires_a_valid_customer_id(flow: Flow) -> None:
    started = flow.start()
    for bad in ("", "has space", "x" * 65, "a;b", "<b>"):
        response = sign_in(flow, started, customer_id=bad)
        assert response.status_code == 400, bad
        assert "set-cookie" not in response.headers
    assert flow.sql("SELECT COUNT(*) FROM customer_sessions") == [(0,)]


# --- wrong customer / bad link / reuse / expiry -----------------------------------------


def test_the_wrong_customer_is_shown_nothing_and_cannot_confirm(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started, customer_id="cust-2")
    page = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    assert page.status_code == 403
    for detail in (
        "Example Depot Bank",
        started.application_id,
        started.action_digest,
        "Confirm and authorize",
    ):
        assert detail not in page.text
    refused = confirm_form(flow, started)  # even with cust-2's own valid CSRF token
    assert refused.status_code == 403 and "Confirmation recorded" not in refused.text
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "customer_mismatch" in reasons


def test_a_malformed_link_token_is_rejected_before_anything_happens(flow: Flow) -> None:
    started = flow.start()
    for token in ("", "short", "bad token with spaces!!!!", "<script>alert(1)</script>"):
        page = flow.customer_http.get(page_url(started), params={"token": token})
        assert page.status_code == 403 and "<script>alert" not in page.text


def test_a_wrong_token_on_a_signed_in_session_is_refused(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    page = flow.customer_http.get(page_url(started), params={"token": "A" * 43})
    assert page.status_code == 403 and started.application_id not in page.text


def test_an_expired_link_shows_an_expiry_message(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    flow.clock.advance(seconds=601)
    page = flow.customer_http.get(page_url(started), params={"token": started.nonce})
    assert page.status_code == 410 and "expired" in page.text.lower()
    assert confirm_form(flow, started).status_code == 410


def test_pressing_confirm_twice_is_refused_the_second_time(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    assert confirm_form(flow, started).status_code == 200
    again = confirm_form(flow, started)  # same session, same (still valid) CSRF token
    assert again.status_code == 409
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]


def test_a_tampered_digest_in_the_form_is_refused(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    assert confirm_form(flow, started, action_digest="0" * 64).status_code == 409
    assert confirm_form(flow, started, action_digest="not-hex").status_code == 400
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


# --- cross-site and oversized requests --------------------------------------------------


def test_cross_site_form_posts_are_rejected(flow: Flow) -> None:
    started = flow.start()
    evil = {"Origin": "https://evil.example"}
    assert sign_in(flow, started, headers=evil).status_code == 403
    sign_in(flow, started)  # a legitimate sign-in exists ...
    csrf = flow.page_csrf(started)
    forged = flow.customer_http.post(
        f"{page_url(started)}/confirm",
        data={"token": started.nonce, "action_digest": started.action_digest, "csrf_token": csrf},
        headers=evil,
    )
    assert forged.status_code == 403  # ... but a request from another site cannot use it
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    same_origin = flow.customer_http.post(
        f"{page_url(started)}/confirm",
        data={"token": started.nonce, "action_digest": started.action_digest, "csrf_token": csrf},
        headers={"Origin": "http://testserver"},
    )
    assert same_origin.status_code == 200


def test_oversized_form_bodies_are_ignored(flow: Flow) -> None:
    started = flow.start()
    sign_in(flow, started)
    response = flow.customer_http.post(
        f"{page_url(started)}/confirm", content=b"token=" + b"A" * 10_000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )  # fmt: skip
    assert response.status_code == 400
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


# --- output escaping --------------------------------------------------


def test_every_dynamic_value_is_html_escaped() -> None:
    from datetime import UTC, datetime, timedelta

    from app.domain.application import DepotApplication
    from app.domain.handoff import ApprovedAction, Handoff

    now = datetime(2026, 11, 3, tzinfo=UTC)
    action = ApprovedAction(
        application_id="app-1", provider_id="p", product_id="q", channel_id="c",
        customer_ref="cust-1", catalog_version="1", document_id="doc", document_version="<i>1</i>",
        document_digest="a" * 64,
    )  # fmt: skip
    handoff = Handoff(
        handoff_id="h-1", application_id="app-1", nonce_hash="b" * 64, action=action,
        action_digest=action.digest(), created_at=now, expires_at=now + timedelta(minutes=1),
    )  # fmt: skip
    application = DepotApplication(
        application_id="app-1", provider_id="p", product_id="q", channel_id="c",
        customer_ref="cust-1", agent_actor_id="a", created_at=now, updated_at=now,
    )  # fmt: skip
    html = pages.review_page(
        review=HandoffReview(handoff, application, "<script>steal()</script>"),
        customer_id="<img src=x onerror=1>",
        token='"><script>x</script>',  # noqa: S106 - hostile test input
        provider_name="<b>Evil</b>",
        product_name="<u>P</u>",
        channel_label="'; DROP TABLE",
        csrf_token='"><img src=y>',  # noqa: S106 - hostile test input
    )
    for raw in (
        "<script>", "<b>Evil</b>", "<u>P</u>", "<img src=x", "<i>1</i>", '"><script>', "<img src=y",
    ):  # fmt: skip
        assert raw not in html, raw
    assert "&lt;script&gt;" in html and "&lt;b&gt;Evil&lt;/b&gt;" in html
    sign_in_html = pages.sign_in_page("h", "t", error="<script>", login_csrf='"><b>x</b>')
    assert "&lt;script&gt;" in sign_in_html and "<b>x</b>" not in sign_in_html


# --- the dev sign-in does not exist in production ---------------------------------------


def test_in_production_the_customer_pages_do_not_offer_a_sign_in(db_url: str) -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION, database_url=db_url)
    with TestClient(create_app(settings)) as prod:
        token = "A" * 43
        page = prod.get("/customer/handoffs/hnd-1", params={"token": token})
        assert page.status_code == 401 and "Sign in" not in page.text and "<form" not in page.text
        assert LOGIN_COOKIE not in page.headers.get("set-cookie", "")
        login = prod.post(
            "/customer/dev-login",
            data={"customer_id": "cust-1", "handoff_id": "hnd-1", "token": token},
        )
        assert login.status_code == 401 and "set-cookie" not in login.headers
        assert (
            prod.post(
                "/customer/handoffs/hnd-1/confirm", data={"token": token, "action_digest": "a" * 64}
            ).status_code
            == 401
        )


@pytest.mark.parametrize("path", ["/customer/handoffs/hnd-1", "/customer/dev-login"])
def test_customer_pages_are_not_in_the_public_openapi_contract(flow: Flow, path: str) -> None:
    assert path not in flow.app.openapi()["paths"]
