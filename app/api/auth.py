"""Agent authentication for the agent-facing API: opaque, scoped bearer tokens (Phase 03).

Every protected agent route requires ``Authorization: Bearer <token>``. The token is verified
server-side (hash lookup, issuer, audience, issued/expiry, revocation of the token AND of its
grant) by `AuthorizationService.authenticate`; the route then demands one operation-specific
scope; the service checks resource binding (the token's application) and the workflow policy.

The Phase 02 ``X-Dev-Actor-Id`` header no longer exists: an agent cannot name itself.

CONTROLLED DEVELOPMENT AUTHORIZATION: tokens come from a trusted dev/test bootstrap and from
the bank on application creation. This is not production OAuth 2.0 / FAPI 2.0 (no
authorization server, no client authentication, no sender-constrained tokens) - see
docs/trust-boundaries.md for the production target.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import (
    AuthorizationServiceDep,
    CorrelationIdDep,
    DelegationServiceDep,
    SettingsDep,
)
from app.core.config import Settings
from app.domain.enums import MandateScope
from app.domain.mandate import AGENT_API_AUDIENCE
from app.services.authorization_service import AgentPrincipal, AuthorizationService
from app.services.delegation_service import DelegationService

_bearer = HTTPBearer(
    auto_error=False,  # missing/other schemes are answered by our own fail-closed 401
    scheme_name="AgentBearer",
    description=(
        "Opaque, short-lived, scoped agent access token (controlled DEV authorization, not "
        "production OAuth/FAPI). Obtain the first one from the trusted bootstrap "
        "(`python -m scripts.dev_auth bootstrap`); application-bound tokens are returned by "
        "POST /v1/applications."
    ),
)


def authenticate_request(
    request: Request,
    settings: Settings,
    authorization: AuthorizationService,
    delegation: DelegationService,
    correlation_id: str,
) -> AgentPrincipal:
    """Verify the token, then bind the request to the customer's delegation if it stands on one.

    A delegated token needs `Authorization: DPoP` and a proof of the delegation's key (RFC 9449),
    and
    never works as a plain bearer token. Other tokens stay plain bearer tokens.
    """
    scheme, _, raw = (request.headers.get("authorization") or "").partition(" ")
    scheme, raw = scheme.lower(), raw.strip()
    principal = authorization.authenticate(
        raw if scheme in {"bearer", "dpop"} and raw else None,
        audience=AGENT_API_AUDIENCE,
        correlation_id=correlation_id,
    )
    return delegation.check_request(
        principal,
        scheme=scheme,
        raw_token=raw,
        proof=request.headers.get("dpop"),
        method=request.method,
        url=f"{settings.public_base_url}{request.url.path}",
        correlation_id=correlation_id,
    )


def get_current_agent(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: SettingsDep,
    authorization: AuthorizationServiceDep,
    delegation: DelegationServiceDep,
    correlation_id: CorrelationIdDep,
) -> AgentPrincipal:
    return authenticate_request(request, settings, authorization, delegation, correlation_id)


CurrentAgent = Annotated[AgentPrincipal, Depends(get_current_agent)]


def require_scope(scope: MandateScope) -> Callable[..., AgentPrincipal]:
    """Dependency factory: a verified agent whose token carries `scope` (else 403, audited)."""

    def dependency(
        principal: CurrentAgent,
        authorization: AuthorizationServiceDep,
        correlation_id: CorrelationIdDep,
    ) -> AgentPrincipal:
        authorization.require_scope(principal, scope, correlation_id=correlation_id)
        return principal

    dependency.__name__ = f"require_scope_{scope.value}"
    return dependency


CanCreateDraft = Annotated[AgentPrincipal, Depends(require_scope(MandateScope.CREATE_DRAFT))]
CanReadStatus = Annotated[AgentPrincipal, Depends(require_scope(MandateScope.READ_STATUS))]
CanRequestHandoff = Annotated[AgentPrincipal, Depends(require_scope(MandateScope.REQUEST_HANDOFF))]
CanSubmit = Annotated[AgentPrincipal, Depends(require_scope(MandateScope.SUBMIT_AFTER_CONSENT))]
