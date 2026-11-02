"""Internal domain contracts (trust zone T2 core).

Initial typed contracts only: no persistence, no workflow logic. Phase 01 adds the state
machine and storage. These models are never returned from the API directly; the API layer
defines its own schemas in `app.api.schemas`.
"""

from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.catalog import Channel, Product, Provider
from app.domain.checks import CheckRecord, DepotSubmissionRecord
from app.domain.customer import AgentActor, Customer
from app.domain.enums import (
    ApplicationStatus,
    AuditActorType,
    AuditOutcome,
    CheckOutcome,
    CheckType,
    MandateScope,
    ProviderRole,
    ReviewCaseReason,
    ReviewCaseStatus,
    TrustZone,
)
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.idempotency import IdempotencyRecord
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt
from app.domain.review import ManualReviewCase

__all__ = [
    "AgentActor",
    "ApprovedAction",
    "Channel",
    "CheckOutcome",
    "CheckRecord",
    "CheckType",
    "DepotSubmissionRecord",
    "ApplicationStatus",
    "AuditActorType",
    "AuditEvent",
    "AuditOutcome",
    "ConsentReceipt",
    "Customer",
    "DepotApplication",
    "DisclosureReceipt",
    "Handoff",
    "IdempotencyRecord",
    "ManualReviewCase",
    "Mandate",
    "MandateScope",
    "Product",
    "Provider",
    "ProviderRole",
    "ReviewCaseReason",
    "ReviewCaseStatus",
    "TrustZone",
]
