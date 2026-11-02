from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, ShortText
from app.domain.enums import CheckOutcome, CheckType


class CheckRecord(DomainModel):
    """Evidence that a mandatory check ran. Internal: never returned to the agent.

    Phase 02 stores only the outcome and a MOCK reference - no personal or compliance detail.
    """

    application_id: OpaqueId
    check_type: CheckType
    outcome: CheckOutcome
    reference: ShortText
    mock: bool
    created_at: AwareDatetime


class DepotSubmissionRecord(DomainModel):
    """Bookkeeping for the hand-over to the depot provider and its trusted confirmation."""

    application_id: OpaqueId
    provider_ref: OpaqueId
    submitted_at: AwareDatetime
    confirmation_id: OpaqueId | None = None
    confirmed_at: AwareDatetime | None = None
