"""Structured logging foundation.

* JSON (or plain text) output on stdout via the standard library only.
* A ``correlation_id`` context variable is attached to every record.
* ``configure_logging`` is idempotent and only touches handlers it installed itself.
* Never log secrets, ID documents, raw AML details or personal data (ADR data handling).
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_HANDLER_MARKER = "_trusteq_handler"
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName", "color_message"}  # color_message: uvicorn ANSI noise
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", None) or correlation_id_var.get() or "-"
        base = f"{self.formatTime(record)} {record.levelname:<8} [{cid}] {record.name}: "
        text = base + record.getMessage()
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(JsonFormatter() if json_logs else _TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Route uvicorn through the same handler. Request logging (with correlation id)
    # is done by our own middleware, so uvicorn's access log is silenced.
    for name in ("uvicorn", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
