"""What an external agent may learn about an application (least-privilege, ADR section 05).

The internal workflow has many fine-grained states. The agent only ever sees this coarse
vocabulary, so it cannot tell, for example, a KYC wait from a compliance wait or a manual
review from a customer action. No compliance rationale is derivable from it.
"""

from enum import StrEnum

from app.domain.enums import ApplicationStatus as S


class AgentVisibleState(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    IN_PROGRESS = "IN_PROGRESS"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    OPENED = "OPENED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


_MAPPING: dict[S, AgentVisibleState] = {
    S.DRAFT: AgentVisibleState.DRAFT,
    S.AWAITING_AUTHORIZATION: AgentVisibleState.AWAITING_CUSTOMER,
    S.AUTHORIZED: AgentVisibleState.IN_PROGRESS,
    S.KYC_PENDING: AgentVisibleState.IN_PROGRESS,
    S.COMPLIANCE_PENDING: AgentVisibleState.IN_PROGRESS,
    S.CONTRACT_PENDING: AgentVisibleState.IN_PROGRESS,
    S.SUBMITTED_TO_DEPOT_PROVIDER: AgentVisibleState.IN_PROGRESS,
    S.ACTION_REQUIRED: AgentVisibleState.ACTION_REQUIRED,
    S.MANUAL_REVIEW: AgentVisibleState.ACTION_REQUIRED,
    S.OPENED: AgentVisibleState.OPENED,
    S.REJECTED: AgentVisibleState.DECLINED,
    S.CANCELLED: AgentVisibleState.CANCELLED,
    S.FAILED: AgentVisibleState.FAILED,
}


def to_agent_visible_state(status: S) -> AgentVisibleState:
    return _MAPPING[status]
