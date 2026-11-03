"""Phase 03: hardening of the bank-controlled customer ceremony (browser flow).

Server-side sessions with expiry and rotation; CSRF tokens bound to ONE session and ONE handoff
(and a pre-session token for the sign-in form); Sec-Fetch-Site / Origin checks as defence in
depth; agent credentials never accepted. The sign-in itself stays a DEV simulation.
"""

import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.api.customer_auth import LOGIN_COOKIE, SESSION_COOKIE
from app.services.customer_session_service import (
    CustomerSessionService,
    csrf_matches,
    csrf_token,
    hash_secret,
)
from tests.flow import INTENT, Flow, Started
from tests.test_handoff import new_catalog_version


def url(started: Started) -> str:
    return f"/customer/handoffs/{started.handoff_id}"


def post_confirm(flow: Flow, started: Started, client: TestClient | None = None, **form: str):  # noqa: ANN201
    browser = client or flow.customer_http
    data = {
        "token": started.nonce,
        "action_digest": started.action_digest,
        "csrf_token": flow.page_csrf(started, browser),
        **form,
    }
    return browser.post(f"{url(started)}/confirm", data=data)


def consents(flow: Flow) -> int:
    return flow.sql("SELECT COUNT(*) FROM consent_receipts")[0][0]


def csrf_refusals(flow: Flow, started: Started) -> int:
    return sum(
        1
        for e in flow.audit(started.application_id)
        if e.event_type == "handoff.rejected" and e.details.get("reason") == "csrf_token_invalid"
    )


# --- CSRF on the confirmation form ----------------------------------------------------------


