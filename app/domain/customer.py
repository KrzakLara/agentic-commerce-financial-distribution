from typing import Literal

from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, ShortText
from app.domain.enums import TrustZone


class Customer(DomainModel):
    """The human customer (T0a). Holds an opaque reference only - no personal data here."""

    customer_id: OpaqueId
    created_at: AwareDatetime


class AgentActor(DomainModel):
    """An external AI agent identity (T0b) - separate from the customer it acts for.

    An agent identity is never treated as customer identity (ADR ADR-07).
    """

    agent_actor_id: OpaqueId
    operator_id: OpaqueId
    display_name: ShortText
    trust_zone: Literal[TrustZone.T0B_EXTERNAL_AGENT] = TrustZone.T0B_EXTERNAL_AGENT
    created_at: AwareDatetime
