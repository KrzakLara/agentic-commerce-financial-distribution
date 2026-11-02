"""Correlation-id middleware (pure ASGI, so it is safe with streaming and exceptions)."""

import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import correlation_id_var, get_logger

CORRELATION_HEADER = "X-Correlation-ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
logger = get_logger("app.request")


def _resolve_correlation_id(value: str | None) -> str:
    # Client-supplied ids are only accepted in a safe, bounded format (log-injection guard).
    if value and _SAFE_ID.fullmatch(value):
        return value
    return uuid.uuid4().hex


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = _resolve_correlation_id(Headers(scope=scope).get(CORRELATION_HEADER))
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        token = correlation_id_var.set(correlation_id)
        status_code: int | None = None
        started = time.perf_counter()

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            if status_code is not None:
                logger.info(
                    "request completed",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status_code": status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            correlation_id_var.reset(token)
