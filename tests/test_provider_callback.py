"""The MOCK depot provider's signed confirmation: the only path to OPENED."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.config import Environment, Settings
from app.integrations.provider_callback import (
    MAX_CLOCK_SKEW_SECONDS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    build_signed_confirmation,
    sign,
    verify,
)
from tests.flow import CALLBACK_PATH, INTENT, PROVIDER_SECRET, Flow

SECRET = b"unit-test-secret"
NOW = 1_800_000_000.0


# --- signing primitives --------------------------------------------------


def test_a_fresh_correctly_signed_message_verifies() -> None:
    cb = build_signed_confirmation(SECRET, application_id="app-1", provider_ref="MOCK-1", now=NOW)
    assert verify(
        SECRET,
        timestamp=cb.headers[TIMESTAMP_HEADER],
        signature=cb.headers[SIGNATURE_HEADER],
        body=cb.body,
        now=NOW,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda cb: {"body": cb.body.replace(b"app-1", b"app-2")},  # tampered body
        lambda cb: {"signature": "sha256=" + "0" * 64},  # wrong signature
        lambda cb: {"signature": None},
        lambda cb: {"signature": cb.headers[SIGNATURE_HEADER].removeprefix("sha256=")},  # no prefix
        lambda cb: {"timestamp": None},
        lambda cb: {"timestamp": "not-a-number"},
        lambda cb: {"timestamp": str(int(NOW) - 1)},  # signature was for another timestamp
        lambda cb: {"secret": b"another-secret"},
    ],
)
def test_any_tampering_or_malformed_input_fails_verification(mutate) -> None:  # noqa: ANN001
    cb = build_signed_confirmation(SECRET, application_id="app-1", provider_ref="MOCK-1", now=NOW)
    args = {
        "secret": SECRET,
        "timestamp": cb.headers[TIMESTAMP_HEADER],
        "signature": cb.headers[SIGNATURE_HEADER],
        "body": cb.body,
        "now": NOW,
    }
    args.update(mutate(cb))
    assert not verify(
        args["secret"],
        timestamp=args["timestamp"],
        signature=args["signature"],
        body=args["body"],
        now=args["now"],
    )


def test_stale_and_future_messages_are_rejected_even_when_correctly_signed() -> None:
    for offset in (MAX_CLOCK_SKEW_SECONDS + 1, -(MAX_CLOCK_SKEW_SECONDS + 1)):
        cb = build_signed_confirmation(
            SECRET, application_id="a", provider_ref="p", now=NOW + offset
        )
        assert not verify(
            SECRET,
            timestamp=cb.headers[TIMESTAMP_HEADER],
            signature=cb.headers[SIGNATURE_HEADER],
            body=cb.body,
            now=NOW,
        )
    assert sign(SECRET, "1", b"x") == sign(SECRET, "1", b"x")  # deterministic


# --- endpoint behaviour --------------------------------------------------


def submitted(flow: Flow) -> str:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    assert flow.agent().continue_after_customer(started.application_id).stopped_at is None
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    return started.application_id


def opened_transitions(flow: Flow, application_id: str) -> int:
    return sum(
        1
        for e in flow.audit(application_id)
        if e.event_type == "application.transition" and e.details["to"] == "OPENED"
    )


def test_the_callback_is_disabled_unless_a_secret_is_configured(client: TestClient) -> None:
    cb = build_signed_confirmation(b"anything", application_id="app-1", provider_ref="p")
    response = client.post(CALLBACK_PATH, content=cb.body, headers=cb.headers)
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_disabled"


def test_the_callback_is_not_part_of_the_public_contract(flow: Flow) -> None:
    schema = flow.app.openapi()
    assert CALLBACK_PATH not in schema["paths"]
    assert "confirmations" not in str(schema)


def test_a_valid_confirmation_opens_the_application_and_revokes_the_mandate(flow: Flow) -> None:
    app_id = submitted(flow)
    response = flow.provider_confirms(app_id)
    assert response.status_code == 200 and response.json() == {"result": "accepted"}

    assert flow.status_of(app_id) == "OPENED"
    assert flow.agent_state(app_id) == "OPENED"
    ((revoked,),) = flow.sql(
        "SELECT revoked_at FROM mandates WHERE application_id = :a AND kind = 'onboarding'",
        a=app_id,
    )
    assert revoked is not None  # the onboarding mandate ended with the workflow

    opened = [e for e in flow.audit(app_id) if e.details.get("to") == "OPENED"]
    assert len(opened) == 1
    assert opened[0].actor_type.value == "provider"
    assert opened[0].details["authority"] == "DEPOT_PROVIDER_CONFIRMATION"
    assert opened[0].details["mandate_revoked"] is True
    assert "provider.confirmation_accepted" in flow.event_types(app_id)
    ((cid, confirmed_at),) = flow.sql("SELECT confirmation_id, confirmed_at FROM depot_submissions")
    assert cid and confirmed_at


@pytest.mark.parametrize(
    "tamper",
    ["body", "signature", "secret", "stale", "missing_headers"],
)
def test_unauthenticated_confirmations_are_rejected_and_change_nothing(
    flow: Flow, tamper: str
) -> None:
    app_id = submitted(flow)
    cb = flow.provider_callback(
        app_id, secret="a-different-secret" if tamper == "secret" else PROVIDER_SECRET
    )
    body, headers = cb.body, dict(cb.headers)
    if tamper == "body":
        body = body.replace(app_id.encode(), b"app-" + b"0" * 32)
    elif tamper == "signature":
        headers[SIGNATURE_HEADER] = "sha256=" + "f" * 64
    elif tamper == "stale":
        flow.clock.advance(seconds=MAX_CLOCK_SKEW_SECONDS + 5)
    elif tamper == "missing_headers":
        headers = {"Content-Type": "application/json"}
    response = flow.provider_http.post(CALLBACK_PATH, content=body, headers=headers)
    assert response.status_code == 401
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert opened_transitions(flow, app_id) == 0


def test_the_agents_credentials_cannot_stand_in_for_the_providers_signature(flow: Flow) -> None:
    app_id = submitted(flow)
    cb = flow.provider_callback(app_id)
    response = flow.agent_http.post(
        CALLBACK_PATH,
        content=cb.body,
        headers={**flow.onboarding_headers(app_id), "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


def test_a_confirmation_before_submission_is_rejected_and_audited(flow: Flow) -> None:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200  # CONTRACT_PENDING, not submitted
    response = flow.provider_confirms(started.application_id)
    assert (
        response.status_code == 403 and response.json()["error"]["code"] == "confirmation_rejected"
    )
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    rejected = [
        e
        for e in flow.audit(started.application_id)
        if e.event_type == "provider.confirmation_rejected"
    ]
    assert rejected and rejected[0].details["reason"] == "unknown_submission_or_provider_ref"


def test_a_confirmation_for_an_unknown_application_is_not_found(flow: Flow) -> None:
    response = flow.provider_confirms("app-" + "0" * 32)
    assert response.status_code == 404


def test_the_provider_reference_must_match_the_recorded_submission(flow: Flow) -> None:
    app_id = submitted(flow)
    response = flow.provider_confirms(app_id, provider_ref="MOCK-DEPOT-someone-else")
    assert response.status_code == 403
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert opened_transitions(flow, app_id) == 0


def test_a_confirmation_cannot_carry_any_other_outcome(flow: Flow) -> None:
    app_id = submitted(flow)
    for outcome in ("REJECTED", "CANCELLED", "FAILED", "OPENED ", "set_status"):
        cb = flow.provider_callback(app_id)
        body = cb.body.replace(b'"OPENED"', f'"{outcome}"'.encode())
        # re-sign so only the *content* is wrong, not the authenticity
        signed = build_signed_confirmation(
            PROVIDER_SECRET.encode(), application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}",
            now=flow.clock().timestamp(),
        )  # fmt: skip
        ts = signed.headers[TIMESTAMP_HEADER]
        headers = {**signed.headers, SIGNATURE_HEADER: sign(PROVIDER_SECRET.encode(), ts, body)}
        response = flow.provider_http.post(CALLBACK_PATH, content=body, headers=headers)
        assert response.status_code == 422, outcome
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


def test_authentic_but_malformed_or_extra_field_messages_are_rejected(flow: Flow) -> None:
    app_id = submitted(flow)
    ts = str(int(flow.clock().timestamp()))
    for body in (b"not json", b"{}", b'{"application_id":"' + app_id.encode() + b'"}', b"[]"):
        headers = {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(PROVIDER_SECRET.encode(), ts, body)}
        assert (
            flow.provider_http.post(CALLBACK_PATH, content=body, headers=headers).status_code == 422
        )
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


def test_a_duplicate_confirmation_has_no_second_effect(flow: Flow) -> None:
    app_id = submitted(flow)
    callback = flow.provider_callback(app_id)
    first = flow.send_callback(callback)
    audit_before = len(flow.audit(app_id))
    second = flow.send_callback(callback)  # the provider retries the identical message
    third = flow.send_callback(callback)

    assert first.json() == {"result": "accepted"}
    assert second.status_code == third.status_code == 200
    assert second.json() == third.json() == {"result": "duplicate_ignored"}
    assert flow.status_of(app_id) == "OPENED"
    assert opened_transitions(flow, app_id) == 1
    assert len(flow.audit(app_id)) == audit_before  # not even an extra audit row
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(1,)]


def test_a_second_confirmation_with_a_new_id_after_opening_is_rejected(flow: Flow) -> None:
    app_id = submitted(flow)
    assert flow.provider_confirms(app_id).status_code == 200
    again = flow.provider_confirms(app_id)  # fresh confirmation_id, application already OPENED
    assert again.status_code == 403
    assert opened_transitions(flow, app_id) == 1
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions WHERE confirmation_id IS NOT NULL") == [
        (1,)
    ]


def test_a_confirmation_id_cannot_be_replayed_against_another_application(flow: Flow) -> None:
    first_app = submitted(flow)
    assert flow.provider_confirms(first_app, confirmation_id="shared-id-1").status_code == 200
    second = flow.start(actor_id="agent-b", intent=INTENT)
    assert flow.customer_confirm(second).status_code == 200
    assert flow.agent("agent-b").continue_after_customer(second.application_id).stopped_at is None
    replay = flow.provider_confirms(second.application_id, confirmation_id="shared-id-1")
    assert replay.status_code == 403
    assert flow.status_of(second.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


def test_simultaneous_identical_confirmations_open_exactly_once(flow: Flow) -> None:
    app_id = submitted(flow)
    callback = flow.provider_callback(app_id)
    clients = [TestClient(flow.app), TestClient(flow.app)]
    barrier = threading.Barrier(2)

    def send(i: int):  # noqa: ANN202
        barrier.wait()
        return clients[i].post(CALLBACK_PATH, content=callback.body, headers=callback.headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, range(2)))
    assert sorted(r.json()["result"] for r in responses) == ["accepted", "duplicate_ignored"]
    assert opened_transitions(flow, app_id) == 1
    assert flow.status_of(app_id) == "OPENED"


# --- configuration safety --------------------------------------------------


def test_the_mock_provider_callback_cannot_be_enabled_in_production() -> None:
    with pytest.raises(ValidationError, match="callback"):
        Settings(
            _env_file=None,
            environment=Environment.PRODUCTION,
            mock_provider_callback_secret=SecretStr("not-a-real-secret"),
        )
    prod = Settings(_env_file=None, environment=Environment.PRODUCTION)
    assert prod.mock_provider_callback_secret is None  # default: endpoint disabled


def test_blank_secret_in_env_means_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET", "")
    assert Settings(_env_file=None).mock_provider_callback_secret is None


def test_a_confirmation_for_a_submission_that_is_no_longer_awaiting_it_is_rejected_and_audited(
    flow: Flow,
) -> None:
    """Failed after the hand-over (e.g. timeout): a late 'opened' must not revive it."""
    from app.domain.enums import ApplicationStatus as S
    from app.domain.workflow import TransitionAuthority as A

    app_id = submitted(flow)
    flow.service().transition(
        app_id, S.FAILED, authority=A.SYSTEM_POLICY, reason_code="provider_timeout",
        correlation_id="corr-t",
    )  # fmt: skip
    response = flow.provider_confirms(app_id)

    assert (
        response.status_code == 403 and response.json()["error"]["code"] == "confirmation_rejected"
    )
    assert flow.status_of(app_id) == "FAILED"
    assert flow.sql("SELECT confirmation_id FROM depot_submissions") == [
        (None,)
    ]  # nothing recorded
    rejected = [e for e in flow.audit(app_id) if e.event_type == "provider.confirmation_rejected"]
    assert [e.details["reason"] for e in rejected] == ["application_not_awaiting_confirmation"]
    assert opened_transitions(flow, app_id) == 0
