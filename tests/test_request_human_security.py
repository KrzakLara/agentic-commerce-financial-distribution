"""Phase 04 finalization: asking for a person needs the customer's SERVER-SIDE session and its
CSRF token - on the JSON API and on the "Speak to a person" button - never a
self-asserted header and never an agent token. Nothing about internal reviews leaks, and the
request cannot move an application past a mandatory check."""

import re

import pytest
from fastapi.testclient import TestClient

from app.api.customer_auth import API_SESSION_COOKIE, SESSION_COOKIE
from app.api.pages import HUMAN_REQUEST_BUTTON
from app.services.customer_session_service import csrf_token
from tests.flow import (
    INTENT,
    Flow,
    Started,
    api_session_secret,
    customer_headers,
    human_csrf,
    sign_in_browser,
)

REVIEW_CUSTOMER = "synthetic-review-1"
REVIEW_INTENT = INTENT.model_copy(update={"customer_ref": REVIEW_CUSTOMER})


def api(app_id: str) -> str:
    return f"/v1/customer/applications/{app_id}/request-human"


def button(app_id: str) -> str:
    return f"/customer/applications/{app_id}/request-human"


def post_api(client: TestClient, app_id: str, headers: dict[str, str]):  # noqa: ANN201
    return client.post(api(app_id), json={"category": "QUESTION_ABOUT_PRODUCT"}, headers=headers)


def press_button(  # noqa: ANN201
    client: TestClient,
    app_id: str,
    csrf: str,
    category: str = "QUESTION_ABOUT_PRODUCT",
    **headers: str,
):
    return client.post(
        button(app_id), data={"csrf_token": csrf, "category": category}, headers=headers
    )


def page_human_csrf(page_html: str, app_id: str) -> str:
    """The CSRF token inside the page's "Speak to a person" form."""
    match = re.search(
        rf'action="/customer/applications/{re.escape(app_id)}/request-human">'
        r'<input type="hidden" name="csrf_token" value="([^"]+)">',
        page_html,
    )
    assert match, "no request-human form on the page"
    return match.group(1)


def cases(flow: Flow, app_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT reason, status, interrupted_step FROM review_cases WHERE application_id = :a "
        "ORDER BY rowid",
        a=app_id,
    )


def denial_reasons(flow: Flow, app_id: str) -> list[str]:
    return [
        e.details["reason"]
        for e in flow.audit(app_id)
        if e.event_type == "review.human_request_denied"
    ]


def rotate_session(browser: TestClient, customer_id: str) -> None:
    """A browser that still holds a session submits a fresh sign-in (e.g. a second tab): the
    sign-in form is fetched without the session, then posted WITH it, so the server rotates."""
    jar = browser.cookies
    (old,) = [c for c in jar.jar if c.name == SESSION_COOKIE]
    jar.delete(SESSION_COOKIE, path="/customer")
    handoff_id, token = "hnd-sign-in", "t" * 24
    form = browser.get(f"/customer/handoffs/{handoff_id}", params={"token": token})
    login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', form.text).group(1)  # type: ignore[union-attr]
    jar.set(SESSION_COOKIE, old.value or "", domain=old.domain, path="/customer")
    response = browser.post(
        "/customer/dev-login",
        data={"customer_id": customer_id, "handoff_id": handoff_id, "token": token,
              "login_csrf": login_csrf},
        follow_redirects=False,
    )  # fmt: skip
    assert response.status_code == 303


def review_page(flow: Flow, started: Started, client: TestClient | None = None):  # noqa: ANN201
    browser = client or flow.customer_http
    return browser.get(f"/customer/handoffs/{started.handoff_id}", params={"token": started.nonce})


# --- the JSON API needs the session and its CSRF token ------------------------------------------


def test_a_signed_in_customer_with_the_pages_csrf_token_is_accepted(flow: Flow) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser("cust-1")
    response = post_api(browser, app_id, {"X-CSRF-Token": human_csrf(browser, app_id)})
    assert response.status_code == 202 and response.json()["status"] == "RECEIVED"
    assert cases(flow, app_id) == [("CUSTOMER_REQUESTED_HUMAN", "OPEN", "AWAITING_AUTHORIZATION")]
    opened = [e for e in flow.audit(app_id) if e.event_type == "review.case_opened"]
    assert [(e.actor_type.value, e.actor_id) for e in opened] == [("customer", "cust-1")]


