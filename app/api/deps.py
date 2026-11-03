import uuid
from typing import Annotated

from fastapi import Depends, Request

from app.container import AppContainer
from app.core.config import Settings
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.authorization_service import AuthorizationService
from app.services.customer_session_service import CustomerSessionService
from app.services.onboarding_service import OnboardingService


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


ContainerDep = Annotated[AppContainer, Depends(get_container)]


def get_authorization_service(
    request: Request, settings: SettingsDep, container: ContainerDep
) -> AuthorizationService:
    session_factory = request.app.state.session_factory
    return AuthorizationService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        clock=container.clock,
        # The HTTP layer can only VERIFY tokens. Issuing a bootstrap grant (or re-issuing
        # tokens) is a trusted operator action outside the API, whatever the configuration.
        bootstrap_enabled=False,
        bootstrap_ttl_seconds=settings.agent_bootstrap_ttl_seconds,
        status_ttl_seconds=settings.agent_status_ttl_seconds,
    )


AuthorizationServiceDep = Annotated[AuthorizationService, Depends(get_authorization_service)]


def get_application_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    authorization: AuthorizationServiceDep,
) -> ApplicationService:
    session_factory = request.app.state.session_factory
    return ApplicationService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        clock=container.clock,
        catalog=container.catalog,
        mandate_ttl_seconds=settings.mandate_ttl_seconds,
        authorization=authorization,
    )


ApplicationServiceDep = Annotated[ApplicationService, Depends(get_application_service)]


def get_onboarding_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    applications: ApplicationServiceDep,
) -> OnboardingService:
    session_factory = request.app.state.session_factory
    return OnboardingService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        catalog=container.catalog,
        kyc=container.kyc,
        compliance=container.compliance,
        contract=container.contract,
        depot=container.depot,
        clock=container.clock,
        handoff_ttl_seconds=settings.handoff_ttl_seconds,
    )


OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]


def get_customer_session_service(
    request: Request, settings: SettingsDep, container: ContainerDep
) -> CustomerSessionService:
    session_factory = request.app.state.session_factory
    return CustomerSessionService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        clock=container.clock,
        ttl_seconds=settings.customer_session_ttl_seconds,
    )


CustomerSessionServiceDep = Annotated[CustomerSessionService, Depends(get_customer_session_service)]


def get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or uuid.uuid4().hex


CorrelationIdDep = Annotated[str, Depends(get_correlation_id)]
