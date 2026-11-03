"""Phase 03: scoped, short-lived, revocable agent authorization at the API boundary.

Every protected agent route needs a bearer token that (1) exists server-side (hash lookup),
(2) is unexpired, unrevoked, from our issuer, for our audience, (3) carries the operation's
scope, and (4) is bound to exactly the application in the path. Every failure is fail-closed,
generic towards the agent, and audited (without the token).
"""

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import Environment, Settings
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import MandateScope
from app.domain.mandate import AGENT_API_AUDIENCE, AUTHORIZATION_ISSUER, AgentAccessToken
from app.domain.workflow import TransitionAuthority as A
from app.main import create_app
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import hash_token
from scripts import dev_auth
from tests.conftest import bootstrap_headers
from tests.flow import CALLBACK_PATH, FORGED_TOKEN, INTENT, Flow, bearer, customer_headers

ROOT = Path(__file__).resolve().parent.parent
BODY = INTENT.model_dump()


def create(flow: Flow, token: str, body: dict | None = None, key: str | None = None):  # noqa: ANN201
    return flow.agent_http.post(
        "/v1/applications",
        json=body or BODY,
        headers={**bearer(token), "Idempotency-Key": key or str(uuid.uuid4())},
    )


def grant_id(flow: Flow, application_id: str, kind: str) -> str:
    ((mandate_id,),) = flow.sql(
        "SELECT mandate_id FROM mandates WHERE application_id = :a AND kind = :k",
        a=application_id,
        k=kind,
    )
    return mandate_id


def plant_token(
    flow: Flow,
    mandate_id: str,
    *,
    scopes: frozenset[MandateScope] = frozenset({MandateScope.READ_STATUS}),
    audience: str = AGENT_API_AUDIENCE,
    issuer: str = AUTHORIZATION_ISSUER,
) -> str:
    """Write a token record directly (as a buggy or malicious issuer would) and return it."""
    raw = "tqa_" + secrets.token_urlsafe(32)
    now = flow.clock()
    with SqlAlchemyUnitOfWork(flow.app.state.session_factory) as uow:
        uow.agent_tokens.add(
            AgentAccessToken(
                jti=f"jti-{uuid.uuid4().hex}",
                token_hash=hash_token(raw),
                mandate_id=mandate_id,
                issuer=issuer,
                audience=audience,
                scopes=scopes,
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        uow.commit()
    return raw


def denial_reasons(flow: Flow, application_id: str | None = None) -> list[str]:
    if application_id is None:
        rows = flow.sql(
            "SELECT details FROM audit_events WHERE event_type = 'authorization.denied'"
        )
    else:
        rows = flow.sql(
            "SELECT details FROM audit_events WHERE event_type = 'authorization.denied' "
            "AND application_id = :a",
            a=application_id,
        )
    return [json.loads(d)["reason"] for (d,) in rows]


# --- valid scope + resource -----------------------------------------------------------------


def test_each_operation_works_with_exactly_its_own_token(flow: Flow) -> None:
    started = flow.start()  # create_draft (bootstrap) + request_handoff (onboarding) + read
    app_id = started.application_id
    assert (
        flow.agent_http.get(
            f"/v1/applications/{app_id}", headers=flow.status_headers(app_id)
        ).status_code
        == 200
    )
    assert flow.customer_confirm(started).status_code == 200
    submitted = flow.agent_http.post(
        f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
    )
    assert submitted.status_code == 202


# --- missing / invalid ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/applications"),
        ("get", "/v1/applications/app-1"),
        ("post", "/v1/applications/app-1/handoff"),
        ("post", "/v1/applications/app-1/submit"),
    ],
)
def test_every_protected_route_needs_a_bearer_token(flow: Flow, method: str, path: str) -> None:
    response = flow.agent_http.request(
        method, path, json=BODY, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == 'Bearer realm="trusteq-agent-api"'


@pytest.mark.parametrize(
    "value",
    [
        FORGED_TOKEN,  # well-formed, never issued
        "tqs_" + "A" * 43,  # a customer-session-shaped secret
        "tqa_" + "A" * 42,  # truncated
        "tqa_" + "A" * 44,  # extended
        "Bearer tqa_" + "A" * 43,  # double scheme
        "tqa_" + "A" * 40 + "%00x",  # control/odd characters
        "x" * 5000,  # oversized
    ],
)
def test_invalid_tokens_are_rejected_without_detail(flow: Flow, value: str) -> None:
    response = flow.agent_http.get("/v1/applications/app-1", headers=bearer(value))
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "invalid_token",
        "message": "The access token is invalid, expired or revoked.",
        "correlation_id": response.headers["x-correlation-id"],
    }
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


