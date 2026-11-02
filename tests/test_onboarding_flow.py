"""The complete simulated happy path and the ways it must NOT advance."""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import Environment, Settings
from app.core.exceptions import InvalidTransitionError, TransitionNotAuthorizedError
from app.domain.application import DepotApplication
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import CheckOutcome
from app.domain.workflow import TransitionAuthority as A
from app.integrations.mock import MockComplianceProvider, MockKycProvider
from app.integrations.results import CheckResult
from app.main import create_app
from tests.flow import CALLBACK_PATH, INTENT, PROVIDER_SECRET, Flow, agent_headers

FORBIDDEN_FOR_AGENT = (
    "kyc", "compliance", "manual", "mock-", "cust-1", "customer_ref", "consent",
    "traceback", "sqlite", "reason", "missing", "nonce_hash",
)  # fmt: skip


# --- the happy path --------------------------------------------------


def test_complete_happy_path_ends_in_a_provider_confirmed_opened(flow: Flow) -> None:
    # 1. The agent discovers, drafts and asks for the customer's approval. Nothing is approved.
    started = flow.start()
    app_id = started.application_id
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert flow.agent_state(app_id) == "AWAITING_CUSTOMER"

    # 2. The customer - a different actor, a different client - confirms the exact action.
    assert flow.customer_confirm(started).status_code == 200
    assert flow.status_of(app_id) == "CONTRACT_PENDING"  # mock KYC, compliance, contract all done
    assert flow.agent_state(app_id) == "IN_PROGRESS"

    # 3. The agent resumes and submits. The application is handed to the provider, NOT opened.
    resumed = flow.agent().continue_after_customer(app_id)
    assert resumed.stopped_at is None and resumed.final_state == "IN_PROGRESS"
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert flow.agent().read_status(app_id).final_state == "IN_PROGRESS"  # no claim of success yet
    assert flow.sql("SELECT status FROM applications") == [("SUBMITTED_TO_DEPOT_PROVIDER",)]

    # 4. Only the provider's signed confirmation opens it.
    assert flow.provider_confirms(app_id).json() == {"result": "accepted"}
    assert flow.status_of(app_id) == "OPENED"
    final = flow.agent().read_status(app_id)
    assert final.final_state == "OPENED" and final.stopped_at is None


def test_the_audit_timeline_records_every_trusted_step_in_order(flow: Flow) -> None:
    app_id = flow.run_to_opened().application_id
    events = flow.audit(app_id)
    assert [e.event_type for e in events] == [
        "application.created",
        "application.transition",  # DRAFT -> AWAITING_AUTHORIZATION
        "handoff.created",
        "application.transition",  # -> AUTHORIZED
        "handoff.confirmed",
        "application.transition",  # -> KYC_PENDING
        "application.transition",  # -> COMPLIANCE_PENDING
        "application.transition",  # -> CONTRACT_PENDING
        "contract.confirmation_recorded",
        "application.transition",  # -> SUBMITTED_TO_DEPOT_PROVIDER
        "application.submitted",
        "application.transition",  # -> OPENED
        "provider.confirmation_accepted",
    ]
    steps = [
        (e.details["to"], e.actor_type.value)
        for e in events
        if e.event_type == "application.transition"
    ]
    assert steps == [
        ("AWAITING_AUTHORIZATION", "system"),
        ("AUTHORIZED", "customer"),
        ("KYC_PENDING", "system"),
        ("COMPLIANCE_PENDING", "system"),
        ("CONTRACT_PENDING", "system"),
        ("SUBMITTED_TO_DEPOT_PROVIDER", "system"),
        ("OPENED", "provider"),
    ]
    assert [
        e.details["version"] for e in events if e.event_type == "application.transition"
    ] == list(range(2, 9))
    assert all(e.outcome.value == "success" for e in events)


def test_mock_services_are_explicitly_labelled_and_leave_internal_evidence(flow: Flow) -> None:
    app_id = flow.run_to_opened().application_id
    checks = flow.sql(
        "SELECT check_type, outcome, reference, mock FROM check_results "
        "WHERE application_id = :a ORDER BY id",
        a=app_id,
    )
    assert [(c[0], c[1]) for c in checks] == [
        ("KYC", "PASS"),
        ("COMPLIANCE", "PASS"),
        ("CONTRACT", "PASS"),
    ]
    assert all(c[3] == 1 and c[2].startswith("MOCK-") for c in checks)
    ((submission_ref,),) = flow.sql("SELECT provider_ref FROM depot_submissions")
    assert submission_ref.startswith("MOCK-DEPOT-")
    contract_events = [
        e for e in flow.audit(app_id) if e.event_type == "contract.confirmation_recorded"
    ]
    assert contract_events[0].details["mock"] is True


