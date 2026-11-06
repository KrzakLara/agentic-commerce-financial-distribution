"""What an independent agent can read about this provider, without scraping a website.

Publishes four things, kept deliberately apart:

* the A2A Agent Card (A2A 1.0): how to talk to this service;
* the OpenID Federation Entity Configuration: the key and the authority a verifier follows;
* the provider profile: canonical identity, sources, escalation and capabilities, signed;
* the catalog: versioned products with approved facts, sources, dates and digests.

A signature proves the content is unchanged. Whether the organization is who it says is the job of a
trust authority that the verifier configures itself. Whether it may provide financial services is a
question for an authoritative register, which this project cannot query, so the answer here is
always
NOT_ESTABLISHED and never a claim. Publishing a card does not make anyone find it: search engines,
agent directories and global reachability are out of this module's hands.

All institutions, keys and documents are synthetic. Nothing here contains customer data.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from joserfc.jwk import ECKey

from app.core.config import Settings
from app.domain.catalog import Channel, Product, Provider
from app.integrations.jose_tools import (
    canonical_json,
    public_jwks,
    sign_compact,
    sign_detached,
    sign_jwt,
)
from app.integrations.ports import ProviderDiscoveryPort
from app.services.bank_claims import approved_claims
from app.services.catalog_policy import product_is_current, provider_is_current
from app.services.delegation_service import DELEGABLE_SCOPES

A2A_PROTOCOL_VERSION = "1.0"
AGENT_CARD_VERSION = "1.0.0"
METADATA_VERSION = "1.0"
SUPPORTED_METADATA_VERSIONS = (METADATA_VERSION,)
ENTITY_STATEMENT_LIFETIME = timedelta(hours=1)
FINANCIAL_PROVIDER_METADATA = "trusteq_financial_provider"
REGULATORY_STATUS = {
    "status": "NOT_ESTABLISHED",
    "reason": "No authoritative register lookup is available to this project.",
    "note": "A valid signature or federation membership does not show a financial licence.",
}
SKILL_SCOPES = sorted(DELEGABLE_SCOPES)
_DELEGATED = {"schemes": {"delegatedAgent": {"list": SKILL_SCOPES}}}


def _skill(
    skill_id: str,
    name: str,
    description: str,
    tags: list[str],
    *,
    delegated: bool = False,
) -> dict[str, Any]:
    skill: dict[str, Any] = {
        "id": skill_id,
        "name": name,
        "description": description,
        "tags": tags,
        "inputModes": ["application/json"],
        "outputModes": ["application/json"],
    }
    if delegated:
        skill["securityRequirements"] = [_DELEGATED]
    return skill


def agent_card(settings: Settings, provider: Provider, key: ECKey) -> dict[str, Any]:
    """An A2A 1.0 AgentCard, signed over its canonical form (without `signatures`)."""
    base = settings.public_base_url
    card: dict[str, Any] = {
        "name": f"{provider.legal_name} - agent interface",
        "description": (
            "Synthetic demonstration provider. Agents may read approved product facts and prepare "
            "a depot application under a customer's delegation. The customer authenticates, "
            "confirms and signs on the bank's own channel. No real account is opened."
        ),
        "supportedInterfaces": [
            {
                "url": f"{base}/a2a/v1",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }
        ],
        "provider": {"url": base, "organization": provider.legal_name},
        "version": AGENT_CARD_VERSION,
        "documentationUrl": f"{base}/v1/agent/profile",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": "urn:trusteq:a2a:provider-trust:v1",
                    "description": "Where a verifier finds this entity's federation metadata.",
                    "required": False,
                    "params": {
                        "entityId": base,
                        "entityConfiguration": f"{base}/.well-known/openid-federation",
                        "metadataVersion": METADATA_VERSION,
                    },
                }
            ],
        },
        "securitySchemes": {
            "delegatedAgent": {
                "oauth2SecurityScheme": {
                    "description": (
                        "A customer's delegation, approved on the bank's page. Tokens are bound to "
                        "the agent's key with DPoP. A local protocol: not FAPI 2.0 conformant."
                    ),
                    "flows": {
                        "deviceCode": {
                            "deviceAuthorizationUrl": f"{base}/v1/delegation/par",
                            "tokenUrl": f"{base}/v1/delegation/token",
                            "scopes": {scope: f"Delegated scope {scope}" for scope in SKILL_SCOPES},
                        }
                    },
                    "oauth2MetadataUrl": f"{base}/.well-known/oauth-authorization-server",
                }
            }
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            _skill(
                "product-information",
                "Approved product information",
                "Answers from bank-approved facts, each with its source and version, or says the "
                "evidence is insufficient. Public. Not investment advice.",
                ["product", "fees", "facts"],
            ),
            _skill(
                "product-comparison",
                "Approved fee comparison",
                "Puts approved fee values side by side when the data supports it. Never ranks.",
                ["product", "comparison"],
            ),
            _skill(
                "document-retrieval",
                "Official documents",
                "Returns a document's identifier, version, digest and text, where available.",
                ["document", "disclosure"],
            ),
            _skill(
                "human-escalation",
                "Reach a person",
                "Says how the customer can ask for a person on the bank's channel. An agent "
                "cannot do this in the customer's name.",
                ["human", "escalation"],
            ),
            _skill(
                "depot-onboarding",
                "Prepare a depot application",
                "Prepares one application under a customer's delegation and reports its coarse "
                "state. It cannot confirm, sign, decide a check or open an account.",
                ["onboarding", "depot"],
                delegated=True,
            ),
        ],
    }
    return {
        **card,
        "signatures": [
            sign_detached(canonical_json(card), key, typ="agent-card+jws"),
        ],
    }


def entity_configuration(settings: Settings, provider: Provider, key: ECKey, now: datetime) -> str:
    """OpenID Federation 1.0 section 3: this entity's self-signed Entity Configuration."""
    base = settings.public_base_url
    claims: dict[str, Any] = {
        "iss": base,
        "sub": base,
        "iat": int(now.timestamp()),
        "exp": int((now + ENTITY_STATEMENT_LIFETIME).timestamp()),
        "jwks": public_jwks(key),
        "metadata": {
            "federation_entity": {
                "organization_name": provider.legal_name,
                "homepage_uri": base,
            },
            FINANCIAL_PROVIDER_METADATA: {
                "provider_id": provider.provider_id,
                "agent_card_uri": f"{base}/.well-known/agent-card.json",
                "profile_uri": f"{base}/v1/agent/profile",
                "catalog_uri": f"{base}/v1/agent/catalog",
                "synthetic": True,
            },
        },
    }
    if settings.trust_superior_entity:
        claims["authority_hints"] = [settings.trust_superior_entity]
    return sign_jwt(claims, key, typ="entity-statement+jwt")


