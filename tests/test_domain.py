from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain import (
    AgentActor,
    ApplicationStatus,
    ApprovedAction,
    AuditActorType,
    AuditEvent,
    AuditOutcome,
    ConsentReceipt,
    Customer,
    DepotApplication,
    DisclosureReceipt,
    Handoff,
    Mandate,
    MandateScope,
    ManualReviewCase,
    Product,
    Provider,
    ProviderRole,
    ReviewCaseReason,
    TrustZone,
)

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


def test_all_required_contracts_are_exported() -> None:
    import app.domain as domain

    expected = {
        "Customer", "AgentActor", "Mandate", "Provider", "Product", "DepotApplication",
        "Handoff", "ConsentReceipt", "DisclosureReceipt", "AuditEvent", "ManualReviewCase",
    }  # fmt: skip
    assert expected <= set(domain.__all__)


def test_agent_actor_is_always_untrusted_and_separate_from_customer() -> None:
    agent = AgentActor(
        agent_actor_id="agent-1", operator_id="op-1", display_name="Demo", created_at=NOW
    )
    assert agent.trust_zone is TrustZone.T0B_EXTERNAL_AGENT
    assert not hasattr(agent, "customer_id")
    with pytest.raises(ValidationError):
        AgentActor(
            agent_actor_id="agent-1", operator_id="op-1", display_name="Demo",
            created_at=NOW, trust_zone=TrustZone.T2_CORE,
        )  # fmt: skip


def test_models_are_immutable_and_reject_unknown_fields() -> None:
    customer = Customer(customer_id="cust-1", created_at=NOW)
    with pytest.raises(ValidationError):
        customer.customer_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Customer(customer_id="cust-1", created_at=NOW, is_admin=True)  # type: ignore[call-arg]


def test_naive_datetimes_and_non_opaque_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Customer(customer_id="cust-1", created_at=datetime(2026, 11, 3))  # noqa: DTZ001
    with pytest.raises(ValidationError):
        Customer(customer_id="Max Mustermann, Berlin", created_at=NOW)


def test_new_application_starts_as_draft() -> None:
    application = DepotApplication(
        application_id="app-1", provider_id="fixture-bank", product_id="depot-basic",
        channel_id="direct", customer_ref="opaque-id", agent_actor_id="agent-1",
        created_at=NOW, updated_at=NOW,
    )  # fmt: skip
    assert application.status is ApplicationStatus.DRAFT


def test_application_status_values_match_the_plan() -> None:
    assert {s.value for s in ApplicationStatus} == {
        "DRAFT", "AWAITING_AUTHORIZATION", "AUTHORIZED", "KYC_PENDING", "COMPLIANCE_PENDING",
        "CONTRACT_PENDING", "SUBMITTED_TO_DEPOT_PROVIDER", "OPENED", "ACTION_REQUIRED",
        "MANUAL_REVIEW", "REJECTED", "CANCELLED", "FAILED",
    }  # fmt: skip


def test_mandate_requires_scope_and_valid_expiry() -> None:
    base = dict(
        mandate_id="m-1", jti="j-1", agent_actor_id="agent-1", customer_id="cust-1",
        audience="trusteq-api", issued_at=NOW,
    )  # fmt: skip
    ok = Mandate(**base, scopes={MandateScope.DISCOVER}, expires_at=NOW + timedelta(minutes=15))
    assert ok.revoked_at is None
    with pytest.raises(ValidationError):
        Mandate(**base, scopes=set(), expires_at=NOW + timedelta(minutes=15))
    with pytest.raises(ValidationError):
        Mandate(**base, scopes={MandateScope.DISCOVER}, expires_at=NOW)


ACTION = ApprovedAction(
    application_id="app-1", provider_id="fixture-bank", product_id="depot-basic",
    channel_id="direct", customer_ref="opaque-id", catalog_version="2026.11",
    document_id="kid-depot-basic", document_version="1.0", document_digest=DIGEST,
)  # fmt: skip


