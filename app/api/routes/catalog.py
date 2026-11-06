"""Public, read-only provider discovery (ADR section 05). No actor needed; no personal data."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import ContainerDep
from app.api.schemas.catalog import (
    ApprovedClaimsResponse,
    ClaimVerificationRequest,
    ClaimVerificationResponse,
    ProductListResponse,
    ProductView,
    ProviderManifestResponse,
)
from app.api.schemas.errors import ErrorResponse
from app.container import AppContainer
from app.core.exceptions import CatalogIntegrityError, NotFoundError
from app.domain.catalog import Provider
from app.services.bank_claims import (
    SAFE_RESPONSE,
    Claim,
    ClaimDecision,
    ClaimVerdict,
    approved_claims,
    validate_claim,
)
from app.services.catalog_policy import provider_is_current

router = APIRouter(prefix="/v1", tags=["discovery"])

_ID = r"^[A-Za-z0-9_\-]+$"


def _current_provider(container: AppContainer, provider_id: str) -> Provider:
    """The verified provider - unavailable (503, fail closed) outside its validity window."""
    provider = container.catalog.get_provider(provider_id)
    if not provider_is_current(provider, container.clock()):
        raise CatalogIntegrityError("The provider's manifest is not currently valid.")
    return provider


@router.get(
    "/providers/{provider_id}/manifest",
    response_model=ProviderManifestResponse,
    summary="Official provider manifest (verified fixture)",
    description=(
        "Provider identity, verified domain, approved endpoint, capabilities, active channels "
        "and product ids from the locally verified fixture catalog. The manifest is checked "
        "against the bank's own provider register, its pinned digest and its signature, and "
        "must be inside its validity window. Synthetic MOCK data - not a BaFin or registry "
        "lookup."
    ),
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def provider_manifest(
    provider_id: Annotated[str, Path(min_length=1, max_length=64, pattern=_ID)],
    container: ContainerDep,
) -> ProviderManifestResponse:
    provider = _current_provider(container, provider_id)
    return ProviderManifestResponse.build(
        provider,
        list(container.catalog.list_channels(provider_id)),
        list(container.catalog.list_products(provider_id)),
    )


@router.get(
    "/providers/{provider_id}/products/{product_id}/approved-claims",
    response_model=ApprovedClaimsResponse,
    summary="Bank-approved facts about a product, each with its source and version",
    description=(
        "Structured facts that passed the bank's deterministic claim check against the verified "
        "catalog: fees, conditions, risk summary, provider identity and registered partners. A "
        "fact the catalog does not contain is absent. This states what the bank approved; it "
        "does not control what a customer's own agent writes. Not investment advice."
    ),
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def product_approved_claims(
    provider_id: Annotated[str, Path(min_length=1, max_length=64, pattern=_ID)],
    product_id: Annotated[str, Path(min_length=1, max_length=64, pattern=_ID)],
    container: ContainerDep,
) -> ApprovedClaimsResponse:
    _current_provider(container, provider_id)
    if not any(p.product_id == product_id for p in container.catalog.list_products(provider_id)):
        raise NotFoundError()
    return ApprovedClaimsResponse(
        claims=approved_claims(container.catalog, provider_id, product_id),
    )


@router.post(
    "/claims/verify",
    response_model=ClaimVerificationResponse,
    summary="Check whether a structured claim may be presented as official bank information",
    description=(
        "Deterministic: the value must equal the verified catalog's value and cite its source. "
        "Free text is never an official claim, and guarantees or personal recommendations go to "
        "a person. Read-only, and nothing is stored."
    ),
)
def verify_claim(
    body: ClaimVerificationRequest, container: ContainerDep
) -> ClaimVerificationResponse:
    claim = Claim(**body.model_dump())
    verdict = validate_claim(container.catalog, claim)
    if verdict.approved:
        provider = container.catalog.get_provider(claim.provider_id)
        if not provider_is_current(provider, container.clock()):
            verdict = ClaimVerdict(
                decision=ClaimDecision.ESCALATE_TO_HUMAN,
                reason="information_missing",
                safe_response=SAFE_RESPONSE,
            )
    return ClaimVerificationResponse(
        decision=verdict.decision, reason=verdict.reason, safe_response=verdict.safe_response
    )


@router.get(
    "/products",
    response_model=ProductListResponse,
    summary="Official, non-personalized product information",
    description=(
        "Versioned product information with fees, conditions, risk information, source "
        "documents and their digests, and the terms digest that a customer's approval binds "
        "to. Not investment advice."
    ),
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def list_products(
    container: ContainerDep,
    provider_id: Annotated[
        str | None,
        Query(min_length=1, max_length=64, pattern=_ID, description="Limit to one provider."),
    ] = None,
) -> ProductListResponse:
    catalog = container.catalog
    if provider_id:
        providers = [_current_provider(container, provider_id)]
    else:
        now = container.clock()
        providers = [p for p in catalog.list_providers() if provider_is_current(p, now)]
    return ProductListResponse(
        products=[
            ProductView.from_domain(product, list(catalog.list_channels(provider.provider_id)))
            for provider in providers
            for product in catalog.list_products(provider.provider_id)
        ]
    )
