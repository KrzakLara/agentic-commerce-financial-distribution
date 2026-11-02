from pydantic import BaseModel, ConfigDict


class ErrorDetail(BaseModel):
    """One validation problem. Never echoes the offending input value."""

    loc: list[str | int]
    message: str
    type: str


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    """Consistent error envelope for every non-2xx response."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