def test_handoff_stores_nonce_hash_and_is_unbound_until_confirmed() -> None:
    handoff = Handoff(
        handoff_id="h-1", application_id="app-1", nonce_hash=DIGEST, action=ACTION,
        action_digest=ACTION.digest(), created_at=NOW, expires_at=NOW + timedelta(minutes=10),
    )  # fmt: skip
    assert handoff.customer_id is None and handoff.consumed_at is None
    assert handoff.revoked_at is None
    with pytest.raises(ValidationError):
        Handoff(
            handoff_id="h-1", application_id="app-1", nonce_hash="not-a-digest", action=ACTION,
            action_digest=ACTION.digest(), created_at=NOW, expires_at=NOW + timedelta(minutes=10),
        )  # fmt: skip
    with pytest.raises(ValidationError):
        Handoff(
            handoff_id="h-1", application_id="app-1", nonce_hash=DIGEST, action=ACTION,
            action_digest=ACTION.digest(), created_at=NOW, expires_at=NOW - timedelta(minutes=1),
        )  # fmt: skip


def test_handoff_digest_must_match_the_action_it_claims_to_fix() -> None:
    kwargs = dict(
        handoff_id="h-1", application_id="app-1", nonce_hash=DIGEST,
        created_at=NOW, expires_at=NOW + timedelta(minutes=10),
    )  # fmt: skip
    with pytest.raises(ValidationError, match="action_digest"):
        Handoff(**kwargs, action=ACTION, action_digest=DIGEST)  # digest of nothing in particular
    other = ACTION.model_copy(update={"channel_id": "partner-1"})
    with pytest.raises(ValidationError, match="action_digest"):
        Handoff(**kwargs, action=other, action_digest=ACTION.digest())  # swapped action
    with pytest.raises(ValidationError, match="different application"):
        Handoff(
            **{**kwargs, "application_id": "app-2"}, action=ACTION, action_digest=ACTION.digest()
        )


def test_every_field_of_the_approved_action_changes_its_digest() -> None:
    base = ACTION.digest()
    for field, value in {
        "application_id": "app-2", "provider_id": "other", "product_id": "other",
        "channel_id": "partner-1", "customer_ref": "someone-else", "catalog_version": "2027.01",
        "document_id": "other-doc", "document_version": "2.0", "document_digest": "d" * 64,
    }.items():  # fmt: skip
        assert ACTION.model_copy(update={field: value}).digest() != base, field


def test_catalog_receipts_audit_and_review_contracts_construct() -> None:
    Provider(
        provider_id="fixture-bank", legal_name="Fixture Bank AG (MOCK)",
        role=ProviderRole.DEPOT_PROVIDER, manifest_version="1.0", manifest_digest=DIGEST,
    )  # fmt: skip
    assert Provider.model_fields["is_mock"].default is True
    Product(
        product_id="depot-basic", provider_id="fixture-bank", name="Basic depot",
        catalog_version="1", risk_document_id="kid-1", risk_document_version="1.0",
        risk_document_digest=DIGEST,
    )  # fmt: skip
    ConsentReceipt(
        receipt_id="r-1", application_id="app-1", customer_id="cust-1", handoff_id="h-1",
        action_digest=DIGEST, confirmed_at=NOW,
    )  # fmt: skip
    DisclosureReceipt(
        receipt_id="r-2", application_id="app-1", customer_id="cust-1", document_id="kid-1",
        document_version="1.0", document_digest=DIGEST, acknowledged_at=NOW,
    )  # fmt: skip
    event = AuditEvent(
        event_id="e-1", occurred_at=NOW, correlation_id="corr-1", event_type="application.created",
        actor_type=AuditActorType.AGENT, outcome=AuditOutcome.SUCCESS,
    )  # fmt: skip
    assert event.details == {}
    case = ManualReviewCase(
        case_id="c-1", application_id="app-1",
        reason=ReviewCaseReason.COMPLIANCE_REVIEW_REQUIRED, created_at=NOW,
    )  # fmt: skip
    assert case.status.value == "OPEN"


def test_domain_models_are_not_the_api_schemas() -> None:
    from app.api.schemas.health import HealthResponse

    assert not issubclass(HealthResponse, Customer)
    assert HealthResponse.__module__.startswith("app.api.schemas")
    assert Customer.__module__.startswith("app.domain")
