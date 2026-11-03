"""Phase 03: replay safety, interrupted transactions and the bounded, idempotent resume.

The Phase 02 limitation: if the process stopped after the customer's confirmation but before
the mandatory (MOCK) checks finished, nothing resumed them. Now the agent's next authorized
`submit` resumes exactly the missing steps - reusing recorded evidence, never re-requesting it,
never running anything without stored customer consent, and still failing closed.
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.domain.application import DepotApplication
from app.domain.enums import CheckOutcome
from app.domain.receipts import ConsentReceipt
from app.integrations.results import CheckResult, SigningResult
from app.services.onboarding_service import OnboardingService
from tests.flow import Flow


class Recorder:
    """MOCK providers that count calls and can crash N times (process death mid-pipeline)."""

    def __init__(self, crash_kyc: int = 0, crash_compliance: int = 0, crash_contract: int = 0,
                 kyc_outcome: CheckOutcome = CheckOutcome.PASS) -> None:  # fmt: skip
        self.calls = {"kyc": 0, "compliance": 0, "contract": 0}
        self._crash = {"kyc": crash_kyc, "compliance": crash_compliance, "contract": crash_contract}
        self._kyc_outcome = kyc_outcome

    def _tick(self, name: str) -> None:
        self.calls[name] += 1
        if self.calls[name] <= self._crash[name]:
            raise RuntimeError(f"{name} provider unavailable (simulated crash)")

    def verify_identity(self, application: DepotApplication) -> CheckResult:
        self._tick("kyc")
        return CheckResult(
            outcome=self._kyc_outcome, reference=f"MOCK-KYC-{application.application_id}", mock=True
        )

    def assess(self, application: DepotApplication) -> CheckResult:
        self._tick("compliance")
        return CheckResult(
            outcome=CheckOutcome.PASS,
            reference=f"MOCK-COMPLIANCE-{application.application_id}",
            mock=True,
        )

    def record_signature(self, consent: ConsentReceipt) -> SigningResult:
        self._tick("contract")
        return SigningResult(
            signed=True, contract_ref=f"MOCK-CONTRACT-{consent.application_id}", mock=True
        )


def providers(recorder: Recorder) -> dict[str, object]:
    return {"kyc": recorder, "compliance": recorder, "contract": recorder}


def crash_after_confirmation(flow_factory: Callable[..., Flow], recorder: Recorder):  # noqa: ANN201
    """Run the journey until the customer confirmed and the check pipeline 'died'."""
    flow = flow_factory(**providers(recorder))
    flow.customer_http = TestClient(flow.app, raise_server_exceptions=False)
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 500  # the process "died" mid-checks
    return flow, started


def restarted(flow_factory: Callable[..., Flow], first: Flow, app_id: str, **overrides) -> Flow:  # noqa: ANN003
    """A brand-new app instance on the same database; the agent runtime kept its tokens."""
    second = flow_factory(**overrides)
    tokens = first.credentials().for_application(app_id)
    assert tokens is not None
    second.credentials().remember(app_id, tokens)
    return second


def checks(flow: Flow, app_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT check_type, outcome FROM check_results WHERE application_id = :a ORDER BY id",
        a=app_id,
    )


@pytest.mark.parametrize(
    ("crash", "stuck_in"),
    [
        ({"crash_kyc": 1}, "KYC_PENDING"),
        ({"crash_compliance": 1}, "COMPLIANCE_PENDING"),
        ({"crash_contract": 1}, "CONTRACT_PENDING"),
    ],
)
def test_after_a_crash_and_restart_the_next_submit_resumes_and_completes(
    flow_factory: Callable[..., Flow], crash: dict[str, int], stuck_in: str
) -> None:
    first, started = crash_after_confirmation(flow_factory, Recorder(**crash))
    app_id = started.application_id
    assert first.status_of(app_id) == stuck_in
    assert first.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]  # consent is durable

    second = restarted(flow_factory, first, app_id)  # providers healthy again
    resumed = second.agent().continue_after_customer(app_id)
    assert resumed.stopped_at is None and resumed.final_state == "IN_PROGRESS"
    assert second.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert [c[0] for c in checks(second, app_id)] == ["KYC", "COMPLIANCE", "CONTRACT"]
    assert second.provider_confirms(app_id).status_code == 200
    assert second.status_of(app_id) == "OPENED"
    resumes = [e for e in second.audit(app_id) if e.event_type == "workflow.resumed"]
    assert [(e.details["from"], e.outcome.value) for e in resumes] == [(stuck_in, "success")]


def test_the_resume_reuses_recorded_evidence_and_never_repeats_a_provider_call(
    flow_factory: Callable[..., Flow],
) -> None:
    recorder = Recorder(crash_compliance=1)  # KYC passed and was recorded, then the crash
    flow, started = crash_after_confirmation(flow_factory, recorder)
    app_id = started.application_id
    assert recorder.calls == {"kyc": 1, "compliance": 1, "contract": 0}
    for _ in range(3):  # however often the agent retries ...
        flow.agent().continue_after_customer(app_id)
    assert recorder.calls == {"kyc": 1, "compliance": 2, "contract": 1}  # ... once per step
    assert len(checks(flow, app_id)) == 3
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(1,)]


def test_re_entering_a_check_reuses_its_recorded_result_and_never_calls_again(
    flow_factory: Callable[..., Flow],
) -> None:
    """After MANUAL_REVIEW an operator resumes the KYC step. The recorded result stands: the
    provider is NOT asked again (no duplicate external call), and the outcome still blocks."""
    from app.domain.enums import ApplicationStatus as S
    from app.domain.workflow import TransitionAuthority as A

    recorder = Recorder(kyc_outcome=CheckOutcome.REVIEW_REQUIRED)
    flow = flow_factory(**providers(recorder))
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    app_id = started.application_id
    assert flow.status_of(app_id) == "MANUAL_REVIEW" and recorder.calls["kyc"] == 1
    flow.service().transition(  # trusted operator decision: re-enter the interrupted step
        app_id, S.KYC_PENDING, authority=A.OPERATOR, reason_code="operator_resume",
        correlation_id="corr-op",
    )  # fmt: skip
    flow.onboarding().advance_checks(app_id, "corr-op")  # trusted recovery
    response = flow.agent_http.post(  # and the agent's resume-on-submit
        f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
    )
    assert response.status_code == 409
    assert recorder.calls["kyc"] == 1  # the recorded REVIEW_REQUIRED was reused, twice
    assert checks(flow, app_id) == [("KYC", "REVIEW_REQUIRED")]
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_nothing_runs_on_the_agents_behalf_without_stored_customer_consent(
    flow_factory: Callable[..., Flow],
) -> None:
    recorder = Recorder()
    flow = flow_factory(**providers(recorder))
    app_id = flow.start().application_id  # handoff requested, NOT confirmed
    response = flow.agent_http.post(
        f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
    )
    assert response.status_code == 409
    assert recorder.calls == {"kyc": 0, "compliance": 0, "contract": 0}
    assert "workflow.resumed" not in flow.event_types(app_id)


def test_the_resume_does_not_bypass_a_check_that_does_not_pass(
    flow_factory: Callable[..., Flow],
) -> None:
    first, started = crash_after_confirmation(flow_factory, Recorder(crash_kyc=1))
    app_id = started.application_id
    second = restarted(
        flow_factory, first, app_id, kyc=Recorder(kyc_outcome=CheckOutcome.REVIEW_REQUIRED)
    )
    response = second.agent_http.post(
        f"/v1/applications/{app_id}/submit", headers=second.onboarding_headers(app_id)
    )
    assert response.status_code == 409  # fail closed, generic
    assert second.status_of(app_id) == "MANUAL_REVIEW"
    assert second.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_a_resume_needs_a_valid_onboarding_token(flow_factory: Callable[..., Flow]) -> None:
    recorder = Recorder(crash_kyc=1)
    flow, started = crash_after_confirmation(flow_factory, recorder)
    app_id = started.application_id
    for auth in (flow.status_headers(app_id), {}):
        response = flow.agent_http.post(f"/v1/applications/{app_id}/submit", headers=auth)
        assert response.status_code in {401, 403}
    flow.clock.advance(hours=2)  # the onboarding grant expired meanwhile
    expired = flow.agent_http.post(
        f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id)
    )
    assert expired.status_code == 401
    assert recorder.calls["kyc"] == 1  # no unauthorized request ever reached a provider
    assert flow.status_of(app_id) == "KYC_PENDING"


def test_concurrent_resumes_record_each_check_once(flow_factory: Callable[..., Flow]) -> None:
    recorder = Recorder(crash_kyc=1)
    flow, started = crash_after_confirmation(flow_factory, recorder)
    app_id = started.application_id
    services: list[OnboardingService] = [flow.onboarding() for _ in range(4)]
    barrier = threading.Barrier(4)

    def resume(i: int) -> str:
        barrier.wait()
        return services[i].advance_checks(app_id, f"corr-{i}").status.value

    with ThreadPoolExecutor(max_workers=4) as pool:
        finals = list(pool.map(resume, range(4)))
    assert set(finals) == {"CONTRACT_PENDING"}
    assert [c[0] for c in checks(flow, app_id)] == ["KYC", "COMPLIANCE", "CONTRACT"]
    assert flow.event_types(app_id).count("contract.confirmation_recorded") == 1


def test_concurrent_submits_hand_over_exactly_once(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    app_id = started.application_id
    clients = [TestClient(flow.app) for _ in range(4)]
    barrier = threading.Barrier(4)

    def submit(i: int) -> int:
        barrier.wait()
        return (
            clients[i]
            .post(f"/v1/applications/{app_id}/submit", headers=flow.onboarding_headers(app_id))
            .status_code
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(submit, range(4)))
    assert codes.count(202) == 1 and set(codes) <= {200, 202, 409}  # never a 500
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(1,)]
    assert flow.event_types(app_id).count("application.submitted") == 1


# --- interrupted transactions leave nothing half-done ------------------------------------------


def test_a_failure_while_issuing_tokens_leaves_no_application_and_keeps_the_grant(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.workflow_repositories import SqlAlchemyAgentTokenRepository

    token = flow.bootstrap()
    original = SqlAlchemyAgentTokenRepository.add
    calls = {"n": 0}

    def flaky_add(self, record):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 2:  # the status token of the new application
            raise RuntimeError("token store unavailable")
        return original(self, record)

    monkeypatch.setattr(SqlAlchemyAgentTokenRepository, "add", flaky_add)
    crashing = TestClient(flow.app, raise_server_exceptions=False)
    body = {"provider_id": "fixture-bank", "product_id": "depot-basic", "channel_id": "direct",
            "customer_ref": "cust-1"}  # fmt: skip
    key = "6f1c2a8e-1d5b-4f1e-9a51-2a4b8f0c6d11"
    failed = crashing.post(
        "/v1/applications",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )
    assert failed.status_code == 500
    for table in ("applications", "idempotency_records"):
        assert flow.sql(f"SELECT COUNT(*) FROM {table}") == [(0,)], table  # noqa: S608
    assert flow.sql("SELECT COUNT(*) FROM mandates WHERE kind != 'bootstrap'") == [(0,)]
    assert flow.sql("SELECT application_id FROM mandates WHERE kind = 'bootstrap'") == [(None,)]
    monkeypatch.undo()
    retry = flow.agent_http.post(
        "/v1/applications",
        json=body,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )
    assert retry.status_code == 201  # the same grant and key work once the store is back


def test_a_failure_while_recording_consent_leaves_the_handoff_usable(
    flow: Flow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.workflow_repositories import SqlAlchemyReceiptRepository

    started = flow.start()

    def broken(self, receipt):  # noqa: ANN001, ANN202
        raise RuntimeError("receipt store unavailable")

    monkeypatch.setattr(SqlAlchemyReceiptRepository, "add_consent", broken)
    flow.customer_http = TestClient(flow.app, raise_server_exceptions=False)
    assert flow.customer_confirm(started).status_code == 500
    assert flow.sql("SELECT consumed_at FROM handoffs") == [(None,)]  # not burned
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    monkeypatch.undo()
    assert flow.customer_confirm(started).status_code == 200
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]
