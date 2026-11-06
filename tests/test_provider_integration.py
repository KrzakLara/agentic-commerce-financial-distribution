"""The versioned provider contract in the real application path: acceptance cases A to H."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.container import build_default_container
from app.core.config import Environment, Settings
from app.integrations.provider_contract import (
    COMPATIBILITY_POLICY,
    SCENARIOS,
    MockVersionedDepotApi,
    ProviderVersionUnavailable,
    VersionedDepotAdapter,
    build_mock_depot,
)
from scripts import provider_compat_matrix as matrix
from tests.flow import Flow, Started

ROOT = Path(__file__).resolve().parent.parent
SUCCESS = ["current_v2", "v1_only", "v2_deprecated_fallback"]
FAILURE = {
    "v2_retired_no_fallback": "no_supported_version",
    "unsupported_version": "no_supported_version",
    "malformed_response": "contract_violation",
    "provider_unavailable": "provider_unavailable",
    "provider_timeout": "provider_unavailable",
    "manipulated_opened": "contract_violation",
}


def confirmed(flow: Flow) -> Started:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    return started


def submit(flow: Flow, application_id: str):  # noqa: ANN201
    return flow.agent_http.post(
        f"/v1/applications/{application_id}/submit", headers=flow.onboarding_headers(application_id)
    )


def submission_audit(flow: Flow, application_id: str) -> dict:
    events = [
        e
        for e in flow.audit(application_id)
        if e.event_type in {"application.submitted", "application.submit_denied"}
    ]
    return events[-1].details


def test_the_application_uses_the_versioned_adapter_by_default() -> None:
    depot = build_default_container(Settings(_env_file=None, environment=Environment.TEST)).depot
    assert isinstance(depot, VersionedDepotAdapter)


def test_an_unknown_scenario_stops_the_application_from_starting() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST, mock_depot_scenario="v9")
    with pytest.raises(ValueError, match="unknown mock depot scenario"):
        build_default_container(settings)


@pytest.mark.parametrize(
    ("scenario", "version", "fallback_from"),
    [
        ("v1_only", "v1", None),  # A
        ("current_v2", "v2", None),  # B
        ("v2_deprecated_fallback", "v1", "v2"),  # C
    ],
)
def test_acceptance_a_b_c_supported_versions_complete_the_journey(
    flow_factory: Callable[..., Flow], scenario: str, version: str, fallback_from: str | None
) -> None:
    flow = flow_factory(mock_depot_scenario=scenario)
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 202
    # The answer changes no authority: the application only waits for the provider's signature.
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    details = submission_audit(flow, started.application_id)
    assert details["contract_version"] == version
    assert details.get("fallback_from") == fallback_from
    assert flow.provider_confirms(started.application_id).status_code == 200
    assert flow.status_of(started.application_id) == "OPENED"


@pytest.mark.parametrize(("scenario", "refusal"), FAILURE.items())
def test_acceptance_d_to_h_a_failed_integration_advances_nothing(
    flow_factory: Callable[..., Flow], scenario: str, refusal: str
) -> None:
    flow = flow_factory(mock_depot_scenario=scenario)
    started = confirmed(flow)
    app_id = started.application_id
    response = submit(flow, app_id)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unavailable"
    # The precise reason is audited; the agent gets the same generic answer every time.
    assert response.json()["error"]["message"] == "The depot provider is currently unavailable."
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]
    assert submission_audit(flow, app_id)["refusal"] == refusal

    # Even a correctly signed confirmation cannot open what was never handed over.
    assert flow.provider_confirms(app_id).status_code == 403
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    assert flow.agent_state(app_id) == "IN_PROGRESS"


def test_acceptance_g_a_manipulated_answer_cannot_open_the_account(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(mock_depot_scenario="manipulated_opened")
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 502
    assert "OPENED" not in {row[0] for row in flow.sql("SELECT status FROM applications")}


def test_acceptance_c_fallback_lowers_no_control(flow_factory: Callable[..., Flow]) -> None:
    api = MockVersionedDepotApi(served=("v1",))
    flow = flow_factory(depot=VersionedDepotAdapter(api))
    started = flow.start()
    app_id = started.application_id

    assert submit(flow, app_id).status_code == 409  # no consent: the provider is not even called
    assert api.calls == []

    assert flow.customer_confirm(started).status_code == 200
    assert submit(flow, app_id).status_code == 202
    assert [call[0] for call in api.calls] == ["v1"]  # one hand-over
    assert submit(flow, app_id).status_code == 200  # a repeat is recognized ...
    assert len(api.calls) == 1  # ... and is no second hand-over

    tampered = flow.provider_callback(app_id)
    forged = flow.provider_http.post(
        "/internal/depot-provider/confirmations",
        content=tampered.body.replace(b"OPENED", b"CLOSED"),
        headers=tampered.headers,
    )
    assert forged.status_code == 401  # the signature is checked exactly as before
    unsigned = flow.provider_http.post(
        "/internal/depot-provider/confirmations", content=tampered.body
    )
    assert unsigned.status_code == 401
    assert flow.status_of(app_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert flow.provider_confirms(app_id, provider_ref="MOCK-DEPOT-other").status_code == 403
    assert flow.provider_confirms(app_id).status_code == 200
    assert flow.status_of(app_id) == "OPENED"


def test_a_gone_version_alone_is_no_permission_to_fall_back(
    flow_factory: Callable[..., Flow],
) -> None:
    asked: list[str] = []

    def says_gone(version: str, _request: dict) -> Mapping[str, Any]:
        asked.append(version)
        raise ProviderVersionUnavailable(version)  # no fallback named

    flow = flow_factory(depot=VersionedDepotAdapter(says_gone))
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 502
    assert asked == ["v2"]


def test_the_bank_policy_must_allow_the_step_too(flow_factory: Callable[..., Flow]) -> None:
    """The provider offers an upgrade the bank's policy does not list: refused, not followed."""
    assert COMPATIBILITY_POLICY == {"v2": ("v1",)}
    asked: list[str] = []

    def offers_v2(version: str, _request: dict) -> Mapping[str, Any]:
        asked.append(version)
        raise ProviderVersionUnavailable(version, fallback_to=("v2",))

    flow = flow_factory(depot=VersionedDepotAdapter(offers_v2, versions=("v1", "v2")))
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 502
    assert asked == ["v1"]