def test_the_onboarding_mandate_is_issued_with_the_draft_and_revoked_at_opened(flow: Flow) -> None:
    started = flow.start()
    ((scopes, revoked),) = flow.sql(
        "SELECT scopes, revoked_at FROM mandates WHERE application_id = :a",
        a=started.application_id,
    )
    assert revoked is None and "submit_after_consent" in scopes and "request_handoff" in scopes
    flow.customer_confirm(started)
    flow.agent().continue_after_customer(started.application_id)
    ((still_active,),) = flow.sql("SELECT revoked_at FROM mandates")
    assert still_active is None  # submitted, not yet opened: the mandate is still needed
    flow.provider_confirms(started.application_id)
    ((revoked_at,),) = flow.sql("SELECT revoked_at FROM mandates")
    assert revoked_at is not None


def test_after_opening_the_agent_can_no_longer_act_on_the_application(flow: Flow) -> None:
    app_id = flow.run_to_opened().application_id
    for path in ("handoff", "submit"):
        response = flow.agent_http.post(
            f"/v1/applications/{app_id}/{path}", headers=agent_headers()
        )
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "mandate_inactive"
    assert flow.status_of(app_id) == "OPENED"
    assert flow.agent_state(app_id) == "OPENED"  # read-only generic status stays available


def test_opened_state_survives_an_application_restart(
    flow_factory: Callable[..., Flow], db_url: str
) -> None:
    first = flow_factory()
    app_id = first.run_to_opened().application_id
    audit_len = len(first.audit(app_id))

    second = flow_factory()  # a brand-new app instance on the same SQLite file
    assert second.app is not first.app
    assert second.agent_state(app_id) == "OPENED"
    assert second.status_of(app_id) == "OPENED"
    assert len(second.audit(app_id)) == audit_len
    assert second.sql("SELECT revoked_at IS NOT NULL FROM mandates") == [(1,)]
    # ... and the restored application is still protected.
    assert (
        second.agent_http.post(
            f"/v1/applications/{app_id}/submit", headers=agent_headers()
        ).status_code
        == 403
    )
    assert second.provider_confirms(app_id).status_code == 403  # nothing left to confirm


def test_in_flight_state_survives_a_restart_and_the_flow_can_continue(
    flow_factory: Callable[..., Flow],
) -> None:
    first = flow_factory()
    started = first.start()
    assert first.customer_confirm(started).status_code == 200  # stop here: "process dies"

    second = flow_factory()
    resumed = second.agent().continue_after_customer(started.application_id)
    assert resumed.stopped_at is None
    assert second.provider_confirms(started.application_id).status_code == 200
    assert second.status_of(started.application_id) == "OPENED"


# --- nothing advances without its prerequisites -----------------------------------------


def test_submission_is_blocked_while_the_customer_has_not_confirmed(flow: Flow) -> None:
    started = flow.start()
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit", headers=agent_headers()
    )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "prerequisites_not_met"
    )
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]

    denied = [
        e for e in flow.audit(started.application_id) if e.event_type == "application.submit_denied"
    ]
    assert len(denied) == 1  # the precise reasons are in the audit trail only
    assert {"customer_consent", "kyc", "compliance", "contract"} <= set(
        denied[0].details["missing"]
    )
    assert "customer_consent" not in response.text and "kyc" not in response.text.lower()


def test_submission_of_a_draft_without_any_handoff_is_blocked(flow: Flow) -> None:
    app_id = flow.agent().draft_application(INTENT).application_id
    response = flow.agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers())
    assert response.status_code == 409
    assert flow.status_of(app_id) == "DRAFT"


