"""Central router registration. Add new route modules here."""

from fastapi import APIRouter

from app.api.routes import applications, catalog, customer, health, internal

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(applications.router)
api_router.include_router(catalog.router)
api_router.include_router(customer.router)
api_router.include_router(internal.router)
