"""Deterministic check of whether a statement may be presented as an official bank claim.

Limited source governance over STRUCTURED claims. A claim is approved only if its value equals the
verified catalog's value and it cites the catalog's own source for that value. Anything else is
unsupported or goes to a person, with a fixed safe answer.

In the journey: `approved_claims` builds what the bank states about a product, and every entry
passes `validate_claim` first. It is served at
``GET /v1/providers/{id}/products/{id}/approved-claims``, and the agent tool `get_official_product`
carries the structured kinds to the agent. ``POST /v1/claims/verify`` lets any client check a
structured claim. `app.services.verified_facts` builds the communication boundary's facts on these.

A citation has to support its claim. Conditions and the risk summary are fields of the signed
manifest and appear in no document's text, so the manifest is what they cite.

Not covered: free-form model language. This module checks structured claims only, and the wording
filter below catches listed phrases, not every way to say something. The bank controls what it
publishes and signs, not what a customer's personal agent then writes. No model is involved here,
and catalog text is compared as data, never followed.
"""

import re
from collections.abc import Iterator, Sequence
from enum import StrEnum

from app.core.exceptions import AppError
from app.domain.base import DomainModel, OpaqueId
from app.domain.catalog import Channel, Product, Provider, SourceReference
from app.integrations.ports import ProviderDiscoveryPort

SAFE_RESPONSE = (
    "I can't confirm that from the bank's approved information. You can ask for a person on the "
    "bank's customer page."
)
MANIFEST_SOURCE = "provider-manifest"

_OVERPROMISE = re.compile(
    r"\b(guarantee[ds]?|risk[- ]free|no risk|cannot lose|can't lose|safe investment|"
    r"recommend(ed|s)?|you should (buy|invest)|best (fund|product|choice)|right for you|"
    r"will (definitely |certainly |surely )?(approve|be approved|accept|open)|"
    r"definitely (approve|be approved|accept|open))\b",
    re.IGNORECASE,
)
_INSTRUCTION = re.compile(
    r"ignore (all |any )?previous|\bsystem\s*:|approve_consent|set status|<script",
    re.IGNORECASE,
)


class ClaimKind(StrEnum):
    FEE = "FEE"
    CONDITIONS = "CONDITIONS"
    RISK_SUMMARY = "RISK_SUMMARY"
    PROVIDER_IDENTITY = "PROVIDER_IDENTITY"
    PARTNER_ATTRIBUTION = "PARTNER_ATTRIBUTION"
    FREE_TEXT = "FREE_TEXT"


class ClaimDecision(StrEnum):
    APPROVED = "APPROVED"
    UNSUPPORTED = "UNSUPPORTED"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"


class SourceCitation(DomainModel):
    document_id: OpaqueId
    document_version: str
    document_digest: str


class Claim(DomainModel):
    kind: ClaimKind
    provider_id: str
    product_id: str | None = None
    subject: str | None = None  # the fee code or the channel id
    text: str
    source: SourceCitation | None = None


class ClaimVerdict(DomainModel):
    decision: ClaimDecision
    reason: str
    safe_response: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision is ClaimDecision.APPROVED


def text_is_inert(text: str) -> bool:
    """False for text that promises, recommends or instructs: such text is never a bank fact."""
    return not (_OVERPROMISE.search(text) or _INSTRUCTION.search(text))


def _verdict(decision: ClaimDecision, reason: str) -> ClaimVerdict:
    return ClaimVerdict(decision=decision, reason=reason, safe_response=SAFE_RESPONSE)


def _unsupported(reason: str) -> ClaimVerdict:
    return _verdict(ClaimDecision.UNSUPPORTED, reason)


def _escalate(reason: str) -> ClaimVerdict:
    return _verdict(ClaimDecision.ESCALATE_TO_HUMAN, reason)


def manifest_citation(provider: Provider) -> SourceCitation:
    return SourceCitation(
        document_id=MANIFEST_SOURCE,
        document_version=provider.manifest_version,
        document_digest=provider.manifest_digest,
    )


def _approved_sources(
    kind: ClaimKind, product: Product, provider: Provider
) -> list[SourceCitation]:
    """What supports the claim. Narrative fields are in the signed manifest, not in a document."""
    if kind in {ClaimKind.RISK_SUMMARY, ClaimKind.CONDITIONS}:
        return [manifest_citation(provider)]
    return [
        SourceCitation(
            document_id=s.document_id,
            document_version=s.document_version,
            document_digest=s.document_digest,
        )
        for s in product.sources
    ]


def provider_identity_text(provider: Provider) -> str:
    return f"{provider.legal_name} ({provider.verified_domain})"


def _check_product_claim(claim: Claim, product: Product, provider: Provider) -> ClaimVerdict:
    if claim.kind is ClaimKind.FEE:
        fee = next((f for f in product.fees if f.code == claim.subject), None)
        if fee is None:
            return _escalate("fee_not_in_the_approved_price_list")
        approved_value = fee.amount
    elif claim.kind is ClaimKind.CONDITIONS:
        approved_value = product.conditions
    else:
        approved_value = product.risk_summary
    if approved_value is None:
        return _escalate("information_missing")
    if _INSTRUCTION.search(approved_value):
        return _escalate("instruction_like_text_in_the_catalog")
    if claim.text != approved_value:
        return _unsupported("value_differs_from_the_catalog")
    if claim.source not in _approved_sources(claim.kind, product, provider):
        return _unsupported("no_verified_source_reference")
    return ClaimVerdict(decision=ClaimDecision.APPROVED, reason="matches_the_verified_catalog")