def _document_refs(settings: Settings, product: Product) -> list[dict[str, str]]:
    base = settings.public_base_url
    refs = [
        {
            "document_id": product.risk_document_id,
            "document_version": product.risk_document_version,
            "document_digest": product.risk_document_digest,
            "kind": "DISCLOSURE",
        }
    ]
    refs += [
        {
            "document_id": source.document_id,
            "document_version": source.document_version,
            "document_digest": source.document_digest,
            "kind": "SOURCE",
        }
        for source in product.sources
    ]
    return [
        {
            **ref,
            "source_url": (
                f"{base}/v1/agent/documents/{ref['document_id']}?version={ref['document_version']}"
            ),
        }
        for ref in refs
    ]


def product_view(
    settings: Settings,
    catalog: ProviderDiscoveryPort,
    provider: Provider,
    product: Product,
    now: datetime,
) -> dict[str, Any]:
    current = product_is_current(product, now) and provider_is_current(provider, now)
    return {
        "product_id": product.product_id,
        "provider_id": provider.provider_id,
        "name": product.name,
        "product_version": product.catalog_version,
        "effective_from": product.effective_from.isoformat() if product.effective_from else None,
        "published_at": product.published_at.isoformat() if product.published_at else None,
        "valid_until": product.valid_until.isoformat() if product.valid_until else None,
        "status": "ACTIVE" if current else "DEPRECATED_OR_EXPIRED",
        "terms_digest": product.terms_digest,
        "documents": _document_refs(settings, product),
        "approved_facts": [
            claim.model_dump(mode="json")
            for claim in approved_claims(catalog, provider.provider_id, product.product_id)
        ]
        if current
        else [],
        "not_investment_advice": True,
    }


def channel_view(channel: Channel) -> dict[str, Any]:
    """The registered route and its partner. It says nothing about any commission."""
    return {
        "channel_id": channel.channel_id,
        "kind": channel.kind.value,
        "label": channel.label,
        "partner_id": channel.partner_id,
        "partner_name": channel.partner_name,
        "active": channel.active,
    }


