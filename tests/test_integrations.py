from datetime import UTC, datetime

import pytest

from app.domain import ConsentReceipt, DepotApplication
from app.integrations.agent_tools import (
    TOOL_ARGUMENT_MODELS,
    AgentToolCall,
    AgentToolName,
)
from app.integrations.mock import (
    MockComplianceProvider,
    MockContractSigner,
    MockDepotProvider,
    MockKycProvider,
)
from app.integrations.ports import (
    AuditSinkPort,
    CompliancePort,
    ContractSigningPort,
    DepotProviderPort,
    KycPort,
    ProviderDiscoveryPort,
)
from app.integrations.results import CheckOutcome, ProviderSubmission

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


@pytest.fixture
def application() -> DepotApplication:
    return DepotApplication(
        application_id="app-1", provider_id="fixture-bank", product_id="depot-basic",
        channel_id="direct", customer_ref="opaque-id", agent_actor_id="agent-1",
        created_at=NOW, updated_at=NOW,
    )  # fmt: skip


def test_required_ports_exist() -> None:
    for port in (
        ProviderDiscoveryPort, KycPort, CompliancePort, ContractSigningPort,
        DepotProviderPort, AuditSinkPort,
    ):  # fmt: skip
        assert port.__name__.endswith("Port")


def test_mock_providers_satisfy_ports_and_are_marked_mock(application: DepotApplication) -> None:
    kyc: KycPort = MockKycProvider()
    compliance: CompliancePort = MockComplianceProvider()
    depot: DepotProviderPort = MockDepotProvider()
    contract: ContractSigningPort = MockContractSigner()

    assert kyc.verify_identity(application).mock is True
    assert kyc.verify_identity(application).reference.startswith("MOCK-")
    assert compliance.assess(application).mock is True
    assert depot.submit_application(application).mock is True
    consent = ConsentReceipt(
        receipt_id="r-1", application_id="app-1", customer_id="cust-1", handoff_id="h-1",
        action_digest="b" * 64, confirmed_at=NOW,
    )  # fmt: skip
    assert contract.record_signature(consent).mock is True
    for cls in (MockKycProvider, MockComplianceProvider, MockContractSigner, MockDepotProvider):
        assert cls.IS_MOCK is True


def test_mock_outcome_is_configurable_for_later_scenarios(application: DepotApplication) -> None:
    assert MockComplianceProvider().assess(application).outcome is CheckOutcome.PASS
    review = MockComplianceProvider(CheckOutcome.REVIEW_REQUIRED).assess(application)
    assert review.outcome is CheckOutcome.REVIEW_REQUIRED


def test_depot_submission_can_never_claim_opened() -> None:
    assert "opened" not in ProviderSubmission.model_fields
    assert not any("open" in name for name in ProviderSubmission.model_fields)


def test_agent_has_no_dangerous_tools() -> None:
    names = {tool.value for tool in AgentToolName}
    assert names == {
        "discover_provider", "get_official_product", "create_draft",
        "request_handoff", "get_generic_status", "submit_after_consent",
    }  # fmt: skip
    for forbidden in (
        "approve_kyc", "sign_contract", "set_opened", "set_status",
        "confirm_handoff", "approve_handoff", "grant_consent", "provider_confirmation",
    ):  # fmt: skip
        assert forbidden not in names
    assert set(TOOL_ARGUMENT_MODELS) == set(AgentToolName)


def test_tool_call_rejects_extra_and_missing_arguments() -> None:
    AgentToolCall(tool=AgentToolName.GET_GENERIC_STATUS, arguments={"application_id": "app-1"})
    with pytest.raises(ValueError, match="extra"):
        AgentToolCall(
            tool=AgentToolName.GET_GENERIC_STATUS,
            arguments={"application_id": "app-1", "status": "OPENED"},
        )
    with pytest.raises(ValueError, match="application_id"):
        AgentToolCall(tool=AgentToolName.REQUEST_HANDOFF, arguments={})
    with pytest.raises(ValueError, match="string_pattern_mismatch"):
        AgentToolCall(
            tool=AgentToolName.REQUEST_HANDOFF, arguments={"application_id": "../../etc; DROP"}
        )
