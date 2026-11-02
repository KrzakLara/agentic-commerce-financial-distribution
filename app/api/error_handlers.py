"""Maps exceptions to the consistent `ErrorResponse` JSON envelope."""

from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.middleware import CORRELATION_HEADER
from app.api.schemas.errors import ErrorBody, ErrorDetail, ErrorResponse
from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger("app.errors")


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _json_error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
) -> JSONResponse:
    correlation_id = _correlation_id(request)
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, correlation_id=correlation_id, details=details)
    )
    headers = {CORRELATION_HEADER: correlation_id} if correlation_id else None
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(AppError, exc)
    return _json_error(request, err.status_code, err.code, err.message)


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(StarletteHTTPException, exc)
    return _json_error(request, err.status_code, f"http_{err.status_code}", str(err.detail))


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    errors = cast(RequestValidationError, exc).errors()
    details = [
        ErrorDetail(loc=list(err["loc"]), message=err["msg"], type=err["type"]) for err in errors
    ]
    return _json_error(request, 422, "validation_error", "Request validation failed.", details)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled exception",
        exc_info=exc,
        extra={"correlation_id": _correlation_id(request), "path": request.url.path},
    )
    # Fail closed with a generic message: no stack trace or internals reach the caller.
    return _json_error(request, 500, "internal_error", "An internal error occurred.")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
