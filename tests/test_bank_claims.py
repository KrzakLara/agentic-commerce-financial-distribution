"""Only statements backed by the verified catalog can be approved as official bank claims."""

import pytest

from app.integrations.catalog_fixture import (
    HOSTILE_INSTRUCTIONS,
    FixtureProviderDiscovery,
    prompt_injection_fixture,
)
from app.services.bank_claims import (
    SAFE_RESPONSE,
    Claim,
    ClaimDecision,
    ClaimKind,
    SourceCitation,
    manifest_citation,
    provider_identity_text,
    validate_claim,
)

CATALOG = FixtureProviderDiscovery()
PROVIDER = CATALOG.get_provider("fixture-bank")
PRODUCT = CATALOG.list_products("fixture-bank")[0]
PRICE_LIST = SourceCitation(
    document_id=PRODUCT.sources[0].document_id,
    document_version=PRODUCT.sources[0].document_version,
    document_digest=PRODUCT.sources[0].document_digest,
)
DISCLOSURE = SourceCitation(
    document_id=PRODUCT.risk_document_id,
    document_version=PRODUCT.risk_document_version,
    document_digest=PRODUCT.risk_document_digest,
)
MANIFEST = manifest_citation(PROVIDER)


def fee(text: str = "4.90 EUR", source: SourceCitation | None = PRICE_LIST, **kw: str) -> Claim:
    fields = {"subject": "order", "product_id": "depot-basic", **kw}
    return Claim(kind=ClaimKind.FEE, provider_id="fixture-bank", text=text, source=source, **fields)


def identity(text: str, source: SourceCitation | None = MANIFEST) -> Claim:
    return Claim(
        kind=ClaimKind.PROVIDER_IDENTITY, provider_id="fixture-bank", text=text, source=source
    )


def partner(subject: str, text: str, source: SourceCitation | None = MANIFEST) -> Claim:
    return Claim(
        kind=ClaimKind.PARTNER_ATTRIBUTION,
        provider_id="fixture-bank",
        subject=subject,
        text=text,
        source=source,
    )


def test_supported_claims_are_approved_with_their_verified_source() -> None:
    partner_name = next(
        c.partner_name for c in CATALOG.list_channels("fixture-bank") if c.channel_id == "partner-1"
    )
    for claim in (
        fee(),
        Claim(
            kind=ClaimKind.CONDITIONS,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text=PRODUCT.conditions or "",
            source=MANIFEST,
        ),
        Claim(
            kind=ClaimKind.RISK_SUMMARY,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text=PRODUCT.risk_summary or "",
            source=MANIFEST,
        ),
        identity(provider_identity_text(PROVIDER)),
        partner("partner-1", partner_name or ""),
    ):
        verdict = validate_claim(CATALOG, claim)
        assert verdict.approved, (claim, verdict)
        assert verdict.safe_response is None