def test_a_modified_valid_token_is_rejected(flow: Flow) -> None:
    app_id = flow.start().application_id
    token = flow.status_headers(app_id)["Authorization"].removeprefix("Bearer ")
    last = token[-1]
    tampered = token[:-1] + ("B" if last != "B" else "C")
    assert (
        flow.agent_http.get(f"/v1/applications/{app_id}", headers=bearer(tampered)).status_code
        == 401
    )
    assert (
        flow.agent_http.get(f"/v1/applications/{app_id}", headers=bearer(token.upper())).status_code
        == 401
    )


# --- expired / revoked ---------------------------------------------------------------------


def test_an_expired_bootstrap_token_cannot_create(flow: Flow) -> None:
    token = flow.bootstrap()
    flow.clock.advance(seconds=900)  # exactly at expiry: closed
    response = create(flow, token)
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(0,)]
    assert denial_reasons(flow) == ["token_expired"]


def test_application_tokens_expire_and_the_status_token_lives_longer(flow: Flow) -> None:
    app_id = flow.start().application_id
    flow.clock.advance(minutes=59)
    assert (
        flow.agent_http.post(
            f"/v1/applications/{app_id}/handoff", headers=flow.onboarding_headers(app_id)
        ).status_code
        == 201
    )
    flow.clock.advance(minutes=1)  # onboarding grant (1 h) is over
    assert (
        flow.agent_http.post(
            f"/v1/applications/{app_id}/handoff", headers=flow.onboarding_headers(app_id)
        ).status_code
        == 401
    )
    assert flow.agent_state(app_id) == "AWAITING_CUSTOMER"  # status grant: 24 h
    flow.clock.advance(hours=23)
    status = flow.agent_http.get(f"/v1/applications/{app_id}", headers=flow.status_headers(app_id))
    assert status.status_code == 401
    assert denial_reasons(flow, app_id) == ["token_expired", "token_expired"]


def test_a_revoked_token_cannot_be_used_and_revocation_is_final(flow: Flow) -> None:
    app_id = flow.start().application_id
    ((jti,),) = flow.sql(
        "SELECT t.jti FROM agent_access_tokens t JOIN mandates m USING (mandate_id) "
        "WHERE m.application_id = :a AND m.kind = 'status'",
        a=app_id,
    )
    assert flow.authz().revoke_token(jti, correlation_id="corr-op") is True
    assert flow.authz().revoke_token(jti, correlation_id="corr-op") is False  # already revoked
    response = flow.agent_http.get(
        f"/v1/applications/{app_id}", headers=flow.status_headers(app_id)
    )
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert denial_reasons(flow, app_id) == ["token_revoked"]
    with pytest.raises(IntegrityError, match="stays revoked"):
        flow.execute("UPDATE agent_access_tokens SET revoked_at = NULL WHERE jti = :j", j=jti)
    # Other credentials are unaffected: revocation is per token.
    assert (
        flow.agent_http.post(
            f"/v1/applications/{app_id}/handoff", headers=flow.onboarding_headers(app_id)
        ).status_code
        == 201
    )


# --- issuer / audience ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"audience": "trusteq-customer-channel"}, "wrong_audience"),
        ({"audience": "some-other-api"}, "wrong_audience"),
        ({"issuer": "evil-issuer"}, "wrong_issuer"),
        ({"scopes": frozenset(MandateScope)}, "scope_exceeds_grant"),
    ],
)
def test_tokens_for_another_audience_issuer_or_beyond_their_grant_are_rejected(
    flow: Flow, overrides: dict, reason: str
) -> None:
    app_id = flow.start().application_id
    raw = plant_token(flow, grant_id(flow, app_id, "status"), **overrides)
    response = flow.agent_http.get(f"/v1/applications/{app_id}", headers=bearer(raw))
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert denial_reasons(flow, app_id) == [reason]
    assert "audience" not in response.text and "issuer" not in response.text  # nothing leaks


# --- wrong scope / application / actor -----------------------------------------------------


