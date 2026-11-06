"""Customer JSON API for tax information and the knowledge-and-experience questions.

Field errors of these bodies report the location and the type of a problem, never the input
(`app.api.error_handlers`). Unknown fields are refused, so a caller cannot state its own source.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.api.schemas.handoffs import Nonce
from app.domain.customer_information import EXTERNAL_VERIFICATION_NOT_IMPLEMENTED
from app.services.appropriateness import ExperienceBand


class TaxInformationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: Nonce
    tax_residency_country: str | None = None
    synthetic_tax_id: str | None = None
    us_person_declared: bool | None = None
    exemption_order_amount_eur: int | None = None
    church_tax_objection: bool | None = None


class TaxInformationResponse(BaseModel):
    """Field statuses only: no value is ever echoed."""

    model_config = ConfigDict(extra="forbid")

    application_id: str
    complete: bool
    field_statuses: dict[str, str]
    data_source: str
    local_validation: Literal["COMPLETE", "INCOMPLETE"]
    external_verification: Literal["NOT_IMPLEMENTED"] = EXTERNAL_VERIFICATION_NOT_IMPLEMENTED
    ready_for_confirmation: bool


class AssessmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: Nonce
    intent: Literal["answer", "acknowledge"] = "answer"
    knows_capital_loss: bool | None = None
    knows_no_guarantee: bool | None = None
    experience: ExperienceBand | None = None


class AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    outcome: str
    customer_notice: str | None
    acknowledged: bool
    rule_set_version: str
    ready_for_confirmation: bool
    notice: str
