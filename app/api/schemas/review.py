"""Schemas for the customer's request to speak with a person (Phase 04)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import HumanRequestCategory


class HumanRequestBody(BaseModel):
    """A fixed category only: no free text, so no personal or sensitive data is collected."""

    model_config = ConfigDict(extra="forbid")

    category: HumanRequestCategory = Field(
        description="Why the customer would like to speak with a person (fixed list).",
        examples=["QUESTION_ABOUT_PRODUCT"],
    )


class HumanRequestResponse(BaseModel):
    """The same generic acknowledgement in every case: it never says whether a review case
    already existed, why, or what the bank's checks found."""

    model_config = ConfigDict(extra="forbid")

    application_id: str
    status: Literal["RECEIVED"] = "RECEIVED"
    message: str
