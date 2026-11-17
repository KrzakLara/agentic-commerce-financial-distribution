import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from app.agents.factory import answer_model_client, build_answer_proposer
from app.container import AppContainer
from app.core.config import AIExecutionMode, Settings
from app.integrations.remote_providers import RemoteProofingProvider, RemoteSigningProvider
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.authorization_service import AuthorizationService
from app.services.communication_contract import proposal_json_schema
from app.services.contract_execution import ContractExecutionService
from app.services.customer_information_service import CustomerInformationService
from app.services.customer_session_service import CustomerSessionService
from app.services.delegation_service import DelegationService
from app.services.distribution_governance import DistributionGovernanceService
from app.services.identity_proofing import IdentityProofingService
from app.services.oidc_login import OidcLoginService
from app.services.onboarding_service import OnboardingService
from app.services.regulated_communication import CommunicationGuard
from app.services.review_service import ReviewService
from app.services.trust_gates import TrustGates
from app.services.verified_llm import QuestionBudget, VerifiedFactsGateway, VerifiedLlmAgent


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


def get_governance_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    applications: ApplicationServiceDep,
) -> DistributionGovernanceService:
    session_factory = request.app.state.session_factory
    return DistributionGovernanceService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        container.catalog,
        referral_jwks=settings.partner_referral_jwks,
        audience=settings.public_base_url,
        clock=container.clock,
    )


GovernanceServiceDep = Annotated[DistributionGovernanceService, Depends(get_governance_service)]


def get_onboarding_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    applications: ApplicationServiceDep,
    governance: GovernanceServiceDep,
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
        customer_information_required=settings.customer_information_required,
        trust=TrustGates(applications, governance, clock=container.clock)
        if settings.full_trust_profile
        else None,
    )


OnboardingServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]


def get_customer_information_service(
    request: Request,
    container: ContainerDep,
    applications: ApplicationServiceDep,
    onboarding: OnboardingServiceDep,
) -> CustomerInformationService:
    session_factory = request.app.state.session_factory
    return CustomerInformationService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        onboarding,
        clock=container.clock,
    )


CustomerInformationServiceDep = Annotated[
    CustomerInformationService, Depends(get_customer_information_service)
]


def get_review_service(
    request: Request,
    container: ContainerDep,
    applications: ApplicationServiceDep,
    onboarding: OnboardingServiceDep,
) -> ReviewService:
    session_factory = request.app.state.session_factory
    return ReviewService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        advance_checks=onboarding.advance_checks,
        clock=container.clock,
    )


ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]


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


def get_delegation_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    authorization: AuthorizationServiceDep,
) -> DelegationService:
    session_factory = request.app.state.session_factory
    return DelegationService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        authorization=authorization,
        catalog=container.catalog,
        public_base_url=settings.public_base_url,
        delegation_ttl_seconds=settings.delegation_ttl_seconds,
        request_ttl_seconds=settings.delegation_request_ttl_seconds,
        bootstrap_ttl_seconds=settings.agent_bootstrap_ttl_seconds,
        clock=container.clock,
    )


DelegationServiceDep = Annotated[DelegationService, Depends(get_delegation_service)]


def get_oidc_login_service(
    request: Request, settings: SettingsDep, container: ContainerDep
) -> OidcLoginService:
    session_factory = request.app.state.session_factory
    return OidcLoginService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        settings,
        container.outbound_http,
        clock=container.clock,
    )


OidcLoginServiceDep = Annotated[OidcLoginService, Depends(get_oidc_login_service)]


def get_proofing_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    applications: ApplicationServiceDep,
) -> IdentityProofingService:
    session_factory = request.app.state.session_factory
    provider = (
        RemoteProofingProvider(
            settings.proofing_provider_url,
            settings.proofing_provider_api_key,
            container.outbound_http,
            allow_insecure_local=settings.oidc_allow_insecure_local,
        )
        if settings.proofing_provider_url
        else None
    )
    return IdentityProofingService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        provider=provider,
        provider_id=settings.proofing_provider_id,
        provider_jwks=settings.proofing_provider_jwks,
        audience=settings.public_base_url,
        ttl_seconds=settings.proofing_ttl_seconds,
        clock=container.clock,
    )


ProofingServiceDep = Annotated[IdentityProofingService, Depends(get_proofing_service)]


def get_contract_service(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    applications: ApplicationServiceDep,
) -> ContractExecutionService:
    session_factory = request.app.state.session_factory
    provider = (
        RemoteSigningProvider(
            settings.signature_provider_url,
            settings.signature_provider_api_key,
            container.outbound_http,
            allow_insecure_local=settings.oidc_allow_insecure_local,
        )
        if settings.signature_provider_url
        else None
    )
    return ContractExecutionService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        applications,
        provider=provider,
        provider_id=settings.signature_provider_id,
        provider_jwks=settings.signature_provider_jwks,
        audience=settings.public_base_url,
        level=settings.signature_level,
        ttl_seconds=settings.signature_ttl_seconds,
        clock=container.clock,
        catalog=container.catalog,
    )


ContractServiceDep = Annotated[ContractExecutionService, Depends(get_contract_service)]


def get_communication_guard(container: ContainerDep) -> CommunicationGuard:
    return CommunicationGuard(container.catalog, container.clock)


CommunicationGuardDep = Annotated[CommunicationGuard, Depends(get_communication_guard)]


def get_verified_llm_agent(
    settings: SettingsDep, container: ContainerDep, guard: CommunicationGuardDep
) -> Callable[[], VerifiedLlmAgent]:
    """Builds the pipeline when a question arrives, so other A2A requests need no model client.

    The proposer gets the fact gateway and nothing else: that is all a model can reach.
    """

    def build() -> VerifiedLlmAgent:
        if container.answer_budget is None:
            container.answer_budget = QuestionBudget(settings.llm_max_questions_per_process)
        if (
            container.answer_model_client is None
            and settings.ai_execution_mode is AIExecutionMode.LLM
            and settings.llm_api_key is not None
        ):
            container.answer_model_client = answer_model_client(settings)
        proposer = build_answer_proposer(
            settings,
            VerifiedFactsGateway(guard),
            proposal_json_schema(),
            container.answer_model_client,
        )
        return VerifiedLlmAgent(guard, proposer, budget=container.answer_budget)

    return build


VerifiedLlmAgentDep = Annotated[Callable[[], VerifiedLlmAgent], Depends(get_verified_llm_agent)]


def questions_need_a_credential(settings: Settings) -> bool:
    """In LLM mode a question costs money, so an anonymous caller cannot spend it."""
    return (
        settings.ai_execution_mode is AIExecutionMode.LLM
        and settings.llm_questions_require_credential
    )


def get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or uuid.uuid4().hex


CorrelationIdDep = Annotated[str, Depends(get_correlation_id)]
