from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, OpaqueId
from app.domain.enums import (
    ApplicationStatus,
    AuditActorType,
    CheckType,
    HumanRequestCategory,
    ReviewCaseReason,
    ReviewCaseStatus,
    ReviewDecision,
    ReviewEvidenceCategory,
)


class ReviewEvidence(DomainModel):
    """One piece of permitted review evidence: a category and a SYNTHETIC reference.

    No document content, no personal data - the reference points into a (mock) evidence store.
    """

    category: ReviewEvidenceCategory
    reference: OpaqueId
    recorded_by: OpaqueId
    recorded_at: AwareDatetime


class ManualReviewCase(DomainModel):
    """A human-review case (Phase 04). It never overrides mandatory KYC or risk controls.

    * `interrupted_step` is the step the application was parked at (its `resume_status`);
      None if the case did not pause the workflow (e.g. a request while the depot provider
      already decides).
    * Internal reasons, evidence and decisions stay here and in the audit log; the external
      agent only ever sees the generic state ACTION_REQUIRED.
    * At most one active (not CLOSED) case per application, enforced by the database.
    """

    case_id: OpaqueId
    application_id: OpaqueId
    reason: ReviewCaseReason
    status: ReviewCaseStatus = ReviewCaseStatus.OPEN
    created_at: AwareDatetime
    check_type: CheckType | None = None
    check_attempt: int | None = Field(default=None, ge=1)  # the attempt that was not PASS
    request_category: HumanRequestCategory | None = None
    interrupted_step: ApplicationStatus | None = None
    opened_by: AuditActorType = AuditActorType.SYSTEM
    requested_information: ReviewEvidenceCategory | None = None
    evidence: tuple[ReviewEvidence, ...] = ()
    resolution: ReviewDecision | None = None
    resolved_by: OpaqueId | None = None
    resolved_at: AwareDatetime | None = None
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status is ReviewCaseStatus.CLOSED) != (self.resolution is not None):
            raise ValueError("a case is CLOSED exactly when it has a resolution")
        if (self.reason is ReviewCaseReason.CUSTOMER_REQUESTED_HUMAN) != (
            self.request_category is not None
        ):
            raise ValueError("only customer requests carry a request category")
        if self.check_attempt is not None and self.check_type is None:
            raise ValueError("a check attempt needs its check type")
        return self

    @property
    def active(self) -> bool:
        return self.status is not ReviewCaseStatus.CLOSED
