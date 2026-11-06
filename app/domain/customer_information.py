"""What the customer supplied on the bank's own pages before confirming (synthetic data only).

Three levels are kept apart on purpose: the values are CUSTOMER-PROVIDED, the bank only checks them
LOCALLY against a fixed format, and no EXTERNAL tax or identity verification exists.

The synthetic tax identifier is validated and then discarded: no column exists for it, so it cannot
reach the database, the audit trail or a backup. A production bank has to keep the real identifier
under encryption and retention rules, which this prototype does not model.
"""

from typing import Literal

from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId

EXTERNAL_VERIFICATION_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class TaxInformationRecord(DomainModel):
    """Stored only when local validation found every required field usable."""

    application_id: OpaqueId
    customer_id: OpaqueId
    source: str
    tax_residency_country: str
    us_person_declared: bool
    exemption_order_amount_eur: int | None = None
    church_tax_objection: bool | None = None
    field_statuses: dict[str, str]
    external_verification: Literal["NOT_IMPLEMENTED"] = EXTERNAL_VERIFICATION_NOT_IMPLEMENTED
    recorded_at: AwareDatetime


class AppropriatenessRecord(DomainModel):
    """The latest deterministic result. New answers replace it and clear the acknowledgement."""

    application_id: OpaqueId
    customer_id: OpaqueId
    product_level: str
    outcome: str
    reasons: tuple[str, ...] = ()
    knows_capital_loss: bool | None = None
    knows_no_guarantee: bool | None = None
    experience: str | None = None
    rule_set_version: str
    acknowledged_at: AwareDatetime | None = None
    recorded_at: AwareDatetime
