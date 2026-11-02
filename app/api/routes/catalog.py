"""Public, read-only provider discovery (ADR section 05). No actor needed; no personal data."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import ContainerDep
from app.api.schemas.catalog import ProductListResponse, ProductView, ProviderManifestResponse
from app.api.schemas.errors import ErrorResponse

router = APIRouter(prefix="/v1", tags=["discovery"])

_ID = r"^[A-Za-z0-9_\-]+$"


@router.get(
    "/providers/{provider_id}/manifest",
    response_model=ProviderManifestResponse,
    summary="Official provider manifest (verified fixture)",
    description=(
        "Provider identity, registered channels and product ids from the locally verified "
        "fixture catalog. The digest is pinned in code and re-checked at load. This is "
        "synthetic MOCK data, not a BaFin or registry lookup."
    ),
    responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def provider_manifest(
    provider_id: Annotated[str, Path(min_length=1, max_length=64, pattern=_ID)],
    container: ContainerDep,
) -> ProviderManifestResponse:
    provider = container.catalog.get_provider(provider_id)
    return ProviderManifestResponse.build(
        provider,
        list(container.catalog.list_channels(provider_id)),
        list(container.catalog.list_products(provider_id)),
    )


@router.get(
    "/products",
    response_model=ProductListResponse,
    summary="Official, non-personalized product information",
    responses={404: {"model": ErrorResponse}},
)
def list_products(
    container: ContainerDep,
    provider_id: Annotated[
        str | None,
        Query(min_length=1, max_length=64, pattern=_ID, description="Limit to one provider."),
    ] = None,
) -> ProductListResponse:
    catalog = container.catalog
    providers = (
        [catalog.get_provider(provider_id)] if provider_id else list(catalog.list_providers())
    )
    return ProductListResponse(
        products=[
            ProductView.from_domain(product, list(catalog.list_channels(provider.provider_id)))
            for provider in providers
            for product in catalog.list_products(provider.provider_id)
        ]
    )
