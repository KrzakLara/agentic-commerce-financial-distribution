"""Deterministic check of whether a statement may be presented as an official bank claim.

Limited source governance over STRUCTURED claims. A claim is approved only if its value equals the
verified catalog's value and it cites the catalog's own source for that value. Anything else is
unsupported or goes to a person, with a fixed safe answer.

Not covered: free-form model language. Nothing routes the agent's text through this check at
runtime (`app/agents/llm.py` is unchanged and its digest is recorded evidence), and the wording
filter below catches listed phrases, not every way to say something. No model is involved here,
and catalog text is compared as data, never followed.
"""

import re
from enum import StrEnum

from app.core.exceptions import AppError
from app.domain.base import DomainModel, OpaqueId
from app.domain.catalog import Channel, Product, Provider
from app.integrations.ports import ProviderDiscoveryPort

SAFE_RESPONSE = (
    "I can't confirm that from the bank's approved information. A member of the bank's staff can "
    "help you with this."
)
MANIFEST_SOURCE = "provider-manifest"

_OVERPROMISE = re.compile(
    r"\b(guarantee[ds]?|risk[- ]free|no risk|cannot lose|can't lose|safe investment|"
    r"recommend(ed|s)?|you should (buy|invest)|best (fund|product|choice)|right for you)\b",
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


def _approved_sources(kind: ClaimKind, product: Product) -> list[SourceCitation]:
    """The risk summary rests on the disclosure document; fees and conditions on the listed ones."""
    if kind is ClaimKind.RISK_SUMMARY:
        return [
            SourceCitation(
                document_id=product.risk_document_id,
                document_version=product.risk_document_version,
                document_digest=product.risk_document_digest,
            )
        ]
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


def _check_product_claim(claim: Claim, product: Product) -> ClaimVerdict:
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
    if claim.source not in _approved_sources(claim.kind, product):
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
    return _check_product_claim(claim, product)
