"""The bank's verified facts: everything the communication boundary may state, and nothing else.

Facts are rebuilt on every use from the verified catalog, so an expired manifest or a withdrawn
product stops supporting anything at that moment. A fact exists only if its citation really supports
it, and that is checked here, deterministically:

* a fee must appear next to its name in the cited document's own text;
* a document's text must match the digest the signed manifest states for it;
* a quoted sentence must be literally in the cited document;
* a field of the signed manifest cites the manifest itself, not a document that does not contain it.

A candidate that fails is not stated. It is kept in `withheld` with its reason, so a proposal naming
it is told why. This is about the sources, and it does not read meaning: which approved sentence may
be quoted is the bank's own list below.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.core.exceptions import AppError
from app.domain.catalog import Channel, Fee, Product, Provider
from app.integrations.ports import ProviderDiscoveryPort
from app.services.bank_claims import MANIFEST_SOURCE, ClaimKind, approved_claims, text_is_inert
from app.services.catalog_policy import product_is_current, provider_is_current
from app.services.communication_contract import (
    ClaimType,
    FactSource,
    ReasonCode,
    SourceConsulted,
    SupportKind,
    VerifiedFact,
)
from app.services.regulatory_policy import (
    POLICY_DATE,
    POLICY_SOURCE,
    POLICY_VERSION,
    Service,
    service_applicability,
)

_CANONICAL_AMOUNT = re.compile(r"^\d+(\.\d+)? [A-Z]{3}( p\.a\.)?$")
_RISK_CLASS = re.compile(r"^\d of \d( \([A-Za-z0-9 ]+\))?$")
_PRODUCT_NAME = re.compile(r"^[\w .,()&'/-]{1,120}$")
_FEE_WORDS = {"custody": ("custody",), "order": ("order", "trade", "transaction")}
# Sentences the bank allows to be quoted. Each must be literally in the cited document's text.
_APPROVED_QUOTES: dict[str, tuple[tuple[ClaimType, str, str], ...]] = {
    "kid-depot-basic": (
        (
            ClaimType.RISK_DISCLOSURE,
            "value_can_fall",
            "the value of securities can fall as well as rise",
        ),
        (ClaimType.RISK_DISCLOSURE, "capital_loss", "you may get back less than you invested"),
        (
            ClaimType.PRODUCT_LIMITATION,
            "no_real_product_described",
            "No real product, price or return is described here.",
        ),
    ),
    "pli-depot-basic": (
        (
            ClaimType.PRODUCT_LIMITATION,
            "demonstration_prices",
            "No real prices; for demonstration only.",
        ),
    ),
}
_SERVICE_STATEMENTS = {
    Service.ACCOUNT_OPENING: "This service prepares the opening of a synthetic securities account.",
    Service.PRODUCT_INFORMATION: (
        "This service gives non-personalized product information from approved sources."
    ),
    Service.INVESTMENT_ADVICE: "Investment advice is not offered through this service.",
    Service.PORTFOLIO_MANAGEMENT: "Portfolio management is not offered through this service.",
    Service.ORDER_NON_ADVISED: "Executing securities orders is not offered through this service.",
    Service.ORDER_EXECUTION_ONLY: (
        "Execution-only orders are not offered through this service, and no exemption is assumed."
    ),
}
_REGULATORY_STATEMENT = (
    "The bank's approved information does not establish a financial licence: no authoritative "
    "register lookup is available to this service."
)


def fact_id(provider_id: str, product_id: str | None, claim_type: ClaimType, name: str) -> str:
    return f"{provider_id}/{product_id or '-'}/{claim_type.value}/{name}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fee_is_stated_in(fee: Fee, document_text: str) -> bool:
    """The fee's name followed by its number and currency, with no other number between them."""
    number_and_currency = fee.amount.removesuffix(" p.a.")
    words = _FEE_WORDS.get(fee.code, (fee.code,))
    return any(
        re.search(
            rf"\b{re.escape(word)}\b[^0-9]{{0,40}}{re.escape(number_and_currency)}\b",
            document_text,
            re.IGNORECASE,
        )  # fmt: skip
        for word in words
    )