@pytest.mark.parametrize("error", [TimeoutError("t"), ConnectionError("c")])
def test_acceptance_f_an_outage_is_never_retried_in_another_version(
    flow_factory: Callable[..., Flow], error: Exception
) -> None:
    asked: list[str] = []

    def broken(version: str, _request: dict) -> Mapping[str, Any]:
        asked.append(version)
        raise error

    flow = flow_factory(depot=VersionedDepotAdapter(broken))
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 502
    assert asked == ["v2"]  # the provider may have acted, so asking again could duplicate it


def test_the_same_application_can_continue_after_the_provider_recovers(
    flow_factory: Callable[..., Flow],
) -> None:
    down: dict[str, bool] = {"yes": True}
    api = MockVersionedDepotApi()

    def recovering(version: str, request: dict) -> Mapping[str, Any]:
        if down["yes"]:
            raise TimeoutError("synthetic")
        return api(version, request)

    flow = flow_factory(depot=VersionedDepotAdapter(recovering))
    started = confirmed(flow)
    assert submit(flow, started.application_id).status_code == 502
    down["yes"] = False
    assert submit(flow, started.application_id).status_code == 202
    assert flow.provider_confirms(started.application_id).status_code == 200
    assert flow.status_of(started.application_id) == "OPENED"


# --- the compatibility matrix -----------------------------------------------------------------


def test_the_matrix_covers_every_scenario_and_matches_the_committed_evidence() -> None:
    rows = matrix.observe_all()
    assert {row.scenario for row in rows} == set(SCENARIOS)
    by_id = {row.case_id: row for row in rows}
    assert (by_id["A"].version, by_id["A"].final_state) == ("v1", "OPENED")
    assert (by_id["B"].version, by_id["B"].final_state) == ("v2", "OPENED")
    assert (by_id["C"].version, by_id["C"].fallback_from) == ("v1", "v2")
    assert by_id["C"].final_state == "OPENED"
    for case_id in ("C2", "D", "E", "F1", "F2", "G"):
        row = by_id[case_id]
        assert (row.submit, row.callback) == (502, 403), case_id
        assert row.state_after_submit == row.final_state == "CONTRACT_PENDING", case_id
    committed = ROOT / "docs" / "evidence" / "provider-compatibility-matrix.md"
    assert committed.read_text(encoding="utf-8") == matrix.render(rows)


def test_the_matrix_runs_with_the_customer_step_switched_on() -> None:
    """Its journey is the integrated one: Confirm is only pressed after the customer's steps."""
    with matrix._flow("current_v2") as flow:  # noqa: SLF001
        assert flow.app.state.settings.customer_information_required is True
        started = matrix._to_customer_confirmation(flow)  # noqa: SLF001
        assert isinstance(flow.customer_http, TestClient)
        assert flow.status_of(started.application_id) == "CONTRACT_PENDING"


def test_every_scenario_builds_a_mock() -> None:
    for name in SCENARIOS:
        assert build_mock_depot(name).IS_MOCK is True
