import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

task_id_var: ContextVar[str | None] = ContextVar("task_id", default=None)

_RECORD_FIELDS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",
    "taskName",
}
_SECRET_KEYS = ("password", "passwd", "secret", "token", "authorization", "api_key")
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s:/@]+:[^\s@]*@"
)

_THIRD_PARTY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "faststream",
    "faststream.access",
)


def redact(text: str) -> str:
    return _URL_CREDENTIALS.sub(r"\g<scheme>***:***@", text)


def _clean(key: str, value: Any) -> Any:
    if any(secret in key.lower() for secret in _SECRET_KEYS):
        return "***"
    if isinstance(value, str):
        return redact(value)
    return value


@contextmanager
def bind_task_id(task_id: object | None) -> Iterator[None]:
    token = task_id_var.set(str(task_id) if task_id else None)
    try:
        yield
    finally:
        task_id_var.reset(token)


class ContextFilter(logging.Filter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        if not getattr(record, "task_id", None):
            record.task_id = task_id_var.get()
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        else:
            record.trace_id = None
            record.span_id = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": getattr(record, "service", None),
            "logger": record.name,
            "message": redact(record.getMessage()),
            "task_id": getattr(record, "task_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "span_id": getattr(record, "span_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_FIELDS and key not in entry:
                entry[key] = _clean(key, value)
        if record.exc_info:
            entry["error_type"] = record.exc_info[0].__name__
            entry["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(default_service: str) -> None:
    service = os.getenv("OTEL_SERVICE_NAME") or default_service
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter(service))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
        third_party.setLevel(logging.NOTSET)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logging.captureWarnings(True)