def catalog_document(
    settings: Settings, catalog: ProviderDiscoveryPort, provider: Provider, now: datetime
) -> dict[str, Any]:
    return {
        "metadata_version": METADATA_VERSION,
        "generated_at": now.isoformat(),
        "provider_id": provider.provider_id,
        "manifest_version": provider.manifest_version,
        "manifest_digest": provider.manifest_digest,
        "products": [
            product_view(settings, catalog, provider, product, now)
            for product in catalog.list_products(provider.provider_id)
        ],
        "channels": [channel_view(c) for c in catalog.list_channels(provider.provider_id)],
        "synthetic": True,
    }


def provider_profile(
    settings: Settings,
    catalog: ProviderDiscoveryPort,
    provider: Provider,
    now: datetime,
    *,
    supported_versions: Sequence[str] = SUPPORTED_METADATA_VERSIONS,
) -> dict[str, Any]:
    base = settings.public_base_url
    return {
        "metadata_version": METADATA_VERSION,
        "supported_metadata_versions": list(supported_versions),
        "synthetic_demonstration_institution": True,
        "canonical_identity": {
            "provider_id": provider.provider_id,
            "legal_name": provider.legal_name,
            "role": provider.role.value,
            "registry_reference": provider.registry_reference,
            "registry_reference_status": "FICTIONAL_NOT_PROOF_OF_ANYTHING",
        },
        "official_domain": {
            "declared": provider.verified_domain,
            "association_status": "DECLARED_IN_SIGNED_METADATA_NOT_DNS_VERIFIED",
            "service_origin": base,
        },
        "regulatory_information": REGULATORY_STATUS,
        "products": [
            {
                "product_id": product.product_id,
                "name": product.name,
                "product_version": product.catalog_version,
                "status": "ACTIVE" if product_is_current(product, now) else "DEPRECATED_OR_EXPIRED",
                "valid_until": product.valid_until.isoformat() if product.valid_until else None,
            }
            for product in catalog.list_products(provider.provider_id)
        ],
        "authoritative_sources": {
            "product_facts": f"{base}/v1/agent/catalog",
            "documents": f"{base}/v1/agent/documents/{{document_id}}?version={{version}}",
            "statement": "Only signed metadata and approved facts from these URLs count.",
            "not_authoritative": ["Marketing text", "Search results", "Anything an agent writes"],
        },
        "agent_interaction": {
            "agent_card": f"{base}/.well-known/agent-card.json",
            "a2a_protocol_version": A2A_PROTOCOL_VERSION,
            "authentication": {
                "public_read": ["product-information", "product-comparison", "document-retrieval"],
                "delegation_required": ["depot-onboarding"],
                "authorization_server_metadata": f"{base}/.well-known/oauth-authorization-server",
                "delegation_requires": [
                    "a registered client key",
                    "the customer's approval on the bank's page",
                    "DPoP-bound requests",
                ],
            },
            "agent_may_not": [
                "impersonate the customer",
                "confirm or sign the contract",
                "decide KYC, compliance or suitability",
                "open the account",
            ],
        },
        "human_escalation": {
            "route": "The customer presses 'Speak to a person' on the bank's customer page.",
            "customer_page_prefix": f"{base}/customer/",
            "agent_can_request_on_behalf": False,
            "promise": "A request is recorded. No response time or staff assignment is promised.",
        },
        "contact": {"support_note": "Synthetic demonstration: no real contact point exists."},
        "supported_languages": ["en"],
        "provenance": {
            "manifest_version": provider.manifest_version,
            "manifest_digest": provider.manifest_digest,
            "issued_at": provider.issued_at.isoformat() if provider.issued_at else None,
            "expires_at": provider.expires_at.isoformat() if provider.expires_at else None,
            "last_updated": provider.issued_at.isoformat() if provider.issued_at else None,
            "revision": provider.manifest_version,
            "deprecation": None,
        },
        "not": [
            "SEO or marketing discoverability",
            "Proof of a financial licence",
            "Affiliation with any real institution",
        ],
    }


def signed_profile(profile: dict[str, Any], key: ECKey) -> str:
    return sign_compact(canonical_json(profile), key, typ="provider-profile+jws")