def test_each_token_is_refused_outside_its_scope(flow: Flow) -> None:
    bootstrap = flow.bootstrap()
    started = flow.start()
    app_id = started.application_id
    onboarding, status = flow.onboarding_headers(app_id), flow.status_headers(app_id)
    refused = [
        ("get", f"/v1/applications/{app_id}", onboarding),  # onboarding cannot read
        ("post", f"/v1/applications/{app_id}/handoff", status),  # read-only cannot act
        ("post", f"/v1/applications/{app_id}/submit", status),
        ("get", f"/v1/applications/{app_id}", bearer(bootstrap)),  # bootstrap: create only
        ("post", f"/v1/applications/{app_id}/submit", bearer(bootstrap)),
        ("post", "/v1/applications", onboarding),  # onboarding cannot create more
        ("post", "/v1/applications", status),
    ]
    for method, path, auth in refused:
        response = flow.agent_http.request(
            method, path, json=BODY, headers={**auth, "Idempotency-Key": str(uuid.uuid4())}
        )
        assert response.status_code == 403, (method, path)
        assert response.json()["error"]["code"] == "insufficient_scope"
        assert 'error="insufficient_scope"' in response.headers["www-authenticate"]
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(1,)]
    assert denial_reasons(flow).count("insufficient_scope") == len(refused)


def test_a_token_for_one_application_cannot_touch_another_of_the_same_agent(flow: Flow) -> None:
    first = flow.start()
    second = flow.start(intent=INTENT.model_copy(update={"customer_ref": "cust-2"}))
    for method, path, auth in (
        ("get", "", flow.status_headers(first.application_id)),
        ("post", "/handoff", flow.onboarding_headers(first.application_id)),
        ("post", "/submit", flow.onboarding_headers(first.application_id)),
    ):
        response = flow.agent_http.request(
            method, f"/v1/applications/{second.application_id}{path}", headers=auth
        )
        assert response.status_code == 404, path
    reasons = [
        e.details["reason"]
        for e in flow.audit(second.application_id)
        if e.event_type == "application.access_denied"
    ]
    assert reasons == ["outside_authorization"] * 3


def test_another_agents_valid_tokens_get_404_and_are_audited(flow: Flow) -> None:
    victim = flow.start()
    attacker = flow.start(
        actor_id="agent-b", intent=INTENT.model_copy(update={"customer_ref": "c9"})
    )
    for path, auth in (
        ("", flow.status_headers(attacker.application_id, "agent-b")),
        ("/handoff", flow.onboarding_headers(attacker.application_id, "agent-b")),
        ("/submit", flow.onboarding_headers(attacker.application_id, "agent-b")),
    ):
        method = "get" if not path else "post"
        response = flow.agent_http.request(
            method, f"/v1/applications/{victim.application_id}{path}", headers=auth
        )
        assert response.status_code == 404
        assert victim.application_id not in response.text
    denied = [
        e for e in flow.audit(victim.application_id) if e.event_type == "application.access_denied"
    ]
    assert {(e.actor_id, e.details["reason"]) for e in denied} == {("agent-b", "not_owner")}
    assert flow.status_of(victim.application_id) == "AWAITING_AUTHORIZATION"


# --- privilege escalation / self-authorization -----------------------------------------------


def test_a_bootstrap_grant_creates_exactly_one_application(flow: Flow) -> None:
    token = flow.bootstrap()
    key = str(uuid.uuid4())
    first = create(flow, token, key=key)
    replay = create(flow, token, key=key)  # idempotent retry: same application, no new tokens
    other = create(flow, token, body={**BODY, "channel_id": "partner-1"})  # a second one: no
    assert (first.status_code, replay.status_code, other.status_code) == (201, 200, 403)
    assert replay.json()["application_id"] == first.json()["application_id"]
    assert replay.json()["authorization"] is None
    assert other.json()["error"]["code"] == "mandate_inactive"
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(1,)]
    reasons = flow.sql(
        "SELECT json_extract(details, '$.reason') FROM audit_events "
        "WHERE event_type = 'application.create_denied'"
    )
    assert reasons == [("bootstrap_already_used",)]


