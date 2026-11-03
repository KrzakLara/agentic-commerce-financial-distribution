"""Public, read-only provider discovery (ADR section 05). No actor needed; no personal data."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import ContainerDep
from app.api.schemas.catalog import ProductListResponse, ProductView, ProviderManifestResponse
from app.api.schemas.errors import ErrorResponse
from app.container import AppContainer
from app.core.exceptions import CatalogIntegrityError
from app.domain.catalog import Provider
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