def test_another_agent_cannot_submit_my_application(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit", headers=agent_headers("agent-b")
    )
    assert response.status_code == 404
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"compliance": MockComplianceProvider(CheckOutcome.REVIEW_REQUIRED)},
            "compliance_not_passed",
        ),
        ({"compliance": MockComplianceProvider(CheckOutcome.FAIL)}, "compliance_not_passed"),
        ({"kyc": MockKycProvider(CheckOutcome.REVIEW_REQUIRED)}, "kyc_not_passed"),
        ({"kyc": MockKycProvider(CheckOutcome.FAIL)}, "kyc_not_passed"),
    ],
)
def test_a_check_that_does_not_pass_stops_the_workflow_in_manual_review(
    flow_factory: Callable[..., Flow], overrides: dict[str, object], reason: str
) -> None:
    flow = flow_factory(**overrides)
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200  # the customer did their part

    app_id = started.application_id
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    assert flow.agent_state(app_id) == "ACTION_REQUIRED"  # generic: no hint it is a review
    response = flow.agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers())
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "prerequisites_not_met"
    )
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]
    assert flow.provider_confirms(app_id).status_code == 403  # and nothing can open it
    assert flow.status_of(app_id) == "MANUAL_REVIEW"

    transitions = [
        e.details["reason_code"]
        for e in flow.audit(app_id)
        if e.event_type == "application.transition"
    ]
    assert transitions[-1] == reason
    # The review remembers where it stopped; it is not a bypass (state-machine rule).
    ((resume,),) = flow.sql("SELECT resume_status FROM applications")
    assert resume in {"KYC_PENDING", "COMPLIANCE_PENDING"}


class FlakyKyc:
    """A MOCK KYC provider that fails once (simulating a crash/outage), then recovers."""

    def __init__(self) -> None:
        self.calls = 0

    def verify_identity(self, application: DepotApplication) -> CheckResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("kyc provider unavailable")
        return CheckResult(
            outcome=CheckOutcome.PASS, reference=f"MOCK-KYC-{application.application_id}", mock=True
        )


def test_an_interrupted_check_pipeline_fails_closed_and_can_be_resumed(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(kyc=FlakyKyc())
    flow.customer_http = TestClient(flow.app, raise_server_exceptions=False)
    started = flow.start()
    crashed = flow.customer_confirm(started)
    assert crashed.status_code == 500 and crashed.json()["error"]["code"] == "internal_error"

    app_id = started.application_id
    assert (
        flow.status_of(app_id) == "KYC_PENDING"
    )  # the consent is durable, the checks are not done
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]
    blocked = flow.agent_http.post(f"/v1/applications/{app_id}/submit", headers=agent_headers())
    assert blocked.status_code == 409  # fail closed: submit stays blocked
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]

    service = flow.onboarding()
    service.advance_checks(app_id, "corr-recovery")  # trusted recovery re-runs what is pending
    service.advance_checks(app_id, "corr-recovery")  # and is idempotent
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT COUNT(*) FROM check_results WHERE application_id = :a", a=app_id) == [
        (3,)
    ]
    assert flow.agent().continue_after_customer(app_id).stopped_at is None
    assert flow.provider_confirms(app_id).status_code == 200


# --- OPENED can only ever come from the trusted confirmation ----------------------------


def test_opened_cannot_be_reached_by_any_authority_except_the_provider_confirmation(
    flow: Flow,
) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    flow.agent().continue_after_customer(started.application_id)  # now SUBMITTED_TO_DEPOT_PROVIDER
    for authority in (A.AGENT, A.SYSTEM_POLICY, A.CUSTOMER_CHANNEL, A.OPERATOR):
        with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
            flow.service().transition(
                started.application_id, S.OPENED, authority=authority,
                reason_code="forced", correlation_id="corr-t",
            )  # fmt: skip
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    rejected = [
        e
        for e in flow.audit(started.application_id)
        if e.event_type == "application.transition_rejected"
    ]
    assert len(rejected) == 4


def test_even_raw_sql_cannot_open_an_application_that_was_never_submitted(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)  # CONTRACT_PENDING
    with pytest.raises(IntegrityError, match="OPENED is only reachable"):
        flow.execute(
            "UPDATE applications SET status = 'OPENED' WHERE application_id = :a",
            a=started.application_id,
        )
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"


