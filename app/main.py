"""FastAPI application factory and ASGI entry point (`app.main:app`)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.error_handlers import register_error_handlers
from app.api.middleware import CORRELATION_HEADER, CorrelationIdMiddleware
from app.api.router import api_router
from app.container import AppContainer, build_default_container
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.repositories.database import create_db_engine, create_session_factory, init_database

DESCRIPTION = """
**Secure Agentic Depot Onboarding** - TRUSTEQ case study (ADR-001 v1.1, L3 controlled autonomy).

An external AI agent may *propose* actions; deterministic bank-side policy decides, the
customer authorizes through a bank-controlled channel, and only a trusted depot provider
confirmation can open a depot.

**Phase 03 - scoped agent authorization.** Agent routes require `Authorization: Bearer
<token>`: opaque, short-lived, server-side tokens (stored as hashes) with issuer, audience,
expiry, `jti`, revocation, resource binding and one scope per operation. The first, narrow
token comes from a trusted DEV bootstrap (no HTTP endpoint); `POST /v1/applications` returns
the application-bound tokens. This is a CONTROLLED DEVELOPMENT authorization implementation,
not production OAuth 2.0 / FAPI 2.0. The customer confirms separately on the bank-controlled
page (DEV-simulated sign-in with a server-side session and CSRF protection). Only a signed MOCK
depot-provider confirmation can set `OPENED`; it revokes the onboarding credentials. KYC,
compliance, contract signing and the depot provider are MOCK, never real financial operations.
"""

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness / readiness information."},
    {"name": "discovery", "description": "Verified (fixture) provider and product metadata."},
    {"name": "applications", "description": "Agent-facing draft, handoff, submit and status."},
    {
        "name": "customer",
        "description": "Bank-controlled customer confirmation (DEV-simulated sign-in).",
    },
]


def create_app(settings: Settings | None = None, container: AppContainer | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    logger = get_logger("app.main")

    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        init_database(engine)
        if settings.dev_agent_bootstrap_active:
            logger.warning(
                "DEV agent bootstrap issuance is enabled (trusted CLI only, no HTTP endpoint) - "
                "controlled development authorization, not production OAuth/FAPI"
            )
        if settings.dev_customer_auth_active:
            logger.warning("DEV simulated customer sign-in is ACTIVE - not identity verification")
        logger.info(
            "application startup",
            extra={
                "environment": settings.environment.value,
                "ai_execution_mode": settings.ai_execution_mode.value,
            },
        )
        yield
        engine.dispose()
        logger.info("application shutdown")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    # Fails fast (CatalogIntegrityError) if the verified catalog fixture was tampered with.
    app.state.container = container or build_default_container(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", CORRELATION_HEADER],
            expose_headers=[CORRELATION_HEADER],
        )
    # Added last so it is outermost: every response (even CORS preflight) gets an id.
    app.add_middleware(CorrelationIdMiddleware)

    register_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
