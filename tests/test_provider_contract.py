"""Synthetic v1/v2 depot provider contract: fixtures, fallback, safe failure, no bypass."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.application import DepotApplication
from app.integrations.provider_contract import (
    SUPPORTED_VERSIONS,
    MockVersionedDepotApi,
    ProviderContractViolation,
    VersionedDepotAdapter,
    build_submission_request,
    parse_submission_response,
)
from tests.flow import Flow

FIXTURES = Path(__file__).parent / "fixtures" / "provider_contract"
NOW = datetime(2026, 11, 5, tzinfo=UTC)
APPLICATION = DepotApplication(
    application_id="app-fixture",
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="cust-1",
    agent_actor_id="agent-a",
    created_at=NOW,
    updated_at=NOW,
)


def fixture(version: str) -> dict:
    return json.loads((FIXTURES / f"depot_submission_{version}.json").read_text(encoding="utf-8"))


# --- the contract itself ------------------------------------------------------------------------


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_request_response_and_mock_all_match_the_fixture(version: str) -> None:
    expected = fixture(version)
    assert build_submission_request(version, APPLICATION) == expected["request"]
    assert MockVersionedDepotApi()(version, expected["request"]) == expected["response"]
    submission = parse_submission_response(version, expected["response"])
    assert submission.accepted is True and submission.mock is True
    assert submission.provider_ref == "MOCK-DEPOT-app-fixture"


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_the_request_sends_references_only(version: str) -> None:
    assert "cust-1" not in json.dumps(build_submission_request(version, APPLICATION))


def _edit(version: str, path: tuple[str, ...], value: object) -> dict:
    payload = copy.deepcopy(fixture(version)["response"])
    target = payload
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return payload


@pytest.mark.parametrize(
    ("version", "payload"),
    [
        ("v1", _edit("v1", ("provider_ref",), KeyError)),
        ("v1", _edit("v1", ("accepted",), KeyError)),
        ("v1", _edit("v1", ("accepted",), "true")),
        ("v1", _edit("v1", ("accepted",), 1)),
        ("v1", _edit("v1", ("provider_ref",), "bad reference with spaces")),
        ("v1", _edit("v1", ("opened",), True)),
        ("v1", _edit("v1", ("contract_version",), "v2")),
        ("v2", _edit("v2", ("submission",), KeyError)),
        ("v2", _edit("v2", ("submission", "reference"), KeyError)),
        ("v2", _edit("v2", ("submission", "status"), "OPENED")),
        ("v2", _edit("v2", ("submission", "status"), "accepted")),
        ("v2", _edit("v2", ("submission", "opened"), True)),
        ("v2", _edit("v2", ("contract_version",), "v3")),
        ("v2", fixture("v1")["response"]),  # the right shape of the wrong version
        ("v1", fixture("v2")["response"]),
        ("v3", fixture("v2")["response"]),  # a version the bank does not support
    ],
)
def test_malformed_or_unsupported_answers_are_rejected(version: str, payload: dict) -> None:
    with pytest.raises(ProviderContractViolation) as caught:
        parse_submission_response(version, payload)  # type: ignore[arg-type]
    assert "MOCK-DEPOT" not in str(caught.value)  # no provider content in the error


def test_a_declined_v2_submission_is_not_accepted() -> None:
    payload = _edit("v2", ("submission", "status"), "DECLINED")
    assert parse_submission_response("v2", payload).accepted is False


def test_the_adapter_cannot_be_configured_with_an_unknown_version() -> None:
    for versions in (("v3",), ("v2", "v3"), ()):
        with pytest.raises(ValueError, match="supported versions"):
            VersionedDepotAdapter(MockVersionedDepotApi(), versions=versions)


# --- negotiation ------------------------------------------------------------------------------


def test_the_newest_version_is_used_and_a_gone_version_falls_back_once() -> None:
    events: list[tuple[str, str, str]] = []
    newest = MockVersionedDepotApi()
    VersionedDepotAdapter(newest, on_event=lambda *e: events.append(e)).submit_application(
        APPLICATION
    )
    assert [call[0] for call in newest.calls] == ["v2"]

    events.clear()
    only_v1 = MockVersionedDepotApi(served=("v1",))
    result = VersionedDepotAdapter(
        only_v1, on_event=lambda *e: events.append(e)
    ).submit_application(APPLICATION)
    assert result.accepted is True
    assert [call[0] for call in only_v1.calls] == ["v1"]  # one hand-over, no duplicate
    assert [e[1:] for e in events] == [("v2", "version_unavailable"), ("v1", "answered")]


def test_no_supported_version_is_a_refused_submission() -> None:
    api = MockVersionedDepotApi(served=("v3",))
    events: list[tuple[str, str, str]] = []
    result = VersionedDepotAdapter(api, on_event=lambda *e: events.append(e)).submit_application(
        APPLICATION
    )
    assert result.accepted is False and api.calls == []
    assert events[-1][2] == "no_supported_version"


def test_a_malformed_answer_is_refused_and_never_retried_in_another_version() -> None:
    calls: list[str] = []

    def garbage(version: str, _request: dict) -> dict:
        calls.append(version)
        return {"contract_version": version, "status": "OPENED"}

    assert VersionedDepotAdapter(garbage).submit_application(APPLICATION).accepted is False
    assert calls == ["v2"]  # the provider may have acted, so asking again could duplicate it


# --- through the real bank --------------------------------------------------------------------


@pytest.mark.parametrize("served", [("v1",), ("v2",), ("v1", "v2")])
def test_every_supported_version_completes_the_same_journey(
    flow_factory: Callable[..., Flow], served: tuple[str, ...]
) -> None:
    api = MockVersionedDepotApi(served=served)
    flow = flow_factory(depot=VersionedDepotAdapter(api))
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    assert flow.agent().continue_after_customer(started.application_id).stopped_at is None
    # Still not open: the version changes the wire format, not who may open an account.
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert len(api.calls) == 1
    assert flow.provider_confirms(started.application_id).status_code == 200
    assert flow.status_of(started.application_id) == "OPENED"


def test_no_version_reaches_the_provider_before_consent(
    flow_factory: Callable[..., Flow],
) -> None:
    api = MockVersionedDepotApi()
    flow = flow_factory(depot=VersionedDepotAdapter(api))
    started = flow.start()
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit",
        headers=flow.onboarding_headers(started.application_id),
    )
    assert response.status_code == 409 and api.calls == []
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


@pytest.mark.parametrize(
    "transport",
    [
        lambda version, _request: {"contract_version": version, "opened": True},
        lambda _version, _request: {
            "contract_version": "v2",
            "submission": {"reference": "MOCK-DEPOT-x", "status": "OPENED"},
        },
        lambda _version, _request: {
            "contract_version": "v2",
            "submission": {"reference": "MOCK-DEPOT-x", "status": "DECLINED"},
        },
        MockVersionedDepotApi(served=()),  # every version is gone
    ],
    ids=["invented-field", "invented-status", "declined", "all-versions-gone"],
)
def test_a_bad_or_absent_provider_answer_fails_safely_and_advances_nothing(
    flow_factory: Callable[..., Flow], transport: Callable
) -> None:
    flow = flow_factory(depot=VersionedDepotAdapter(transport))
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    url = f"/v1/applications/{started.application_id}/submit"
    response = flow.agent_http.post(url, headers=flow.onboarding_headers(started.application_id))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unavailable"
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_after_a_version_outage_the_same_application_can_still_continue(
    flow_factory: Callable[..., Flow],
) -> None:
    api = MockVersionedDepotApi(served=())
    flow = flow_factory(depot=VersionedDepotAdapter(api))
    started = flow.start()
    flow.customer_confirm(started)
    url = f"/v1/applications/{started.application_id}/submit"
    headers = flow.onboarding_headers(started.application_id)
    assert flow.agent_http.post(url, headers=headers).status_code == 502
    api.served = ("v1",)  # the provider is reachable again, on the older version
    assert flow.agent_http.post(url, headers=headers).status_code == 202
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(1,)]