def test_the_agent_has_no_route_that_could_open_anything(flow: Flow) -> None:
    started = flow.start()
    for method, path in (
        ("post", f"/v1/applications/{started.application_id}/open"),
        ("post", f"/v1/applications/{started.application_id}/confirm"),
        ("patch", f"/v1/applications/{started.application_id}"),
        ("put", f"/v1/applications/{started.application_id}/status"),
        ("post", "/internal/applications/open"),
        ("post", "/v1/internal/depot-provider/confirmations"),
    ):
        response = flow.agent_http.request(
            method, path, json={"status": "OPENED"}, headers=agent_headers()
        )
        assert response.status_code in {404, 405}, (method, path)
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_the_customer_cannot_open_an_application_either(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    flow.agent().continue_after_customer(started.application_id)
    response = flow.customer_http.post(
        f"/v1/applications/{started.application_id}/submit", headers={"X-Dev-Customer-Id": "cust-1"}
    )
    assert response.status_code == 401  # a customer context is not an agent context
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


# --- duplicates --------------------------------------------------


def test_duplicate_submit_requests_hand_over_to_the_provider_only_once(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    url = f"/v1/applications/{started.application_id}/submit"
    first = flow.agent_http.post(url, headers=agent_headers())
    second = flow.agent_http.post(url, headers=agent_headers())
    third = flow.agent_http.post(url, headers=agent_headers())
    assert (first.status_code, second.status_code, third.status_code) == (202, 200, 200)
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(1,)]
    assert flow.event_types(started.application_id).count("application.submitted") == 1


def test_repeating_the_whole_agent_journey_does_not_duplicate_applications(flow: Flow) -> None:
    first = flow.start()
    second = flow.start()  # same intent: same idempotency key, same application, new handoff
    assert first.application_id == second.application_id
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(1,)]
    assert flow.customer_confirm(first).status_code == 409  # the earlier handoff was superseded
    assert flow.customer_confirm(second).status_code == 200


# --- what the agent can see --------------------------------------------------


def test_agent_facing_responses_never_expose_internal_or_compliance_detail(flow: Flow) -> None:
    seen: list[str] = []

    class Recording(TestClient):
        def request(self, *args, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ANN002, ANN003, ANN201
            response = super().request(*args, **kwargs)
            seen.append(response.text)
            return response

    flow.agent_http = Recording(flow.app)
    started = flow.start()
    flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit", headers=agent_headers()
    )  # denied
    flow.customer_confirm(started)
    flow.agent().continue_after_customer(started.application_id)
    flow.provider_confirms(started.application_id)
    flow.agent().read_status(started.application_id)
    flow.agent_http.get("/v1/applications/app-nope", headers=agent_headers())

    assert len(seen) >= 10  # the whole journey was actually observed
    for body in seen:
        lowered = body.lower()
        for token in FORBIDDEN_FOR_AGENT:
            assert token not in lowered, (token, body[:200])


def test_the_agent_only_ever_sees_the_coarse_state_vocabulary(flow: Flow) -> None:
    started = flow.start()
    states = {flow.agent_state(started.application_id)}
    flow.customer_confirm(started)
    states.add(flow.agent_state(started.application_id))
    flow.agent().continue_after_customer(started.application_id)
    states.add(flow.agent_state(started.application_id))
    flow.provider_confirms(started.application_id)
    states.add(flow.agent_state(started.application_id))
    assert states == {"AWAITING_CUSTOMER", "IN_PROGRESS", "OPENED"}


# --- production safety --------------------------------------------------


def test_in_production_the_whole_ceremony_fails_closed(db_url: str) -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION, database_url=db_url)
    with TestClient(create_app(settings)) as prod:
        # No dev identities, no mock provider callback: nobody can start, confirm or open anything.
        body = {
            "provider_id": "fixture-bank",
            "product_id": "depot-basic",
            "channel_id": "direct",
            "customer_ref": "c",
        }
        assert (
            prod.post(
                "/v1/applications",
                json=body,
                headers={**agent_headers(), "Idempotency-Key": str(uuid.uuid4())},
            ).status_code
            == 401
        )
        assert (
            prod.post("/v1/applications/app-1/submit", headers=agent_headers()).status_code == 401
        )
        assert (
            prod.post(
                "/v1/customer/handoffs/hnd-1/confirm",
                json={"nonce": "n" * 20, "action_digest": "a" * 64},
                headers={"X-Dev-Customer-Id": "c"},
            ).status_code
            == 401
        )
        assert prod.post(CALLBACK_PATH, content=b"{}", headers={}).status_code == 503
        assert prod.get("/health").status_code == 200
    assert PROVIDER_SECRET  # (the secret exists only in tests; production refuses to hold one)
