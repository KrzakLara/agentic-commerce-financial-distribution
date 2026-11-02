"""Trusted internal callback of the MOCK depot provider (T3 -> T2).

This is NOT an administration endpoint and cannot set arbitrary states. It accepts exactly one
kind of message - "the provider opened the account for application X" - and only when:

1. the endpoint is enabled (a callback secret is configured; refused in production),
2. the message carries a valid, fresh HMAC signature over the raw body,
3. the confirmation matches the recorded submission (application id AND provider reference),
4. the application is awaiting exactly this confirmation (SUBMITTED_TO_DEPOT_PROVIDER),
5. the confirmation id has not been used before (replay-safe, duplicates are no-ops).

It is hidden from the public OpenAPI contract. A real provider would use mutual TLS / signed
messages and an allow-listed network path; the shared HMAC secret here is a MOCK credential.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from app.api.deps import ContainerDep, CorrelationIdDep, OnboardingServiceDep, SettingsDep
from app.core.exceptions import AppError, AuthenticationRequiredError, ServiceDisabledError
from app.integrations.provider_callback import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from app.services.onboarding_service import ProviderConfirmation

router = APIRouter(tags=["internal"])
logger = logging.getLogger("app.internal")
_MAX_BODY_BYTES = 4096


class _MalformedConfirmationError(AppError):
    status_code = 422
    code = "validation_error"

    @classmethod
    def default_message(cls) -> str:
        return "The confirmation is malformed."


@router.post("/internal/depot-provider/confirmations", include_in_schema=False)
async def depot_provider_confirmation(
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> dict[str, str]:
    secret = settings.mock_provider_callback_secret
    if secret is None:
        raise ServiceDisabledError()  # fail closed: no secret configured, no callback
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise _MalformedConfirmationError()
    if not verify(
        secret.get_secret_value().encode("utf-8"),
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
        body=body,
        now=container.clock().timestamp(),
    ):
        logger.warning("depot-provider callback rejected: bad or stale signature")
        raise AuthenticationRequiredError()
    try:
        confirmation = ProviderConfirmation.model_validate_json(body)
    except ValidationError:
        raise _MalformedConfirmationError() from None
    result = await run_in_threadpool(
        service.handle_provider_confirmation, confirmation, correlation_id
    )
    return {"result": "duplicate_ignored" if result.duplicate else "accepted"}