def _check_partner_claim(claim: Claim, provider: Provider, channels: list[Channel]) -> ClaimVerdict:
    channel = next((c for c in channels if c.channel_id == claim.subject), None)
    if channel is None or not channel.active or channel.partner_name is None:
        return _unsupported("not_a_registered_active_partner")
    if claim.text != channel.partner_name:
        return _unsupported("value_differs_from_the_register")
    if claim.source != manifest_citation(provider):
        return _unsupported("no_verified_source_reference")
    return ClaimVerdict(decision=ClaimDecision.APPROVED, reason="matches_the_register")


def validate_claim(catalog: ProviderDiscoveryPort, claim: Claim) -> ClaimVerdict:
    """Fail closed: any doubt is unsupported or goes to a person, never approved."""
    if _OVERPROMISE.search(claim.text):
        return _escalate("guarantee_or_personal_recommendation")
    if _INSTRUCTION.search(claim.text):
        return _escalate("instruction_like_text")
    if claim.kind is ClaimKind.FREE_TEXT:
        return _unsupported("free_text_is_not_an_official_claim")
    try:
        provider = catalog.get_provider(claim.provider_id)
        if claim.kind is ClaimKind.PROVIDER_IDENTITY:
            if claim.text != provider_identity_text(provider):
                return _unsupported("provider_identity_differs_from_the_register")
            if claim.source != manifest_citation(provider):
                return _unsupported("no_verified_source_reference")
            return ClaimVerdict(decision=ClaimDecision.APPROVED, reason="matches_the_register")
        if claim.kind is ClaimKind.PARTNER_ATTRIBUTION:
            return _check_partner_claim(
                claim, provider, list(catalog.list_channels(provider.provider_id))
            )
        product = next(
            (
                p
                for p in catalog.list_products(provider.provider_id)
                if p.product_id == claim.product_id
            ),
            None,
        )
    except AppError:  # unknown provider, failed verification, expired manifest
        return _escalate("information_missing")
    if product is None:
        return _escalate("information_missing")
    return _check_product_claim(claim, product, provider)


class ApprovedClaim(DomainModel):
    """The narrow contract of a bank-approved fact: value, source, version. Nothing else."""

    kind: ClaimKind
    provider_id: str
    product_id: str | None = None
    product_version: str | None = None
    subject: str | None = None
    value: str
    statement: str
    source: SourceCitation
    decision: ClaimDecision = ClaimDecision.APPROVED
    mock: bool = True


def _citation(source: SourceReference) -> SourceCitation:
    return SourceCitation(
        document_id=source.document_id,
        document_version=source.document_version,
        document_digest=source.document_digest,
    )


def _candidates(
    provider: Provider, product: Product, channels: Sequence[Channel]
) -> Iterator[tuple[Claim, str]]:
    """Every statement the bank can make about this product, from its verified catalog only."""
    identifiers = {"provider_id": provider.provider_id, "product_id": product.product_id}
    identity = provider_identity_text(provider)
    yield (
        Claim(
            kind=ClaimKind.PROVIDER_IDENTITY,
            provider_id=provider.provider_id,
            text=identity,
            source=manifest_citation(provider),
        ),
        f"The verified provider is {identity}, according to its verified manifest "
        f"version {provider.manifest_version}.",
    )
    for channel in channels:
        if channel.partner_name is None or not channel.serves(product.product_id):
            continue
        yield (
            Claim(
                kind=ClaimKind.PARTNER_ATTRIBUTION,
                provider_id=provider.provider_id,
                subject=channel.channel_id,
                text=channel.partner_name,
                source=manifest_citation(provider),
            ),
            f"{channel.partner_name} is a registered distribution partner on channel "
            f"{channel.channel_id}, according to the verified register.",
        )
    manifest = manifest_citation(provider)
    if product.risk_summary:
        yield (
            Claim(
                kind=ClaimKind.RISK_SUMMARY,
                text=product.risk_summary,
                source=manifest,
                **identifiers,
            ),
            f"According to the verified manifest version {manifest.document_version}: "
            f"{product.risk_summary}",
        )
    if product.conditions:
        yield (
            Claim(
                kind=ClaimKind.CONDITIONS,
                text=product.conditions,
                source=manifest,
                **identifiers,
            ),
            f"According to the verified manifest version {manifest.document_version}: "
            f"{product.conditions}",
        )
    if not product.sources:
        return  # no source to cite: no fee may be stated
    source = _citation(product.sources[0])
    for fee in product.fees:
        yield (
            Claim(
                kind=ClaimKind.FEE,
                subject=fee.code,
                text=fee.amount,
                source=source,
                **identifiers,
            ),
            f"According to the verified synthetic price list {source.document_id} version "
            f"{source.document_version}, the {fee.code} fee of {product.product_id} is "
            f"{fee.amount}",
        )


def approved_claims(
    catalog: ProviderDiscoveryPort, provider_id: str, product_id: str
) -> list[ApprovedClaim]:
    """What the bank may state about this product. Each entry passed `validate_claim`.

    A catalog value the check refuses (instruction-like text, for example) is simply absent, so an
    agent has nothing to repeat. Unknown provider or product: an empty list, never a guess.
    """
    try:
        provider = catalog.get_provider(provider_id)
        product = next(
            (p for p in catalog.list_products(provider_id) if p.product_id == product_id), None
        )
        channels = list(catalog.list_channels(provider_id))
    except AppError:
        return []
    if product is None:
        return []
    return [
        ApprovedClaim(
            kind=claim.kind,
            provider_id=provider.provider_id,
            product_id=claim.product_id,
            product_version=product.catalog_version,
            subject=claim.subject,
            value=claim.text,
            statement=statement,
            source=claim.source,
            mock=provider.is_mock,
        )
        for claim, statement in _candidates(provider, product, channels)
        if claim.source is not None and validate_claim(catalog, claim).approved
    ]