def test_the_compare_and_set_stops_a_second_use_even_when_the_pre_check_raced(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the real race: the second request's read still sees the grant UNUSED (stale), so
    only the atomic binding (`... WHERE application_id IS NULL`) stands between it and a
    second application."""
    from app.repositories.workflow_repositories import SqlAlchemyMandateRepository

    token = flow.bootstrap()
    ((grant,),) = flow.sql(
        "SELECT mandate_id FROM agent_access_tokens WHERE token_hash = :h", h=hash_token(token)
    )
    with SqlAlchemyUnitOfWork(flow.app.state.session_factory) as uow:
        unused = uow.mandates.get(grant)  # the grant as it looked before its first use
    assert unused is not None and unused.application_id is None
    assert create(flow, token).status_code == 201

    original = SqlAlchemyMandateRepository.get

    def stale_get(self, mandate_id):  # noqa: ANN001, ANN202
        return unused if mandate_id == grant else original(self, mandate_id)

    monkeypatch.setattr(SqlAlchemyMandateRepository, "get", stale_get)
    second = create(flow, token, body={**BODY, "channel_id": "partner-1"})
    monkeypatch.undo()
    assert second.status_code == 403 and second.json()["error"]["code"] == "mandate_inactive"
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(1,)]
    assert flow.sql("SELECT COUNT(*) FROM idempotency_records") == [(1,)]  # rolled back


def test_concurrent_creates_with_one_bootstrap_grant_yield_one_application(flow: Flow) -> None:
    token = flow.bootstrap()
    clients = [TestClient(flow.app) for _ in range(4)]
    barrier = threading.Barrier(4)

    def attempt(i: int) -> int:
        barrier.wait()
        response = clients[i].post(
            "/v1/applications",
            json={**BODY, "channel_id": "direct" if i % 2 else "partner-1"},
            headers={**bearer(token), "Idempotency-Key": str(uuid.uuid4())},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(attempt, range(4)))
    assert codes == [201, 403, 403, 403]
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(1,)]
    assert flow.sql("SELECT COUNT(*) FROM mandates WHERE kind = 'onboarding'") == [(1,)]


def test_the_agent_can_only_act_for_the_customer_its_grant_names(flow: Flow) -> None:
    token = flow.bootstrap(customer_id="cust-1")
    response = create(flow, token, body={**BODY, "customer_ref": "cust-somebody-else"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "mandate_inactive"
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(0,)]
    ok = create(flow, token)  # the grant was not burned by the refused attempt
    assert ok.status_code == 201


def test_grants_and_tokens_cannot_be_widened_even_with_raw_sql(flow: Flow) -> None:
    app_id = flow.start().application_id
    widen = [
        'UPDATE mandates SET scopes = \'["create_draft","read_status","request_handoff",'
        '"submit_after_consent"]\' WHERE application_id = :a',
        "UPDATE mandates SET expires_at = '2099-01-01 00:00:00' WHERE application_id = :a",
        "UPDATE mandates SET agent_actor_id = 'agent-evil' WHERE application_id = :a",
        "UPDATE mandates SET kind = 'onboarding' WHERE application_id = :a AND kind = 'status'",
        'UPDATE agent_access_tokens SET scopes = \'["read_status","submit_after_consent"]\' '
        "WHERE mandate_id IN (SELECT mandate_id FROM mandates WHERE application_id = :a)",
        "UPDATE agent_access_tokens SET expires_at = '2099-01-01 00:00:00' "
        "WHERE mandate_id IN (SELECT mandate_id FROM mandates WHERE application_id = :a)",
    ]
    for statement in widen:
        with pytest.raises(IntegrityError, match="immutable"):
            flow.execute(statement, a=app_id)
    other = flow.start(intent=INTENT.model_copy(update={"customer_ref": "cust-2"})).application_id
    with pytest.raises(IntegrityError, match="bound, once"):  # re-binding a used bootstrap
        flow.execute(
            "UPDATE mandates SET application_id = :o "
            "WHERE application_id = :a AND kind = 'bootstrap'",
            a=app_id,
            o=other,
        )


def test_the_agent_cannot_use_its_token_on_customer_or_internal_routes(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    auth = flow.onboarding_headers(app_id)
    page = flow.agent_http.get(
        f"/customer/handoffs/{started.handoff_id}", params={"token": started.nonce}, headers=auth
    )
    login = flow.agent_http.post(
        "/customer/dev-login",
        data={"customer_id": "cust-1", "handoff_id": started.handoff_id, "token": started.nonce},
        headers=auth,
    )
    form = flow.agent_http.post(
        f"/customer/handoffs/{started.handoff_id}/confirm",
        data={"token": started.nonce, "action_digest": started.action_digest},
        headers=auth,
    )
    callback = flow.agent_http.post(
        CALLBACK_PATH, content=flow.provider_callback(app_id).body, headers=auth
    )
    assert (page.status_code, login.status_code, form.status_code) == (401, 401, 401)
    assert "set-cookie" not in login.headers  # no customer session for the agent
    assert callback.status_code == 401
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM customer_sessions") == [(0,)]


def test_a_customer_credential_is_not_an_agent_credential(flow: Flow) -> None:
    started = flow.start()
    flow.page_sign_in(started)
    session_secret = flow.customer_http.cookies.get("trusteq_customer_session", path="/customer")
    assert session_secret and session_secret.startswith("tqs_")
    for auth in (bearer(session_secret), customer_headers("cust-1")):
        response = flow.customer_http.get(
            f"/v1/applications/{started.application_id}", headers=auth
        )
        assert response.status_code == 401


# --- after OPENED / terminal states ----------------------------------------------------------


def test_after_opening_only_the_read_only_status_survives(flow: Flow) -> None:
    started = flow.run_to_opened()
    app_id = started.application_id
    onboarding, status = flow.onboarding_headers(app_id), flow.status_headers(app_id)
    for path in ("/handoff", "/submit"):
        response = flow.agent_http.post(f"/v1/applications/{app_id}{path}", headers=onboarding)
        assert response.status_code == 401  # revoked at OPENED
        response = flow.agent_http.post(f"/v1/applications/{app_id}{path}", headers=status)
        assert response.status_code == 403  # and the read-only token never could
    view = flow.agent_http.get(f"/v1/applications/{app_id}", headers=status)
    assert view.status_code == 200 and view.json()["state"] == "OPENED"
    assert set(view.json()) == {
        "application_id", "provider_id", "product_id", "channel_id", "state",
        "created_at", "updated_at",
    }  # fmt: skip
    assert denial_reasons(flow, app_id).count("token_revoked") == 2


@pytest.mark.parametrize(
    ("target", "authority"),
    [(S.CANCELLED, A.CUSTOMER_CHANNEL), (S.FAILED, A.SYSTEM_POLICY)],
)
def test_terminal_closure_revokes_grants_tokens_and_open_handoffs_atomically(
    flow: Flow, target: S, authority: A
) -> None:
    started = flow.start()
    app_id = started.application_id
    flow.service().transition(
        app_id, target, authority=authority, reason_code="closed", correlation_id="corr-t"
    )
    states = dict(
        flow.sql(
            "SELECT kind, revoked_at IS NOT NULL FROM mandates WHERE application_id = :a", a=app_id
        )
    )
    assert states == {"bootstrap": 1, "onboarding": 1, "status": 0}
    tokens = flow.sql(
        "SELECT m.kind, t.revoked_at IS NOT NULL FROM agent_access_tokens t "
        "JOIN mandates m USING (mandate_id) WHERE m.application_id = :a ORDER BY m.kind",
        a=app_id,
    )
    assert tokens == [("bootstrap", 1), ("onboarding", 1), ("status", 0)]
    assert flow.sql(
        "SELECT COUNT(*) FROM handoffs WHERE consumed_at IS NULL AND revoked_at IS NULL"
    ) == [(0,)]
    closing = next(e for e in flow.audit(app_id) if e.details.get("to") == target.value)
    assert closing.details["mandate_revoked"] is True
    assert (closing.details["tokens_revoked"], closing.details["handoffs_revoked"]) == (2, 1)
    assert flow.customer_confirm(started).status_code == 409  # the open handoff died too
    assert flow.agent_state(app_id) in {"CANCELLED", "FAILED"}


def test_a_failed_revocation_rolls_back_the_terminal_transition(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.workflow_repositories import SqlAlchemyAgentTokenRepository

    app_id = flow.start().application_id

    def broken(self, application_id, kinds, at):  # noqa: ANN001, ANN202
        raise RuntimeError("token store unavailable")

    monkeypatch.setattr(SqlAlchemyAgentTokenRepository, "revoke_for_application", broken)
    with pytest.raises(RuntimeError, match="token store unavailable"):
        flow.service().transition(
            app_id, S.CANCELLED, authority=A.CUSTOMER_CHANNEL, reason_code="x", correlation_id="c"
        )
    monkeypatch.undo()
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"  # no closure without revocation
    assert flow.sql(
        "SELECT COUNT(*) FROM mandates WHERE application_id = :a AND revoked_at IS NOT NULL",
        a=app_id,
    ) == [(0,)]


# --- secrets hygiene -------------------------------------------------------------------------


def test_no_raw_token_is_stored_logged_or_audited(
    flow: Flow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started = flow.run_to_opened()
    tokens = flow.credentials().for_application(started.application_id)
    assert tokens is not None
    raw = [tokens.onboarding, tokens.status, flow.credentials().bootstrap or ""]
    tables = [name for (name,) in flow.sql("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        dump = repr(flow.sql(f"SELECT * FROM {table}"))  # noqa: S608
        for token in raw:
            assert token not in dump, table
    stored = {h for (h,) in flow.sql("SELECT token_hash FROM agent_access_tokens")}
    assert {hash_token(t) for t in raw} <= stored  # what IS stored: the SHA-256 hashes
    for record in caplog.records:
        assert "tqa_" not in record.getMessage() and "tqa_" not in repr(record.__dict__)


def test_token_values_are_random_and_unique(flow: Flow) -> None:
    tokens = {flow.bootstrap() for _ in range(20)}
    assert len(tokens) == 20 and all(len(t) == 47 for t in tokens)
    assert len(set(flow.sql("SELECT jti FROM agent_access_tokens"))) == 20


# --- the trusted dev CLI (real clock: it is a separate operator process) ----------------------


@pytest.fixture
def live(db_url: str) -> Iterator[TestClient]:
    """An app on a file database with the REAL clock - like a running dev server."""
    settings = Settings(_env_file=None, environment=Environment.TEST, database_url=db_url)
    with TestClient(create_app(settings)) as client:
        yield client


def cli_tokens(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.strip().startswith("tqa_")]


def test_dev_cli_bootstrap_issues_a_working_narrow_token(
    live: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = live.app.state.settings  # type: ignore[attr-defined]
    assert (
        dev_auth.main(["bootstrap", "--agent", "agent-cli", "--customer", "cust-1"], settings) == 0
    )
    out = capsys.readouterr().out
    assert "not production OAuth/FAPI" in out and "scope=create_draft" in out
    (token,) = cli_tokens(out)
    response = live.post(
        "/v1/applications",
        json=BODY,
        headers={**bearer(token), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201
    status = bearer(response.json()["authorization"]["status"]["access_token"])
    assert (
        live.get(
            f"/v1/applications/{response.json()['application_id']}", headers=status
        ).status_code
        == 200
    )


def test_dev_cli_can_reissue_and_revoke_but_never_runs_in_production(
    live: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = live.app.state.settings  # type: ignore[attr-defined]
    created = live.post(
        "/v1/applications",
        json=BODY,
        headers={
            **bootstrap_headers(live, customer_id="cust-1"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
    ).json()
    app_id = created["application_id"]
    assert dev_auth.main(["app-tokens", "--application-id", app_id], settings) == 0
    onboarding, status = cli_tokens(capsys.readouterr().out)
    assert live.get(f"/v1/applications/{app_id}", headers=bearer(status)).status_code == 200
    assert (
        live.post(f"/v1/applications/{app_id}/handoff", headers=bearer(onboarding)).status_code
        == 201
    )
    with live.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        jti = connection.exec_driver_sql(
            "SELECT jti FROM agent_access_tokens WHERE token_hash = ?", (hash_token(status),)
        ).scalar_one()
    assert dev_auth.main(["revoke", "--jti", jti], settings) == 0
    assert live.get(f"/v1/applications/{app_id}", headers=bearer(status)).status_code == 401
    assert dev_auth.main(["revoke", "--jti", jti], settings) == 1  # nothing live to revoke
    assert dev_auth.main(["app-tokens", "--application-id", "app-nope"], settings) == 1
    prod = Settings(
        _env_file=None, environment=Environment.PRODUCTION, database_url=settings.database_url
    )
    assert dev_auth.main(["bootstrap", "--agent", "a", "--customer", "c"], prod) == 2
    assert "refused" in capsys.readouterr().err


def test_dev_cli_runs_as_a_module(tmp_path: Path) -> None:
    db = (tmp_path / "cli.db").as_posix()
    env = {"TRUSTEQ_DATABASE_URL": f"sqlite:///{db}", "TRUSTEQ_ENVIRONMENT": "development"}
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            sys.executable,
            "-m",
            "scripts.dev_auth",
            "bootstrap",
            "--agent",
            "a1",
            "--customer",
            "c1",
        ],
        cwd=ROOT,
        env={**{k: v for k, v in os.environ.items() if not k.startswith("TRUSTEQ_")}, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("tqa_") == 1