def test_without_a_customer_session_the_request_is_refused(flow: Flow) -> None:
    app_id = flow.start().application_id
    anonymous = TestClient(flow.app)
    response = post_api(
        anonymous, app_id, {"X-CSRF-Token": csrf_token("tqs_guess", "human", app_id)}
    )
    assert response.status_code == 401
    assert cases(flow, app_id) == [] and flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert denial_reasons(flow, app_id) == ["no_customer_session"]


def test_a_self_asserted_customer_header_is_not_a_session(flow: Flow) -> None:
    app_id = flow.start().application_id
    header_only = post_api(TestClient(flow.app), app_id, customer_headers("cust-1"))
    assert header_only.status_code == 401
    # ... and next to ANOTHER customer's real session it changes nothing: the session decides.
    other = flow.customer_browser("cust-2")
    response = post_api(
        other, app_id, {**customer_headers("cust-1"), "X-CSRF-Token": human_csrf(other, app_id)}
    )
    assert response.status_code == 404
    assert cases(flow, app_id) == []
    assert denial_reasons(flow, app_id) == ["no_customer_session", "not_the_customer"]


def test_an_expired_session_is_refused(flow: Flow) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser("cust-1")
    csrf = human_csrf(browser, app_id)
    flow.clock.advance(seconds=901)  # the session lifetime is 900 s; the server enforces it
    assert post_api(browser, app_id, {"X-CSRF-Token": csrf}).status_code == 401
    assert cases(flow, app_id) == []
    assert denial_reasons(flow, app_id) == ["no_customer_session"]


def test_a_revoked_session_is_refused(flow: Flow) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser("cust-1")
    old_secret = api_session_secret(browser)
    old_csrf = human_csrf(browser, app_id)
    rotate_session(browser, "cust-1")  # signing in again rotates (revokes) the old session
    assert api_session_secret(browser) != old_secret
    replay = TestClient(flow.app)
    replay.cookies.set(API_SESSION_COOKIE, old_secret, path="/v1/customer")
    assert post_api(replay, app_id, {"X-CSRF-Token": old_csrf}).status_code == 401
    assert cases(flow, app_id) == []
    fresh = post_api(browser, app_id, {"X-CSRF-Token": human_csrf(browser, app_id)})
    assert fresh.status_code == 202  # the current session still works


