"""Phase 04 human review: the customer's request for a person, the synthetic compliance review,
the operator workflow - and that no human action bypasses a mandatory control."""

from collections.abc import Callable, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.core.config import Environment, Settings
from app.core.exceptions import InvalidTransitionError
from app.domain.application import DepotApplication
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import (
    AuditActorType,
    HumanRequestCategory,
    OperatorRole,
    ReviewCaseReason,
    ReviewCaseStatus,
    ReviewDecision,
    ReviewEvidenceCategory,
)
from app.domain.review import ManualReviewCase
from app.domain.workflow import TransitionAuthority as A
from app.integrations.mock import MockComplianceProvider, MockKycProvider
from app.integrations.results import CheckResult
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.review_service import (
    HUMAN_REQUEST_MESSAGE,
    OperatorNotPermittedError,
    OperatorPrincipal,
    ReviewService,
    resolve_dev_operator,
)
from scripts import dev_operator
from tests.flow import FORGED_TOKEN, INTENT, Flow, bearer, customer_headers, human_csrf

REVIEWER = resolve_dev_operator("op-reviewer-1")
SUPERVISOR = resolve_dev_operator("op-supervisor-1")
REVIEW_CUSTOMER = "synthetic-review-1"
REVIEW_INTENT = INTENT.model_copy(update={"customer_ref": REVIEW_CUSTOMER})
SOURCE_OF_FUNDS = ReviewEvidenceCategory.SOURCE_OF_FUNDS_CONFIRMATION
D = ReviewDecision
NOW_ISO = "2026-11-03 09:00:00.000000"


class CountingChecks:
    """The MOCK KYC + compliance providers, counting calls."""

    def __init__(self) -> None:
        self.calls = {"kyc": 0, "compliance": 0}

    def verify_identity(
        self, application: DepotApplication, evidence: Sequence = ()
    ) -> CheckResult:
        self.calls["kyc"] += 1
        return MockKycProvider().verify_identity(application, evidence)

    def assess(self, application: DepotApplication, evidence: Sequence = ()) -> CheckResult:
        self.calls["compliance"] += 1
        return MockComplianceProvider().assess(application, evidence)


def ask_human(
    flow: Flow,
    application_id: str,
    customer_id: str = "cust-1",
    category: str = "QUESTION_ABOUT_PRODUCT",
    *,
    client: TestClient | None = None,
    headers: dict[str, str] | None = None,
):  # noqa: ANN201
    """The customer's request through the JSON API, as their own signed-in browser sends it:
    the server-side session cookie plus the page's CSRF token for this application.
    `headers` REPLACES the CSRF header (probes); `client` replaces the browser."""
    browser = client or flow.customer_browser(customer_id)
    sent = {"X-CSRF-Token": human_csrf(browser, application_id)} if headers is None else headers
    return browser.post(
        f"/v1/customer/applications/{application_id}/request-human",
        json={"category": category},
        headers=sent,
    )


def reviews(flow: Flow) -> ReviewService:
    factory = flow.app.state.session_factory
    return ReviewService(
        lambda: SqlAlchemyUnitOfWork(factory),
        flow.service(),
        advance_checks=flow.onboarding().advance_checks,
        clock=flow.clock,
    )


def cases(flow: Flow, application_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT reason, status, interrupted_step, check_type, check_attempt, resolution "
        "FROM review_cases WHERE application_id = :a ORDER BY rowid",
        a=application_id,
    )


def active_case(flow: Flow, application_id: str) -> str:
    (row,) = flow.sql(
        "SELECT case_id FROM review_cases WHERE application_id = :a AND status != 'CLOSED'",
        a=application_id,
    )
    return row[0]


def attempts(flow: Flow, application_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT check_type, attempt, outcome FROM check_results WHERE application_id = :a "
        "ORDER BY id",
        a=application_id,
    )


