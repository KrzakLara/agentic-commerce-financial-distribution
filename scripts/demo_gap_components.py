"""Gap-closure demo: the isolated components, in-process, synthetic and offline.

    python -m scripts.demo_gap_components

Prints the outcomes of the provider contract (v1/v2), the knowledge-and-experience assessment, the
tax-information mock, the KYC and signing boundaries and the claim check. None of them runs in the
three onboarding demos; the provider contract's run through the bank is in
`tests/test_provider_contract.py`. No server, no database, no model, no key.
"""

import sys
from datetime import UTC, datetime, timedelta

from app.domain.application import DepotApplication
from app.integrations.catalog_fixture import FixtureProviderDiscovery
from app.integrations.mock import MockKycProvider
from app.integrations.provider_contract import MockVersionedDepotApi, VersionedDepotAdapter
from app.integrations.regulated_boundaries import (
    IdentityVerificationRequest,
    SignatureLevel,
    SignatureRequest,
    identity_result_from_check,
)
from app.services.appropriateness import (
    PROTOTYPE_NOTICE,
    SYNTHETIC_PROFILES,
    ExecutionOnlyFacts,
    ProductKnowledgeLevel,
    ServiceContext,
    assess,
    review_execution_only,
)
from app.services.bank_claims import (
    Claim,
    ClaimKind,
    SourceCitation,
    validate_claim,
)
from app.services.tax_information import TaxDataSource, validate_tax_information


def _application() -> DepotApplication:
    now = datetime.now(UTC)
    return DepotApplication(
        application_id="app-demo",
        provider_id="fixture-bank",
        product_id="depot-basic",
        channel_id="direct",
        customer_ref="synthetic-customer",
        agent_actor_id="synthetic-agent",
        created_at=now,
        updated_at=now,
    )


def _submit(served: tuple[str, ...]) -> tuple[bool, list[str]]:
    events: list[str] = []
    result = VersionedDepotAdapter(
        MockVersionedDepotApi(served=served),
        on_event=lambda _app, version, event: events.append(f"{version}:{event}"),
    ).submit_application(_application())
    return result.accepted, events


def _provider_contract() -> None:
    print("\n== Provider contract v1/v2 (MOCK transport; OPENED still needs the signed callback)")
    for label, served in (
        ("provider serves v1 and v2", ("v1", "v2")),
        ("v2 withdrawn", ("v1",)),
        ("v1 withdrawn", ("v2",)),
        ("no supported version left", ()),
    ):
        accepted, events = _submit(served)
        print(f"  {label:<28} accepted={accepted!s:<5}  {', '.join(events)}")


def _appropriateness() -> None:
    print(f"\n== Knowledge and experience (PROTOTYPE). {PROTOTYPE_NOTICE}")
    for name, answers in SYNTHETIC_PROFILES.items():
        result = assess(ServiceContext.ORDER_EXECUTION, ProductKnowledgeLevel.ADVANCED, answers)
        print(f"  {name:<24} order   -> {result.outcome.value:<26} {', '.join(result.reasons)}")
    opening = assess(
        ServiceContext.ACCOUNT_OPENING,
        ProductKnowledgeLevel.ADVANCED,
        SYNTHETIC_PROFILES["synthetic-novice"],
    )
    print(f"  {'synthetic-novice':<24} account -> {opening.outcome.value}")
    review = review_execution_only(ExecutionOnlyFacts(initiated_by_customer=True))
    print(f"  execution-only with one established fact -> {review.value}")


def _tax() -> None:
    print("\n== Tax information (standalone MOCK, synthetic identifier, statuses only)")
    good = {
        "tax_residency_country": "DE",
        "synthetic_tax_id": "SYNTH-TAX-000123",
        "us_person_declared": False,
    }
    for label, data, source in (
        ("complete", good, TaxDataSource.CUSTOMER_ON_BANK_CHANNEL),
        (
            "real-looking identifier",
            {**good, "synthetic_tax_id": "12345678901"},
            TaxDataSource.CUSTOMER_ON_BANK_CHANNEL,
        ),
        ("supplied by a model", good, TaxDataSource.AGENT_OR_MODEL),
    ):
        details = validate_tax_information(data, source).audit_details()
        print(f"  {label:<25} complete={details['complete']:<5} id={details['synthetic_tax_id']}")


def _boundaries() -> None:
    print("\n== KYC and signing boundaries (MOCK ONLY)")
    application = _application()
    request = IdentityVerificationRequest(
        request_id="idv-demo", application_id="app-demo", customer_ref="synthetic-customer"
    )
    identity = identity_result_from_check(request, MockKycProvider().verify_identity(application))
    print(f"  mock KYC {identity.outcome.value}: evidence={identity.is_regulatory_evidence}")
    try:
        SignatureRequest(
            request_id="sig-demo",
            application_id="app-demo",
            action_digest="0" * 64,
            level=SignatureLevel.QUALIFIED_VIA_QTSP,
            expires_at=application.created_at + timedelta(days=1),
        )
    except ValueError:
        print("  mock claiming a qualified signature -> refused")


def _claims() -> None:
    print("\n== Claim check (limited source governance, not supervision of free-form model text)")
    catalog = FixtureProviderDiscovery()
    product = catalog.list_products("fixture-bank")[0]
    source = product.sources[0]
    citation = SourceCitation(
        document_id=source.document_id,
        document_version=source.document_version,
        document_digest=source.document_digest,
    )
    for label, text in (
        ("the approved order fee", "4.90 EUR"),
        ("a fabricated fee", "0.00 EUR"),
        ("a guarantee", "Guaranteed to grow."),
    ):
        verdict = validate_claim(
            catalog,
            Claim(
                kind=ClaimKind.FEE,
                provider_id="fixture-bank",
                product_id="depot-basic",
                subject="order",
                text=text,
                source=citation,
            ),
        )
        print(f"  {label:<24} -> {verdict.decision.value:<18} {verdict.reason}")


def main() -> int:
    print("TRUSTEQ - gap-closure components (SYNTHETIC / MOCK, in-process, no model)")
    _provider_contract()
    _appropriateness()
    _tax()
    _boundaries()
    _claims()
    print("\nReminder: isolated demonstrations, not part of the onboarding journey.")
    print("None of this is regulatory compliance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