@pytest.mark.parametrize(
    "token",
    ["missing", "malformed", "other_application", "other_purpose", "other_session"],
)
def test_a_missing_or_foreign_csrf_token_is_refused(flow: Flow, token: str) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser("cust-1")
    secret = api_session_secret(browser)
    second = TestClient(flow.app)
    sign_in_browser(second, "cust-1")  # the same customer, another session
    presented = {
        "missing": None,
        "malformed": "not-a-token",
        "other_application": csrf_token(secret, "human", "app-other"),
        "other_purpose": csrf_token(secret, "confirm", app_id),
        "other_session": human_csrf(second, app_id),
    }[token]
    headers = {} if presented is None else {"X-CSRF-Token": presented}
    response = post_api(browser, app_id, headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_token_invalid"
    assert cases(flow, app_id) == []
    assert denial_reasons(flow, app_id) == ["csrf_token_invalid"]


def test_a_cross_site_browser_request_is_refused(flow: Flow) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser("cust-1")
    csrf = human_csrf(browser, app_id)
    for hostile in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        response = post_api(browser, app_id, {"X-CSRF-Token": csrf, **hostile})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_site_request"
    assert cases(flow, app_id) == []
    assert denial_reasons(flow, app_id) == ["cross_site_request", "cross_site_request"]


def test_the_api_session_cookie_is_httponly_samesite_strict_scoped_and_expiring(
    flow: Flow,
) -> None:
    for base_url, secure in (("http://testserver", False), ("https://testserver", True)):
        browser = TestClient(flow.app, base_url=base_url)
        handoff_id, token = "hnd-sign-in", "t" * 24
        form = browser.get(f"/customer/handoffs/{handoff_id}", params={"token": token})
        login_csrf = re.search(r'name="login_csrf" value="([^"]*)"', form.text).group(1)  # type: ignore[union-attr]
        response = browser.post(
            "/customer/dev-login",
            data={"customer_id": "cust-1", "handoff_id": handoff_id, "token": token,
                  "login_csrf": login_csrf},
            follow_redirects=False,
        )  # fmt: skip
        lines = response.headers.get_list("set-cookie")
        (api_cookie,) = [c.lower() for c in lines if c.startswith(API_SESSION_COOKIE)]
        (page_cookie,) = [c.lower() for c in lines if c.startswith(SESSION_COOKIE)]
        for flag in ("httponly", "samesite=strict", "path=/v1/customer", "max-age=900"):
            assert flag in api_cookie, flag
        assert "path=/customer" in page_cookie  # the pages' cookie keeps its own scope
        assert ("; secure" in api_cookie) is secure  # Secure exactly when served over HTTPS
        same_secret = (
            api_cookie.split(";")[0].split("=", 1)[1] == page_cookie.split(";")[0].split("=", 1)[1]
        )
        assert same_secret  # one server-side session behind both cookies


# --- the "Speak to a person" button ----------------------------------------------------


def test_the_review_page_shows_the_button_to_the_signed_in_customer_only(flow: Flow) -> None:
    started = flow.start()
    anonymous = review_page(flow, started)
    assert HUMAN_REQUEST_BUTTON not in anonymous.text  # the link alone shows nothing
    flow.page_sign_in(started)
    page = review_page(flow, started)
    assert HUMAN_REQUEST_BUTTON in page.text
    secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    assert page_human_csrf(page.text, started.application_id) == csrf_token(
        secret, "human", started.application_id
    )
    for value in ("QUESTION_ABOUT_PRODUCT", "HELP_WITH_PROCESS", "OTHER"):
        assert f'<option value="{value}">' in page.text


def test_pressing_the_button_opens_a_case_with_a_neutral_acknowledgement(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    csrf = page_human_csrf(review_page(flow, started).text, started.application_id)
    response = press_button(flow.customer_http, started.application_id, csrf)
    assert response.status_code == 200 and "Request received" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert cases(flow, started.application_id) == [
        ("CUSTOMER_REQUESTED_HUMAN", "OPEN", "AWAITING_AUTHORIZATION")
    ]
    assert flow.status_of(started.application_id) == "MANUAL_REVIEW"
    assert flow.agent_state(started.application_id) == "ACTION_REQUIRED"


@pytest.mark.parametrize(
    "probe", ["no_session", "no_csrf", "csrf_of_other_application", "cross_site", "agent_token"]
)
def test_the_button_needs_the_session_and_its_csrf_token(flow: Flow, probe: str) -> None:
    started = flow.start()
    app_id = started.application_id
    flow.page_sign_in(started)
    csrf = page_human_csrf(review_page(flow, started).text, app_id)
    secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    client, sent, headers, status = flow.customer_http, csrf, {}, 403
    if probe == "no_session":
        client, status = TestClient(flow.app), 401
    elif probe == "no_csrf":
        sent = ""
    elif probe == "csrf_of_other_application":
        sent = csrf_token(secret, "human", "app-other")
    elif probe == "cross_site":
        headers = {"Origin": "https://evil.example"}
    else:
        headers, status = flow.onboarding_headers(app_id), 401
    response = press_button(client, app_id, sent, **headers)
    assert response.status_code == status
    assert "Request received" not in response.text
    assert cases(flow, app_id) == [] and flow.status_of(app_id) == "AWAITING_AUTHORIZATION"


def test_the_button_accepts_only_the_fixed_categories(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    csrf = page_human_csrf(review_page(flow, started).text, started.application_id)
    response = press_button(flow.customer_http, started.application_id, csrf, "APPROVE_ME")
    assert response.status_code == 400
    assert cases(flow, started.application_id) == []


def test_repeated_requests_are_idempotent(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    flow.page_sign_in(started)
    csrf = page_human_csrf(review_page(flow, started).text, app_id)
    pages = [press_button(flow.customer_http, app_id, csrf) for _ in range(2)]
    browser = flow.customer_browser("cust-1")
    api_ack = post_api(browser, app_id, {"X-CSRF-Token": human_csrf(browser, app_id)})
    assert [p.status_code for p in pages] == [200, 200] and api_ack.status_code == 202
    assert pages[0].text == pages[1].text
    assert len(cases(flow, app_id)) == 1  # one active case, however often the customer asks
    types = flow.event_types(app_id)
    assert types.count("review.case_opened") == 1 and types.count("review.human_requested") == 2
    assert flow.status_of(app_id) == "MANUAL_REVIEW"


# --- no leakage, no bypass -----------------------------------------------------------------------


def confirmed_on_the_page(
    flow: Flow, intent=INTENT, customer_id: str = "cust-1", agent: str = "agent-a"
):  # noqa: ANN001, ANN201
    """Sign in on the bank page, confirm there; returns (started, confirmation page)."""
    started = flow.start(intent=intent, actor_id=agent)
    browser = TestClient(flow.app)
    flow.page_sign_in(started, customer_id, client=browser)
    page = review_page(flow, started, browser)
    digest = re.search(r'name="action_digest" value="([0-9a-f]{64})"', page.text).group(1)  # type: ignore[union-attr]
    confirm = browser.post(
        f"/customer/handoffs/{started.handoff_id}/confirm",
        data={"token": started.nonce, "action_digest": digest,
              "csrf_token": flow.page_csrf(started, browser)},
    )  # fmt: skip
    assert confirm.status_code == 200 and "Confirmation recorded" in confirm.text
    return started, confirm, browser


def test_the_confirmation_page_is_identical_whether_or_not_compliance_reviews(flow: Flow) -> None:
    """No tipping-off: its status line is fixed text, so a running review cannot show through."""

    def without_ids(started, page) -> str:  # noqa: ANN001
        fixed = re.sub(r'name="csrf_token" value="[^"]+"', "", page.text)
        return fixed.replace(started.application_id, "<application>")

    plain, plain_page, _ = confirmed_on_the_page(flow, customer_id="cust-1")
    flagged, flagged_page, _ = confirmed_on_the_page(
        flow, REVIEW_INTENT, REVIEW_CUSTOMER, agent="agent-b"
    )
    assert flow.status_of(plain.application_id) == "CONTRACT_PENDING"
    assert flow.status_of(flagged.application_id) == "MANUAL_REVIEW"  # compliance asked
    assert without_ids(plain, plain_page) == without_ids(flagged, flagged_page)
    assert "Step 3 of 3" in flagged_page.text


def test_the_acknowledgement_is_identical_whether_or_not_compliance_reviews(flow: Flow) -> None:
    """No tipping-off: the button's answer for an application under compliance review is
    byte-for-byte the answer for an ordinary one."""
    plain, plain_page, plain_browser = confirmed_on_the_page(flow, customer_id="cust-1")
    flagged, flagged_page, flagged_browser = confirmed_on_the_page(
        flow, REVIEW_INTENT, REVIEW_CUSTOMER, agent="agent-b"
    )
    assert flow.status_of(flagged.application_id) == "MANUAL_REVIEW"  # compliance asked
    answers = []
    for started, page, browser in (
        (plain, plain_page, plain_browser), (flagged, flagged_page, flagged_browser),
    ):  # fmt: skip
        assert HUMAN_REQUEST_BUTTON in page.text  # offered after the confirmation, too
        csrf = page_human_csrf(page.text, started.application_id)
        answers.append(press_button(browser, started.application_id, csrf))
    assert answers[0].status_code == answers[1].status_code == 200
    assert answers[0].text == answers[1].text
    for word in ("compliance", "aml", "review", "case", "suspicious", "verdacht", "pruefung"):
        assert word not in answers[1].text.lower()
    assert [row[0] for row in cases(flow, flagged.application_id)] == [
        "COMPLIANCE_REVIEW_REQUIRED"
    ]  # joined the existing case - no second one


def test_asking_for_a_person_never_bypasses_the_mandatory_checks(flow: Flow) -> None:
    started, page, browser = confirmed_on_the_page(flow, REVIEW_INTENT, REVIEW_CUSTOMER)
    app_id = started.application_id
    csrf = page_human_csrf(page.text, app_id)
    assert press_button(browser, app_id, csrf).status_code == 200
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    assert flow.sql(
        "SELECT resume_status FROM applications WHERE application_id = :a", a=app_id
    ) == [("COMPLIANCE_PENDING",)]
    submit = flow.agent_http.post(
        f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
    )
    assert submit.status_code == 409  # the compliance result still blocks
    assert flow.provider_confirms(app_id).status_code == 403  # and nothing can open it
    assert flow.sql(
        "SELECT COUNT(*) FROM check_results WHERE application_id = :a AND check_type = 'CONTRACT'",
        a=app_id,
    ) == [(0,)]
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