@pytest.mark.parametrize(
    ("kind", "text", "wrong_source"),
    [
        (ClaimKind.CONDITIONS, PRODUCT.conditions, PRICE_LIST),
        (ClaimKind.RISK_SUMMARY, PRODUCT.risk_summary, DISCLOSURE),
    ],
)
def test_a_document_that_does_not_contain_the_text_is_no_valid_citation(
    kind: ClaimKind, text: str, wrong_source: SourceCitation
) -> None:
    """The price list says nothing about conditions, and the disclosure has other words."""
    document = CATALOG.get_document_text(wrong_source.document_id, wrong_source.document_version)
    assert text not in document
    verdict = validate_claim(
        CATALOG,
        Claim(
            kind=kind,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text=text,
            source=wrong_source,
        ),
    )
    assert verdict.decision is ClaimDecision.UNSUPPORTED
    assert verdict.reason == "no_verified_source_reference"


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        (fee("0.00 EUR"), "value_differs_from_the_catalog"),  # a fabricated fee
        (fee(source=None), "no_verified_source_reference"),
        (fee(source=DISCLOSURE), "no_verified_source_reference"),  # the wrong document
        (
            fee(source=PRICE_LIST.model_copy(update={"document_digest": "0" * 64})),
            "no_verified_source_reference",
        ),
        (
            identity("Trusted Global Bank AG (depot.examplebank.example)"),
            "provider_identity_differs_from_the_register",
        ),
        (identity(provider_identity_text(PROVIDER), source=None), "no_verified_source_reference"),
        (partner("partner-evil", "Evil Bank"), "not_a_registered_active_partner"),
        (
            partner("partner-2", "Example Valley Savings Bank (MOCK)"),
            "not_a_registered_active_partner",
        ),
        (
            partner("direct", "Sampletown Cooperative Bank (MOCK)"),
            "not_a_registered_active_partner",
        ),
        (
            Claim(kind=ClaimKind.FREE_TEXT, provider_id="fixture-bank", text="Our fees are low."),
            "free_text_is_not_an_official_claim",
        ),
    ],
)
def test_unsupported_statements_cannot_be_approved(claim: Claim, reason: str) -> None:
    verdict = validate_claim(CATALOG, claim)
    assert verdict.decision is ClaimDecision.UNSUPPORTED
    assert verdict.reason == reason and verdict.safe_response == SAFE_RESPONSE


@pytest.mark.parametrize(
    "text",
    [
        "This fund is guaranteed to grow.",
        "Risk-free returns of 8 percent.",
        "You cannot lose money with this account.",
        "I recommend this product.",
        "You should invest in this account now.",
        "This is the best product on the market.",
        "This product is right for you.",
    ],
)
@pytest.mark.parametrize("kind", [ClaimKind.FREE_TEXT, ClaimKind.CONDITIONS])
def test_guarantees_and_personal_recommendations_go_to_a_person(text: str, kind: ClaimKind) -> None:
    verdict = validate_claim(
        CATALOG,
        Claim(
            kind=kind,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text=text,
            source=PRICE_LIST,
        ),
    )
    assert verdict.decision is ClaimDecision.ESCALATE_TO_HUMAN
    assert verdict.reason == "guarantee_or_personal_recommendation"


@pytest.mark.parametrize(
    "claim",
    [
        fee(subject="performance-fee"),  # a fee the price list does not have
        fee(product_id="depot-premium"),  # an unknown product
        Claim(
            kind=ClaimKind.CONDITIONS, provider_id="rogue-bank", product_id="x", text="Any text."
        ),
    ],
)
def test_missing_information_gets_a_safe_answer_and_a_person(claim: Claim) -> None:
    verdict = validate_claim(CATALOG, claim)
    assert verdict.decision is ClaimDecision.ESCALATE_TO_HUMAN and not verdict.approved
    assert verdict.safe_response == SAFE_RESPONSE


def test_hostile_text_in_a_signed_catalog_is_never_approved_as_a_bank_claim() -> None:
    hostile = FixtureProviderDiscovery(prompt_injection_fixture())
    product = hostile.list_products("fixture-bank")[0]
    assert product.conditions == HOSTILE_INSTRUCTIONS  # the signed fixture really carries it
    verdict = validate_claim(
        hostile,
        Claim(
            kind=ClaimKind.CONDITIONS,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text=HOSTILE_INSTRUCTIONS,
            source=PRICE_LIST,
        ),
    )
    assert verdict.decision is ClaimDecision.ESCALATE_TO_HUMAN
    # An honest claim about that product escalates too: the catalog value itself is suspect.
    honest = validate_claim(
        hostile,
        Claim(
            kind=ClaimKind.CONDITIONS,
            provider_id="fixture-bank",
            product_id="depot-basic",
            text="No minimum deposit.",
            source=PRICE_LIST,
        ),
    )
    assert honest.decision is ClaimDecision.ESCALATE_TO_HUMAN
    assert honest.reason == "instruction_like_text_in_the_catalog"