@dataclass(frozen=True)
class FactLookup:
    """The verified facts of one provider and, optionally, one product, at one moment."""

    provider_id: str
    product_id: str | None
    facts: tuple[VerifiedFact, ...] = ()
    withheld: dict[str, ReasonCode] = field(default_factory=dict)
    sources: tuple[SourceConsulted, ...] = ()
    source_texts: tuple[str, ...] = ()
    problem: ReasonCode | None = None

    def by_id(self) -> dict[str, VerifiedFact]:
        return {fact.claim_id: fact for fact in self.facts}


class _Builder:
    def __init__(
        self, catalog: ProviderDiscoveryPort, provider: Provider, product: Product | None
    ) -> None:
        self.catalog = catalog
        self.provider = provider
        self.product = product
        self.facts: list[VerifiedFact] = []
        self.withheld: dict[str, ReasonCode] = {}
        self.documents: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def manifest_source(self) -> FactSource:
        provider = self.provider
        return FactSource(
            source_id=MANIFEST_SOURCE,
            source_version=provider.manifest_version,
            source_timestamp=provider.issued_at.isoformat() if provider.issued_at else None,
            integrity_digest=provider.manifest_digest,
            support=SupportKind.SIGNED_MANIFEST_FIELD,
        )

    @property
    def manifest_note(self) -> str:
        return f"according to the verified manifest version {self.provider.manifest_version}"

    def add(
        self,
        claim_type: ClaimType,
        name: str,
        value: str,
        statement: str,
        source: FactSource,
        *,
        narrative: bool = False,
        provider_level: bool = False,
    ) -> None:
        product = None if provider_level else self.product
        self.facts.append(
            VerifiedFact(
                claim_id=fact_id(
                    self.provider.provider_id,
                    product.product_id if product else None,
                    claim_type,
                    name,
                ),
                provider_id=self.provider.provider_id,
                product_id=product.product_id if product else None,
                product_version=product.catalog_version if product else None,
                claim_type=claim_type,
                field=name,
                canonical_value=value,
                statement=statement,
                source=source,
                narrative=narrative,
                mock=self.provider.is_mock,
            )
        )

    def withhold(self, claim_type: ClaimType, name: str, reason: ReasonCode) -> None:
        product_id = self.product.product_id if self.product else None
        self.withheld[fact_id(self.provider.provider_id, product_id, claim_type, name)] = reason

    def document_text(self, document_id: str, version: str, digest: str) -> str | None:
        """The text, only if it still matches the digest the signed manifest states for it."""
        try:
            text = self.catalog.get_document_text(document_id, version)
        except (AppError, KeyError, ValueError):
            return None
        if _digest(text) != digest:
            return None
        self.documents[(document_id, version)] = (digest, text)
        return text

    def document_source(
        self, document_id: str, version: str, digest: str, support: SupportKind, span: str | None
    ) -> FactSource:
        published = self.product.published_at if self.product else None
        return FactSource(
            source_id=document_id,
            source_version=version,
            source_timestamp=published.isoformat() if published else None,
            integrity_digest=digest,
            support=support,
            evidence_span=span,
        )

    # --- the provider ----------------------------------------------------------------------------

    def provider_facts(self) -> None:
        provider = self.provider
        self.add(
            ClaimType.PROVIDER_IDENTITY,
            "legal_identity",
            f"{provider.legal_name} ({provider.verified_domain})",
            f"The verified provider is {provider.legal_name} ({provider.verified_domain}), "
            f"{self.manifest_note}.",
            self.manifest_source,
            provider_level=True,
        )
        policy = FactSource(
            source_id=POLICY_SOURCE,
            source_version=POLICY_VERSION,
            source_timestamp=POLICY_DATE,
            integrity_digest=None,
            support=SupportKind.BANK_POLICY_VERSION,
        )
        self.add(
            ClaimType.REGULATORY_STATUS,
            "licence",
            "NOT_ESTABLISHED",
            _REGULATORY_STATEMENT,
            policy,
            provider_level=True,
        )
        for service, decision in service_applicability({}).items():
            self.add(
                ClaimType.SERVICE_SCOPE,
                service,
                "OFFERED" if decision["offered"] else "NOT_OFFERED",
                _SERVICE_STATEMENTS[Service(service)],
                policy,
                provider_level=True,
            )

    # --- one product -----------------------------------------------------------------------------

    def product_facts(self, channels: list[Channel]) -> None:
        if self.product is None:
            return
        self._approved_claims()
        self._identity_and_availability(channels)
        self._risk_class()
        self._documents_and_quotes()
        self._eligibility()

    def _approved_claims(self) -> None:
        product = self.product
        if product is None:
            return
        for claim in approved_claims(self.catalog, self.provider.provider_id, product.product_id):
            if claim.kind is ClaimKind.FEE:
                self._fee(claim.subject or "", claim.value)
            elif claim.kind is ClaimKind.RISK_SUMMARY:
                self.add(
                    ClaimType.RISK_DISCLOSURE,
                    "summary",
                    claim.value,
                    claim.statement,
                    self.manifest_source,
                    narrative=True,
                )
            elif claim.kind is ClaimKind.CONDITIONS:
                self.add(
                    ClaimType.CONTRACT_TERM,
                    "conditions",
                    claim.value,
                    claim.statement,
                    self.manifest_source,
                    narrative=True,
                )
            elif claim.kind is ClaimKind.PARTNER_ATTRIBUTION:
                self.add(
                    ClaimType.PARTNER_ATTRIBUTION,
                    claim.subject or "",
                    claim.value,
                    claim.statement,
                    self.manifest_source,
                )
        for name, text in (
            ("summary", product.risk_summary),
            ("conditions", product.conditions),
        ):
            claim_type = ClaimType.RISK_DISCLOSURE if name == "summary" else ClaimType.CONTRACT_TERM
            if text and not text_is_inert(text):
                self.withhold(claim_type, name, ReasonCode.SOURCE_TEXT_UNSAFE)

    def _fee(self, code: str, amount: str) -> None:
        product = self.product
        if product is None:
            return
        if not _CANONICAL_AMOUNT.fullmatch(amount):
            self.withhold(ClaimType.FEE, code, ReasonCode.SOURCE_TEXT_UNSAFE)
            return
        fee = next((item for item in product.fees if item.code == code), None)
        for source in product.sources:
            text = self.document_text(
                source.document_id, source.document_version, source.document_digest
            )
            if fee is not None and text is not None and fee_is_stated_in(fee, text):
                self.add(
                    ClaimType.FEE,
                    code,
                    amount,
                    f"According to the verified synthetic price list {source.document_id} version "
                    f"{source.document_version}, the {code} fee of {product.product_id} is "
                    f"{amount}",
                    self.document_source(
                        source.document_id,
                        source.document_version,
                        source.document_digest,
                        SupportKind.VALUE_IN_DOCUMENT_TEXT,
                        amount.removesuffix(" p.a."),
                    ),
                )
                return
        self.withhold(ClaimType.FEE, code, ReasonCode.SOURCE_DOES_NOT_SUPPORT_CLAIM)

    def _identity_and_availability(self, channels: list[Channel]) -> None:
        product = self.product
        if product is None:
            return
        named = _PRODUCT_NAME.fullmatch(product.name) and text_is_inert(product.name)
        if named:
            self.add(
                ClaimType.PRODUCT_IDENTITY,
                "name",
                product.name,
                f"Product {product.product_id} is {product.name}, version "
                f"{product.catalog_version}, {self.manifest_note}.",
                self.manifest_source,
            )
        else:
            self.withhold(ClaimType.PRODUCT_IDENTITY, "name", ReasonCode.SOURCE_TEXT_UNSAFE)
        self.add(
            ClaimType.PRODUCT_AVAILABILITY,
            "status",
            "OFFERED",
            f"Product {product.product_id} is currently offered, {self.manifest_note}.",
            self.manifest_source,
        )
        for channel in channels:
            if product.product_id not in channel.product_ids and channel.product_ids:
                continue
            available = channel.serves(product.product_id)
            self.add(
                ClaimType.PRODUCT_AVAILABILITY,
                f"channel:{channel.channel_id}",
                "AVAILABLE" if available else "NOT_AVAILABLE",
                f"Product {product.product_id} is "
                f"{'available' if available else 'not available'} through channel "
                f"{channel.channel_id}, {self.manifest_note}.",
                self.manifest_source,
            )

    def _risk_class(self) -> None:
        product = self.product
        if product is None or not product.risk_class:
            return
        if not _RISK_CLASS.fullmatch(product.risk_class):
            self.withhold(
                ClaimType.RISK_CLASSIFICATION, "risk_class", ReasonCode.SOURCE_TEXT_UNSAFE
            )
            return
        self.add(
            ClaimType.RISK_CLASSIFICATION,
            "risk_class",
            product.risk_class,
            f"The risk class of {product.product_id} is {product.risk_class}, "
            f"{self.manifest_note}.",
            self.manifest_source,
        )

    def _documents_and_quotes(self) -> None:
        product = self.product
        if product is None:
            return
        references = [
            (product.risk_document_id, product.risk_document_version, product.risk_document_digest),
            *(
                (source.document_id, source.document_version, source.document_digest)
                for source in product.sources
            ),
        ]
        for document_id, version, digest in references:
            text = self.document_text(document_id, version, digest)
            name = f"document:{document_id}"
            if text is None:
                self.withhold(
                    ClaimType.DOCUMENT_VERSION, name, ReasonCode.SOURCE_DOES_NOT_SUPPORT_CLAIM
                )
                continue
            self.add(
                ClaimType.DOCUMENT_VERSION,
                name,
                version,
                f"The approved document {document_id} version {version} has the SHA-256 digest "
                f"{digest}.",
                self.document_source(
                    document_id, version, digest, SupportKind.DOCUMENT_DIGEST_VERIFIED, None
                ),
            )
            for claim_type, field_name, quote in _APPROVED_QUOTES.get(document_id, ()):
                if quote not in text:
                    self.withhold(claim_type, field_name, ReasonCode.SOURCE_DOES_NOT_SUPPORT_CLAIM)
                    continue
                cited = f"the verified document {document_id} version {version}"
                self.add(
                    claim_type,
                    field_name,
                    quote,
                    f'According to {cited}: "{quote}"',
                    self.document_source(
                        document_id, version, digest, SupportKind.VALUE_IN_DOCUMENT_TEXT, quote
                    ),
                )

    def _eligibility(self) -> None:
        product = self.product
        if product is None:
            return
        for rule in product.eligibility_rules:
            if not text_is_inert(rule.text):
                self.withhold(ClaimType.ELIGIBILITY_RULE, rule.code, ReasonCode.SOURCE_TEXT_UNSAFE)
                continue
            self.add(
                ClaimType.ELIGIBILITY_RULE,
                rule.code,
                rule.text,
                f"A published eligibility rule of {product.product_id}, {self.manifest_note}: "
                f"{rule.text} Whether a person meets it is decided by the bank.",
                self.manifest_source,
                narrative=True,
            )

    # --- the result ------------------------------------------------------------------------------

    def lookup(self) -> FactLookup:
        manifest = self.manifest_source
        sources = [
            SourceConsulted(
                source_id=manifest.source_id,
                source_version=manifest.source_version,
                source_timestamp=manifest.source_timestamp,
                integrity_digest=manifest.integrity_digest,
            )
        ]
        published = self.product.published_at if self.product else None
        sources += [
            SourceConsulted(
                source_id=document_id,
                source_version=version,
                source_timestamp=published.isoformat() if published else None,
                integrity_digest=digest,
            )
            for (document_id, version), (digest, _) in self.documents.items()
        ]
        return FactLookup(
            provider_id=self.provider.provider_id,
            product_id=self.product.product_id if self.product else None,
            facts=tuple(self.facts),
            withheld=dict(self.withheld),
            sources=tuple(sources),
            source_texts=tuple(text for _, text in self.documents.values()),
        )


def build_fact_lookup(
    catalog: ProviderDiscoveryPort, provider_id: str, product_id: str | None, now: datetime
) -> FactLookup:
    """Current facts, or the reason there are none. Unknown or expired: nothing to state."""
    try:
        provider = catalog.get_provider(provider_id)
        products = list(catalog.list_products(provider_id))
        channels = list(catalog.list_channels(provider_id))
    except AppError:
        return FactLookup(provider_id, product_id, problem=ReasonCode.UNKNOWN_PROVIDER)
    if not provider_is_current(provider, now):
        return FactLookup(provider_id, product_id, problem=ReasonCode.SOURCE_EXPIRED)
    product = None
    if product_id is not None:
        product = next((item for item in products if item.product_id == product_id), None)
        if product is None:
            return FactLookup(provider_id, product_id, problem=ReasonCode.UNKNOWN_PRODUCT)
        if not product_is_current(product, now):
            return FactLookup(provider_id, product_id, problem=ReasonCode.PRODUCT_NOT_CURRENT)
    builder = _Builder(catalog, provider, product)
    builder.provider_facts()
    builder.product_facts(channels)
    return builder.lookup()
