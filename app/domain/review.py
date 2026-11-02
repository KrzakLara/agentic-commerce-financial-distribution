from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId
from app.domain.enums import ReviewCaseReason, ReviewCaseStatus


class ManualReviewCase(DomainModel):
    """A human-review case. It never overrides mandatory KYC or risk controls.

    Internal reasons stay here; the external agent only sees a generic action status.
    """

    case_id: OpaqueId
    application_id: OpaqueId
    reason: ReviewCaseReason
    status: ReviewCaseStatus = ReviewCaseStatus.OPEN
    created_at: AwareDatetime
