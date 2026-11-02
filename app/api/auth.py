"""DEV/TEST actor identification - NOT authentication.

Real scoped, short-lived mandates (audience, expiry, jti, revocation) are Phase 03. Until
then the caller names its actor in the ``X-Dev-Actor-Id`` header so that ownership checks,
idempotency scoping and audit attribution can be built and tested.

This is safe only because:
* it is switched on by `Settings.dev_actor_auth_enabled` and is *never* active in
  production (`Settings.dev_actor_auth_active`); in production every protected route
  answers 401, i.e. the system fails closed;
* it only ever yields an *agent* identity. It cannot represent a customer, an operator or a
  depot provider, so it can never authorise a state transition.
"""

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import APIKeyHeader

from app.api.deps import SettingsDep
from app.core.exceptions import AuthenticationRequiredError

DEV_ACTOR_HEADER = "X-Dev-Actor-Id"
_ACTOR_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_dev_actor_header = APIKeyHeader(
    name=DEV_ACTOR_HEADER,
    scheme_name="DevActorHeader",
    description=(
        "DEV/TEST ONLY. Names the calling agent actor. This is not authentication and is "
        "disabled in production."
    ),
    auto_error=False,
)


@dataclass(frozen=True)
class AgentActorContext:
    agent_actor_id: str
    is_dev_actor: bool = True  # always True until Phase 03 introduces real mandates


def get_current_agent_actor(
    settings: SettingsDep, header_value: Annotated[str | None, Depends(_dev_actor_header)]
) -> AgentActorContext:
    if not settings.dev_actor_auth_active:
        raise AuthenticationRequiredError()
    if header_value is None or not _ACTOR_ID.fullmatch(header_value):
        raise AuthenticationRequiredError()
    return AgentActorContext(agent_actor_id=header_value)


CurrentAgentActor = Annotated[AgentActorContext, Depends(get_current_agent_actor)]