def resume_status(flow: Flow, application_id: str) -> str | None:
    return flow.sql(
        "SELECT resume_status FROM applications WHERE application_id = :a", a=application_id
    )[0][0]


def submit(flow: Flow, application_id: str):  # noqa: ANN201
    return flow.agent_http.post(
        f"/v1/applications/{application_id}/submit",
        headers=flow.onboarding_headers(application_id),
    )


def start_review(flow: Flow) -> str:
    """The synthetic manual-review scenario: KYC passes, compliance asks for a review."""
    started = flow.start(intent=REVIEW_INTENT)
    assert flow.customer_confirm(started, REVIEW_CUSTOMER).status_code == 200
    return started.application_id


# --- the customer asks for a person ------------------------------------------------------------


def test_the_customer_can_ask_for_a_person_and_the_workflow_pauses_at_its_step(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    response = ask_human(flow, app_id)
    assert response.status_code == 202
    assert response.json() == {
        "application_id": app_id,
        "status": "RECEIVED",
        "message": HUMAN_REQUEST_MESSAGE,
    }
    assert response.headers["cache-control"] == "no-store"
    # The existing MANUAL_REVIEW state, remembering the interrupted step - no second machine.
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    assert resume_status(flow, app_id) == "AWAITING_AUTHORIZATION"
    assert cases(flow, app_id) == [
        ("CUSTOMER_REQUESTED_HUMAN", "OPEN", "AWAITING_AUTHORIZATION", None, None, None)
    ]
    assert flow.agent_state(app_id) == "ACTION_REQUIRED"
    opened = [e for e in flow.audit(app_id) if e.event_type == "review.case_opened"]
    assert len(opened) == 1 and opened[0].actor_type is AuditActorType.CUSTOMER
    # While paused, neither the customer's confirmation nor the agent's submit gets through.
    assert flow.customer_confirm(started).status_code == 409
    assert submit(flow, app_id).status_code == 409


@pytest.mark.parametrize("next_to_a_customer_session", [False, True])
def test_an_agent_token_is_never_accepted_on_request_human(
    flow: Flow, next_to_a_customer_session: bool
) -> None:
    app_id = flow.start().application_id
    # The agent's own client - or even a browser that holds the customer's valid session and
    # CSRF token: an Authorization header is refused either way.
    client = flow.customer_browser() if next_to_a_customer_session else flow.agent_http
    headers = {
        **flow.onboarding_headers(app_id),
        **customer_headers("cust-1"),
        "X-CSRF-Token": human_csrf(client, app_id) or "x" * 43,
    }
    response = ask_human(flow, app_id, client=client, headers=headers)
    assert response.status_code == 401
    forged = ask_human(flow, app_id, client=client, headers=bearer(FORGED_TOKEN))
    assert forged.status_code == 401
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert cases(flow, app_id) == []
    denied = [e for e in flow.audit(app_id) if e.event_type == "review.human_request_denied"]
    assert [(e.actor_type, e.actor_id) for e in denied] == [
        (AuditActorType.AGENT, "agent-a"),
        (AuditActorType.AGENT, None),  # forged: not attributable to any agent
    ]


def test_only_the_applications_own_customer_may_ask(flow: Flow) -> None:
    app_id = flow.start().application_id
    other = ask_human(flow, app_id, customer_id="cust-2")
    unknown = ask_human(flow, "app-does-not-exist")
    assert other.status_code == unknown.status_code == 404
    for key in ("code", "message"):  # ids cannot be probed
        assert other.json()["error"][key] == unknown.json()["error"][key]
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION" and cases(flow, app_id) == []
    reasons = [
        e.details["reason"]
        for e in flow.audit(app_id)
        if e.event_type == "review.human_request_denied"
    ]
    assert reasons == ["not_the_customer"]


def test_without_the_simulated_customer_sign_in_the_request_is_refused(
    flow_factory: Callable[..., Flow],
) -> None:
    """Production (or a disabled DEV sign-in): there is no customer session, so request-human
    fails closed - a self-asserted header changes nothing."""
    flow = flow_factory(dev_customer_auth_enabled=False)
    app_id = flow.start().application_id
    for headers in (customer_headers("cust-1"), {}):
        response = ask_human(flow, app_id, client=flow.customer_http, headers=headers)
        assert response.status_code == 401
    assert cases(flow, app_id) == []


@pytest.mark.parametrize(
    "body",
    [
        {"category": "PLEASE_APPROVE_MY_ACCOUNT"},
        {"category": "OTHER", "message": "free text with personal data"},
        {},
    ],
)
def test_only_a_fixed_category_is_accepted(flow: Flow, body: dict[str, str]) -> None:
    app_id = flow.start().application_id
    browser = flow.customer_browser()
    response = browser.post(
        f"/v1/customer/applications/{app_id}/request-human",
        json=body,
        headers={"X-CSRF-Token": human_csrf(browser, app_id)},
    )
    assert response.status_code == 422
    assert cases(flow, app_id) == []


def test_a_second_request_joins_the_active_case_with_the_same_answer(flow: Flow) -> None:
    app_id = flow.start().application_id
    first = ask_human(flow, app_id)
    second = ask_human(flow, app_id, category="HELP_WITH_PROCESS")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert len(cases(flow, app_id)) == 1
    joined = [e.details for e in flow.audit(app_id) if e.event_type == "review.human_requested"]
    assert joined == [
        {
            "case_id": active_case(flow, app_id),
            "category": "HELP_WITH_PROCESS",
            "joined_existing_case": True,
        }
    ]


def test_the_acknowledgement_never_reveals_an_existing_compliance_case(flow: Flow) -> None:
    """No tipping-off: asking for a person looks the same whether or not compliance already
    opened a review, and the request joins that case instead of creating another."""
    review_app = start_review(flow)
    plain_app = flow.start(
        actor_id="agent-b", intent=INTENT.model_copy(update={"customer_ref": "cust-2"})
    ).application_id
    hidden = ask_human(flow, review_app, customer_id=REVIEW_CUSTOMER)
    fresh = ask_human(flow, plain_app, customer_id="cust-2")
    assert hidden.status_code == fresh.status_code == 202
    assert {**hidden.json(), "application_id": ""} == {**fresh.json(), "application_id": ""}
    for word in ("compliance", "aml", "review", "check", "case", "suspicious"):
        assert word not in hidden.text.lower()
    assert [row[0] for row in cases(flow, review_app)] == ["COMPLIANCE_REVIEW_REQUIRED"]


def test_after_submission_a_request_is_recorded_without_blocking_the_provider(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    assert flow.customer_confirm(started).status_code == 200
    assert flow.agent().continue_after_customer(app_id).stopped_at is None
    assert ask_human(flow, app_id).status_code == 202
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"  # the provider decides now
    assert cases(flow, app_id) == [("CUSTOMER_REQUESTED_HUMAN", "OPEN", None, None, None, None)]
    assert flow.provider_confirms(app_id).status_code == 200
    case_id = active_case(flow, app_id)
    with pytest.raises(OperatorNotPermittedError):
        reviews(flow).decide(REVIEWER, case_id, D.RESUME, "corr-op")
    reviews(flow).decide(REVIEWER, case_id, D.ACKNOWLEDGE, "corr-op")
    assert cases(flow, app_id)[0][1:] == ("CLOSED", None, None, None, "ACKNOWLEDGE")
    assert flow.status_of(app_id) == "OPENED"


# --- the synthetic REVIEW_REQUIRED compliance scenario ---------------------------------------


def test_the_synthetic_review_scenario_parks_the_application_with_a_durable_case(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory()
    app_id = start_review(flow)
    # 1. automatic onboarding stops, 2. MANUAL_REVIEW is persisted
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    assert attempts(flow, app_id) == [("KYC", 1, "PASS"), ("COMPLIANCE", 1, "REVIEW_REQUIRED")]
    # 3. a review case exists, 4. the interrupted step is preserved
    assert cases(flow, app_id) == [
        ("COMPLIANCE_REVIEW_REQUIRED", "OPEN", "COMPLIANCE_PENDING", "COMPLIANCE", 1, None)
    ]
    assert resume_status(flow, app_id) == "COMPLIANCE_PENDING"
    # 5. audited
    opened = [e for e in flow.audit(app_id) if e.event_type == "review.case_opened"]
    assert len(opened) == 1 and opened[0].actor_type is AuditActorType.SYSTEM
    # 6. the agent sees only a generic state
    status = flow.agent_http.get(f"/v1/applications/{app_id}", headers=flow.status_headers(app_id))
    assert status.json()["state"] == "ACTION_REQUIRED"
    for word in ("review", "compliance", "aml", "manual", "case"):
        assert word not in status.text.lower()
    # 7. it waits for an authorized human: nothing the agent or the provider does moves it
    refused = submit(flow, app_id)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "prerequisites_not_met"
    assert flow.provider_confirms(app_id).status_code == 403
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    # durable: a restarted application on the same database sees the same case
    restarted = flow_factory()
    assert restarted.status_of(app_id) == "MANUAL_REVIEW"
    assert cases(restarted, app_id) == cases(flow, app_id)


def test_ordinary_customers_are_not_affected_by_the_synthetic_scenario(flow: Flow) -> None:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    assert cases(flow, started.application_id) == []


# --- operator workflow ----------------------------------------------------------------------


def test_evidence_and_re_evaluation_let_the_deterministic_check_decide(flow: Flow) -> None:
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    service.request_information(REVIEWER, case_id, SOURCE_OF_FUNDS, "corr-op")
    with pytest.raises(OperatorNotPermittedError):  # waits for the requested information
        service.decide(REVIEWER, case_id, D.RE_EVALUATE, "corr-op")
    service.record_evidence(REVIEWER, case_id, SOURCE_OF_FUNDS, "SYN-SOF-0001", "corr-op")
    result = service.decide(REVIEWER, case_id, D.RE_EVALUATE, "corr-op")

    assert result.status is S.CONTRACT_PENDING
    # Append-only attempts: the first result is still there, unchanged.
    assert attempts(flow, app_id) == [
        ("KYC", 1, "PASS"),
        ("COMPLIANCE", 1, "REVIEW_REQUIRED"),
        ("COMPLIANCE", 2, "PASS"),
        ("CONTRACT", 1, "PASS"),
    ]
    assert cases(flow, app_id) == [
        (
            "COMPLIANCE_REVIEW_REQUIRED",
            "CLOSED",
            "COMPLIANCE_PENDING",
            "COMPLIANCE",
            1,
            "RE_EVALUATE",
        )
    ]
    # The agent continues as usual; OPENED still comes only from the provider.
    assert flow.agent().continue_after_customer(app_id).stopped_at is None
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert flow.provider_confirms(app_id).status_code == 200
    assert flow.status_of(app_id) == "OPENED"
    events = flow.event_types(app_id)
    for expected in (
        "review.information_requested",
        "review.decision_denied",
        "review.evidence_recorded",
        "review.decided",
        "check.re_evaluated",
    ):
        assert expected in events
    operator_steps = [
        e.details["reason_code"]
        for e in flow.audit(app_id)
        if e.event_type == "application.transition" and e.actor_type is AuditActorType.OPERATOR
    ]
    assert operator_steps == ["review_re_evaluate"]


def test_re_evaluation_without_the_resolving_evidence_parks_again(flow: Flow) -> None:
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    service.record_evidence(
        REVIEWER, case_id, ReviewEvidenceCategory.ADDRESS_CONFIRMATION, "SYN-ADR-0001", "corr-op"
    )
    result = service.decide(REVIEWER, case_id, D.RE_EVALUATE, "corr-op")
    assert result.status is S.MANUAL_REVIEW
    assert attempts(flow, app_id)[1:] == [
        ("COMPLIANCE", 1, "REVIEW_REQUIRED"),
        ("COMPLIANCE", 2, "REVIEW_REQUIRED"),
    ]
    first, second = cases(flow, app_id)
    assert first[1] == "CLOSED" and first[5] == "RE_EVALUATE"
    assert second == (
        "COMPLIANCE_REVIEW_REQUIRED", "OPEN", "COMPLIANCE_PENDING", "COMPLIANCE", 2, None
    )  # fmt: skip


def test_evidence_of_another_kind_does_not_end_the_wait_for_requested_information(
    flow: Flow,
) -> None:
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    service.request_information(REVIEWER, case_id, SOURCE_OF_FUNDS, "corr-op")
    case = service.record_evidence(
        REVIEWER, case_id, ReviewEvidenceCategory.ADDRESS_CONFIRMATION, "SYN-ADR-0001", "corr-op"
    )
    assert case.status is ReviewCaseStatus.AWAITING_INFORMATION
    with pytest.raises(OperatorNotPermittedError):
        service.decide(REVIEWER, case_id, D.RE_EVALUATE, "corr-op")


@pytest.mark.parametrize("reference", ["free text", "SYN-", "../etc/passwd", "syn-lower-1", ""])
def test_evidence_references_must_be_synthetic_ids(flow: Flow, reference: str) -> None:
    app_id = start_review(flow)
    with pytest.raises(ValueError, match="synthetic"):
        reviews(flow).record_evidence(
            REVIEWER, active_case(flow, app_id), SOURCE_OF_FUNDS, reference, "corr-op"
        )


def test_only_a_supervisor_may_reject_and_rejection_ends_the_agents_privileges(
    flow: Flow,
) -> None:
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    with pytest.raises(OperatorNotPermittedError):
        service.decide(REVIEWER, case_id, D.REJECT, "corr-op")
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    denied = [e.details for e in flow.audit(app_id) if e.event_type == "review.decision_denied"]
    assert denied[-1]["reason"] == "supervisor_required"

    assert service.decide(SUPERVISOR, case_id, D.REJECT, "corr-op").status is S.REJECTED
    assert flow.agent_state(app_id) == "DECLINED"
    assert submit(flow, app_id).status_code == 401  # onboarding token revoked at the end


def test_no_operator_decision_can_open_an_account(flow: Flow) -> None:
    assert {d.value for d in ReviewDecision} == {"RE_EVALUATE", "RESUME", "REJECT", "ACKNOWLEDGE"}
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    for decision in (D.RESUME, D.ACKNOWLEDGE):  # a compliance pause needs RE_EVALUATE or REJECT
        with pytest.raises(OperatorNotPermittedError):
            service.decide(SUPERVISOR, case_id, decision, "corr-op")
    assert flow.status_of(app_id) == "MANUAL_REVIEW"
    with pytest.raises(InvalidTransitionError):  # and the state machine has no such edge
        flow.service().transition(
            app_id, S.OPENED, authority=A.OPERATOR, reason_code="operator_open",
            correlation_id="corr-op",
        )  # fmt: skip
    assert flow.status_of(app_id) == "MANUAL_REVIEW"


def test_a_customer_requested_pause_resumes_at_the_interrupted_step(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    assert ask_human(flow, app_id).status_code == 202
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    with pytest.raises(OperatorNotPermittedError):  # there is no check result to re-evaluate
        service.decide(REVIEWER, case_id, D.RE_EVALUATE, "corr-op")
    assert service.decide(REVIEWER, case_id, D.RESUME, "corr-op").status is S.AWAITING_AUTHORIZATION
    assert flow.customer_confirm(started).status_code == 200  # the same, still valid handoff
    assert flow.agent().continue_after_customer(app_id).stopped_at is None
    assert flow.provider_confirms(app_id).status_code == 200
    assert flow.status_of(app_id) == "OPENED"


def test_resume_never_repeats_or_skips_a_recorded_check(flow_factory: Callable[..., Flow]) -> None:
    counter = CountingChecks()
    flow = flow_factory(kyc=counter, compliance=counter)
    started = flow.start()
    app_id = started.application_id
    assert flow.customer_confirm(started).status_code == 200
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    assert ask_human(flow, app_id).status_code == 202
    assert resume_status(flow, app_id) == "CONTRACT_PENDING"
    assert submit(flow, app_id).status_code == 409  # paused
    service = reviews(flow)
    assert service.decide(REVIEWER, active_case(flow, app_id), D.RESUME, "corr-op").status is (
        S.CONTRACT_PENDING
    )
    assert counter.calls == {"kyc": 1, "compliance": 1}  # recorded results were reused
    assert submit(flow, app_id).status_code == 202


def test_operator_identities_are_separate_from_agents_customers_and_tokens(flow: Flow) -> None:
    for value in ("agent-a", "cust-1", FORGED_TOKEN, "tqs_" + "A" * 43, "op-unknown", ""):
        assert resolve_dev_operator(value) is None
    promoted = OperatorPrincipal("op-reviewer-1", OperatorRole.SUPERVISOR)  # self-promoted
    with pytest.raises(OperatorNotPermittedError):
        reviews(flow).list_active(promoted)


def test_a_decided_case_cannot_be_decided_again(flow: Flow) -> None:
    app_id = start_review(flow)
    service = reviews(flow)
    case_id = active_case(flow, app_id)
    service.decide(SUPERVISOR, case_id, D.REJECT, "corr-op")
    for decision in ReviewDecision:
        with pytest.raises(OperatorNotPermittedError):
            service.decide(SUPERVISOR, case_id, decision, "corr-op")
    assert flow.status_of(app_id) == "REJECTED"


def test_the_database_keeps_one_active_case_and_review_history_final(flow: Flow) -> None:
    app_id = start_review(flow)
    case_id = active_case(flow, app_id)
    with pytest.raises(DBAPIError):  # a second ACTIVE case for the same application
        flow.execute(
            "INSERT INTO review_cases (case_id, application_id, reason, status, opened_by, "
            "evidence, created_at, updated_at, version) VALUES ('rvw-dup', :a, "
            "'CUSTOMER_REQUESTED_HUMAN', 'OPEN', 'customer', '[]', :t, :t, 1)",
            a=app_id,
            t=NOW_ISO,
        )
    with pytest.raises(DBAPIError):  # why and where a case was opened is immutable
        flow.execute("UPDATE review_cases SET interrupted_step = 'CONTRACT_PENDING'")
    with pytest.raises(DBAPIError):  # cases are never deleted
        flow.execute("DELETE FROM review_cases")
    with pytest.raises(DBAPIError):  # check attempts are append-only
        flow.execute("UPDATE check_results SET outcome = 'PASS'")
    reviews(flow).decide(SUPERVISOR, case_id, D.REJECT, "corr-op")
    with pytest.raises(DBAPIError):  # a closed case never reopens
        flow.execute("UPDATE review_cases SET status = 'OPEN', resolution = NULL")
    assert cases(flow, app_id)[0][1] == "CLOSED"


def test_review_case_contract() -> None:
    from tests.flow import FakeClock

    now = FakeClock()()
    base = {"case_id": "c-1", "application_id": "app-1", "created_at": now}
    with pytest.raises(ValueError, match="CLOSED"):
        ManualReviewCase(
            **base, reason=ReviewCaseReason.CHECK_FAILED, status=ReviewCaseStatus.CLOSED
        )
    with pytest.raises(ValueError, match="category"):
        ManualReviewCase(**base, reason=ReviewCaseReason.CUSTOMER_REQUESTED_HUMAN)
    with pytest.raises(ValueError, match="category"):
        ManualReviewCase(
            **base,
            reason=ReviewCaseReason.CHECK_FAILED,
            request_category=HumanRequestCategory.OTHER,
        )
    with pytest.raises(ValueError, match="check type"):
        ManualReviewCase(**base, reason=ReviewCaseReason.CHECK_FAILED, check_attempt=1)


def route_paths(routes: Sequence[object]) -> set[str]:
    """Every path the app serves (included routers are nested)."""
    paths: set[str] = set()
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            paths |= route_paths(nested.routes)
        elif getattr(route, "path", None):
            paths.add(route.path)  # type: ignore[attr-defined]
    return paths


def test_there_is_no_operator_http_endpoint(app: FastAPI) -> None:
    paths = route_paths(app.routes)
    assert [p for p in paths if "operator" in p or "review" in p] == []
    assert "/v1/customer/applications/{application_id}/request-human" in paths


# --- the trusted dev operator console --------------------------------------------------------


def console(flow: Flow, *argv: str, **settings_overrides: object) -> int:
    settings = Settings(
        _env_file=None,
        environment=settings_overrides.pop("environment", Environment.TEST),
        database_url=flow.app.state.settings.database_url,
        **settings_overrides,  # type: ignore[arg-type]
    )
    return dev_operator.main(list(argv), settings=settings, container=flow.container)


def test_the_operator_console_lists_inspects_and_decides(
    flow: Flow, capsys: pytest.CaptureFixture[str]
) -> None:
    app_id = start_review(flow)
    case_id = active_case(flow, app_id)
    assert console(flow, "--operator", "op-reviewer-1", "list") == 0
    listed = capsys.readouterr().out
    assert case_id in listed and "COMPLIANCE_REVIEW_REQUIRED" in listed
    assert console(flow, "--operator", "op-reviewer-1", "show", "--case", case_id) == 0
    shown = capsys.readouterr().out
    assert "allowed decisions: REJECT, RE_EVALUATE" in shown
    assert "review.case_opened" in shown and "tqa_" not in shown
    assert console(
        flow, "--operator", "op-reviewer-1", "add-evidence", "--case", case_id,
        "--category", "SOURCE_OF_FUNDS_CONFIRMATION", "--reference", "SYN-SOF-0001",
    ) == 0  # fmt: skip
    assert console(
        flow, "--operator", "op-reviewer-1", "decide", "--case", case_id,
        "--decision", "RE_EVALUATE",
    ) == 0  # fmt: skip
    assert "now CONTRACT_PENDING" in capsys.readouterr().out
    assert flow.status_of(app_id) == "CONTRACT_PENDING"


def test_the_operator_console_refuses_what_it_must(
    flow: Flow, capsys: pytest.CaptureFixture[str]
) -> None:
    app_id = start_review(flow)
    case_id = active_case(flow, app_id)
    token = "tqa_" + "B" * 43
    assert console(flow, "--operator", token, "list") == 2
    assert console(flow, "--operator", "agent-a", "list") == 2
    assert token not in capsys.readouterr().err  # a pasted token is never echoed
    assert console(flow, "--operator", "op-reviewer-1", "list", dev_operator_cli_enabled=False) == 2
    assert console(
        flow, "--operator", "op-reviewer-1", "decide", "--case", case_id, "--decision", "REJECT"
    ) == 1  # fmt: skip
    assert console(
        flow, "--operator", "op-reviewer-1", "add-evidence", "--case", case_id,
        "--category", "SOURCE_OF_FUNDS_CONFIRMATION", "--reference", "not synthetic",
    ) == 1  # fmt: skip
    assert flow.status_of(app_id) == "MANUAL_REVIEW"


def test_the_operator_console_is_off_in_production(flow: Flow) -> None:
    assert console(flow, "--operator", "op-supervisor-1", "list", environment="production") == 2
