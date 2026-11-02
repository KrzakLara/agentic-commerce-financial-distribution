from typing import Any

from pydantic import AwareDatetime

from app.domain.base import CorrelationId, DomainModel, OpaqueId, ShortText
from app.domain.enums import AuditActorType, AuditOutcome


class AuditEvent(DomainModel):
    """One auditable fact. `details` must never contain secrets, ID documents or AML detail."""

    event_id: OpaqueId
    occurred_at: AwareDatetime
    correlation_id: CorrelationId
    event_type: ShortText
    actor_type: AuditActorType
    actor_id: OpaqueId | None = None
    application_id: OpaqueId | None = None
    outcome: AuditOutcome
    details: dict[str, Any] = {}
