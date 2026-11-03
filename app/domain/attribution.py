from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, ShortText
from app.domain.catalog import ChannelKind


class ChannelAttribution(DomainModel):
    """The verified distribution attribution of an application (Phase 04, append-only).

    Recorded when the customer confirms the exact action, from the bank's channel registry -
    never from agent-supplied text. Settles which partner (if any) the onboarding is
    attributed to. No commission is calculated in the prototype.
    """

    application_id: OpaqueId
    provider_id: OpaqueId
    channel_id: OpaqueId
    channel_kind: ChannelKind
    partner_id: OpaqueId | None = None
    registry_version: ShortText
    consent_receipt_id: OpaqueId
    recorded_at: AwareDatetime
