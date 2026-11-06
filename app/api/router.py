"""Central router registration. Add new route modules here."""

from fastapi import APIRouter

from app.api.routes import (
    agent_surface,
    applications,
    catalog,
    customer,
    health,
    internal,
    trust_customer,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(applications.router)
api_router.include_router(catalog.router)
api_router.include_router(customer.router)
api_router.include_router(internal.router)
api_router.include_router(agent_surface.router)
api_router.include_router(trust_customer.router)