def test_a_missing_csrf_token_is_refused_and_audited(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    response = post_confirm(flow, started, csrf_token="")
    assert response.status_code == 403 and "Confirmation recorded" not in response.text
    no_field = flow.customer_http.post(
        f"{url(started)}/confirm",
        data={"token": started.nonce, "action_digest": started.action_digest},
    )
    assert no_field.status_code == 403
    assert consents(flow) == 0
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert csrf_refusals(flow, started) == 2
    # A refused form must not burn the handoff: the real form still works.
    assert post_confirm(flow, started).status_code == 200


@pytest.mark.parametrize(
    "forged",
    [
        "A" * 43,  # well-formed, wrong
        "not-a-token",
        "A" * 44,
        "<script>",
    ],
)
def test_an_invalid_csrf_token_is_refused(flow: Flow, forged: str) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    assert post_confirm(flow, started, csrf_token=forged).status_code == 403
    assert consents(flow) == 0


def test_a_csrf_token_for_another_purpose_is_refused(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    wrong_purpose = csrf_token(secret, "login", started.handoff_id)
    assert post_confirm(flow, started, csrf_token=wrong_purpose).status_code == 403


def test_cross_session_substitution_is_refused(flow: Flow) -> None:
    started = flow.start()
    victim, attacker = TestClient(flow.app), TestClient(flow.app)
    flow.page_sign_in(started, client=victim)
    flow.page_sign_in(started, "cust-2", client=attacker)
    stolen = flow.page_csrf(started, victim)  # suppose the victim's form token leaked
    response = post_confirm(flow, started, client=attacker, csrf_token=stolen)
    assert response.status_code == 403  # bound to the victim's session, useless elsewhere
    assert consents(flow) == 0
    # And the attacker's own, valid form still cannot approve someone else's request.
    assert post_confirm(flow, started, client=attacker).status_code == 403
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "csrf_token_invalid" in reasons and "customer_mismatch" in reasons
    assert post_confirm(flow, started, client=victim).status_code == 200


def test_a_csrf_token_is_bound_to_one_handoff(flow: Flow) -> None:
    first = flow.start()
    second = flow.start(intent=INTENT.model_copy(update={"channel_id": "partner-1"}))
    flow.page_sign_in(first)
    token_for_first = flow.page_csrf(first)
    response = post_confirm(flow, second, csrf_token=token_for_first)
    assert response.status_code == 403
    assert flow.status_of(second.application_id) == "AWAITING_AUTHORIZATION"


def sign_in_again(flow: Flow, started: Started) -> None:
    """A browser that still holds a session submits a fresh sign-in form (e.g. a second tab)."""
    jar = flow.customer_http.cookies
    (old,) = [c for c in jar.jar if c.name == SESSION_COOKIE]
    jar.delete(SESSION_COOKIE, path="/customer")
    form = flow.customer_http.get(url(started), params={"token": started.nonce})
    login_csrf = re.search(r'name="login_csrf" value="([^"]+)"', form.text).group(1)  # type: ignore[union-attr]
    # ... the old session cookie is sent along with the new sign-in (same domain and path).
    jar.set(SESSION_COOKIE, old.value or "", domain=old.domain, path="/customer")
    response = flow.customer_http.post(
        "/customer/dev-login",
        data={
            "customer_id": "cust-1",
            "handoff_id": started.handoff_id,
            "token": started.nonce,
            "login_csrf": login_csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_csrf_token_of_a_rotated_session_is_dead(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    old_secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    old_csrf = flow.page_csrf(started)
    sign_in_again(flow, started)  # signing in again rotates the session
    new_secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    assert new_secret and new_secret != old_secret
    assert post_confirm(flow, started, csrf_token=old_csrf).status_code == 403
    replayed = TestClient(flow.app)
    replayed.cookies.set(SESSION_COOKIE, old_secret, path="/customer")
    assert post_confirm(flow, started, client=replayed, csrf_token=old_csrf).status_code == 401
    revoked = flow.sql(
        "SELECT revoked_at IS NOT NULL FROM customer_sessions ORDER BY created_at, session_id"
    )
    assert sorted(revoked) == [(0,), (1,)]
    rotated = flow.sql(
        "SELECT json_extract(details, '$.replaced_previous_session') FROM audit_events "
        "WHERE event_type = 'customer.session_started'"
    )
    assert sorted(rotated) == [(0,), (1,)]


def test_a_used_csrf_token_cannot_confirm_twice(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    csrf = flow.page_csrf(started)
    assert post_confirm(flow, started, csrf_token=csrf).status_code == 200
    again = post_confirm(flow, started, csrf_token=csrf)
    assert again.status_code == 409  # the single-use handoff is consumed
    assert consents(flow) == 1


# --- the sign-in form ------------------------------------------------------------------------


def test_the_sign_in_form_needs_its_own_pre_session_csrf_token(flow: Flow) -> None:
    started = flow.start()
    form = {"customer_id": "cust-1", "handoff_id": started.handoff_id, "token": started.nonce}
    blind = TestClient(flow.app).post("/customer/dev-login", data=form, follow_redirects=False)
    assert blind.status_code == 403 and "set-cookie" not in blind.headers  # login CSRF

    browser, other = TestClient(flow.app), TestClient(flow.app)
    page = browser.get(url(started), params={"token": started.nonce})
    other_page = other.get(url(started), params={"token": started.nonce})
    login_cookie = next(
        c for c in page.headers.get_list("set-cookie") if c.startswith(LOGIN_COOKIE)
    )
    assert all(
        flag in login_cookie.lower() for flag in ("httponly", "samesite=strict", "path=/customer")
    )
    mine = re.search(r'name="login_csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    theirs = re.search(r'name="login_csrf" value="([^"]+)"', other_page.text).group(1)  # type: ignore[union-attr]
    assert mine != theirs
    crossed = browser.post(
        "/customer/dev-login", data={**form, "login_csrf": theirs}, follow_redirects=False
    )
    assert crossed.status_code == 403
    ok = browser.post(
        "/customer/dev-login", data={**form, "login_csrf": mine}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert flow.sql("SELECT COUNT(*) FROM customer_sessions") == [(1,)]


# --- session expiry ---------------------------------------------------------------------------


def test_sessions_expire_on_the_server(flow_factory: Callable[..., Flow]) -> None:
    flow = flow_factory(handoff_ttl_seconds=3600)  # keep the handoff alive past the session
    started = flow.start()
    flow.page_sign_in(started)
    flow.clock.advance(seconds=899)
    assert (
        "Confirm and authorize"
        in flow.customer_http.get(url(started), params={"token": started.nonce}).text
    )
    flow.clock.advance(seconds=1)  # 900 s: the session is over, whatever the cookie says
    page = flow.customer_http.get(url(started), params={"token": started.nonce})
    assert "Sign in" in page.text and started.application_id not in page.text
    assert post_confirm(flow, started).status_code == 401
    assert consents(flow) == 0


# --- browser cross-site signals ------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # a sibling sub-domain is not this origin
        {"Origin": "null"},  # sandboxed / opaque origins
        {"Origin": "http://testserver.evil.example"},
    ],
)
def test_cross_site_signals_are_refused(flow: Flow, headers: dict[str, str]) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    response = flow.customer_http.post(
        f"{url(started)}/confirm",
        data={
            "token": started.nonce,
            "action_digest": started.action_digest,
            "csrf_token": flow.page_csrf(started),
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert consents(flow) == 0


def test_same_origin_browser_signals_are_accepted(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    response = flow.customer_http.post(
        f"{url(started)}/confirm",
        data={
            "token": started.nonce,
            "action_digest": started.action_digest,
            "csrf_token": flow.page_csrf(started),
        },
        headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
    )
    assert response.status_code == 200


# --- the link is never authentication ------------------------------------------------------


def test_a_signed_in_customer_cannot_open_someone_elses_link(flow: Flow) -> None:
    mine = flow.start()
    theirs = flow.start(
        actor_id="agent-b", intent=INTENT.model_copy(update={"customer_ref": "cust-2"})
    )
    flow.page_sign_in(mine)  # cust-1
    page = flow.customer_http.get(url(theirs), params={"token": theirs.nonce})
    assert page.status_code == 403
    for detail in (theirs.application_id, theirs.action_digest, "Confirm and authorize"):
        assert detail not in page.text
    assert post_confirm(flow, theirs).status_code == 403
    assert flow.status_of(theirs.application_id) == "AWAITING_AUTHORIZATION"


def test_the_nonce_alone_never_reaches_the_ceremony(flow: Flow) -> None:
    started = flow.start()
    anonymous = TestClient(flow.app)
    page = anonymous.get(url(started), params={"token": started.nonce})
    assert "Sign in" in page.text and started.action_digest not in page.text
    confirm = anonymous.post(
        f"{url(started)}/confirm",
        data={"token": started.nonce, "action_digest": started.action_digest},
    )
    assert confirm.status_code == 401
    assert consents(flow) == 0


# --- ceremony integrity on the page ----------------------------------------------------------


def test_parallel_form_confirmations_let_exactly_one_win(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    csrf = flow.page_csrf(started)
    secret = flow.customer_http.cookies.get(SESSION_COOKIE, path="/customer") or ""
    browsers = [TestClient(flow.app), TestClient(flow.app)]
    for browser in browsers:  # two tabs of the same signed-in browser
        browser.cookies.set(SESSION_COOKIE, secret, path="/customer")
    barrier = threading.Barrier(2)

    def press(i: int) -> int:
        barrier.wait()
        return post_confirm(flow, started, client=browsers[i], csrf_token=csrf).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(press, range(2)))
    assert codes == [200, 409]
    assert consents(flow) == 1


def test_a_new_document_version_blocks_the_page_confirmation(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    flow.container.catalog = new_catalog_version()  # the disclosure document is now v2.0
    response = post_confirm(flow, started)
    assert response.status_code == 409
    assert consents(flow) == 0
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "action_no_longer_current" in reasons


def test_a_tampered_product_in_the_digest_is_refused(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    other = flow.start(intent=INTENT.model_copy(update={"channel_id": "partner-1"}))
    response = post_confirm(flow, started, action_digest=other.action_digest)
    assert response.status_code == 409
    assert consents(flow) == 0


# --- the building blocks -------------------------------------------------------------------


def test_csrf_tokens_change_with_every_input_and_reject_garbage() -> None:
    base = csrf_token("s" * 47, "confirm", "hnd-1")
    assert len(base) == 43 and base == csrf_token("s" * 47, "confirm", "hnd-1")
    assert base != csrf_token("t" * 47, "confirm", "hnd-1")  # other session
    assert base != csrf_token("s" * 47, "login", "hnd-1")  # other purpose
    assert base != csrf_token("s" * 47, "confirm", "hnd-2")  # other handoff
    assert csrf_matches("s" * 47, "confirm", "hnd-1", base)
    for presented in (None, "", base[:-1], base + "A", base.upper()):
        assert not csrf_matches("s" * 47, "confirm", "hnd-1", presented)
    assert not csrf_matches(None, "confirm", "hnd-1", base)


def test_session_service_stores_hashes_and_resolves_only_active_sessions(flow: Flow) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork

    factory = flow.app.state.session_factory
    sessions = CustomerSessionService(lambda: SqlAlchemyUnitOfWork(factory), clock=flow.clock)
    record, secret = sessions.start(customer_id="cust-1", replaces=None, correlation_id="c")
    assert record.secret_hash == hash_secret(secret) and secret.startswith("tqs_")
    assert sessions.resolve(secret) == record
    for garbage in (None, "", "tqs_" + "A" * 43, secret + "x", hash_secret(secret)):
        assert sessions.resolve(garbage) is None
    _, rotated = sessions.start(customer_id="cust-1", replaces=secret, correlation_id="c")
    assert sessions.resolve(secret) is None and sessions.resolve(rotated) is not None
    flow.clock.advance(seconds=900)
    assert sessions.resolve(rotated) is None
