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

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import AuthorizationServiceDep, CorrelationIdDep
from app.domain.enums import MandateScope
from app.domain.mandate import AGENT_API_AUDIENCE
from app.services.authorization_service import AgentPrincipal

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


def get_current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
) -> AgentPrincipal:
    raw = credentials.credentials if credentials is not None else None
    return authorization.authenticate(
        raw, audience=AGENT_API_AUDIENCE, correlation_id=correlation_id
    )


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
