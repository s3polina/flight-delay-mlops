import io
import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider

from flight_delay_mlops.logs import ContextFilter, JsonFormatter, bind_task_id
from flight_delay_mlops.telemetry import build_resource


@pytest.fixture
def json_logger():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter("flight-test"))
    logger = logging.getLogger("tests.json")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def lines() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    yield logger, lines
    logger.removeHandler(handler)


def test_record_has_common_fields_and_context(json_logger) -> None:
    logger, lines = json_logger
    tracer = TracerProvider().get_tracer("test")

    with tracer.start_as_current_span("work") as span, bind_task_id("task-1"):
        logger.info("task processed", extra={"attempt": 2})
    logger.info("outside")

    inside, outside = lines()
    context = span.get_span_context()
    assert inside["service"] == "flight-test"
    assert inside["level"] == "INFO"
    assert inside["message"] == "task processed"
    assert inside["task_id"] == "task-1"
    assert inside["trace_id"] == format(context.trace_id, "032x")
    assert inside["span_id"] == format(context.span_id, "016x")
    assert inside["attempt"] == 2
    assert set(inside) - {"attempt"} == set(outside)
    assert outside["task_id"] is None
    assert outside["trace_id"] is None


def test_secrets_are_masked(json_logger) -> None:
    logger, lines = json_logger

    logger.info(
        "connecting to %s",
        "amqp://guest:guest@rabbitmq:5672/",
        extra={"password": "postgres", "dsn": "postgresql://u:p@db:5432/x"},
    )

    (entry,) = lines()
    assert "guest" not in entry["message"]
    assert entry["message"] == "connecting to amqp://***:***@rabbitmq:5672/"
    assert entry["password"] == "***"
    assert entry["dsn"] == "postgresql://***:***@db:5432/x"


def test_exception_has_stack_trace(json_logger) -> None:
    logger, lines = json_logger

    try:
        raise ConnectionError("database is down")
    except ConnectionError:
        logger.exception("task processing failed")

    (entry,) = lines()
    assert entry["level"] == "ERROR"
    assert entry["error_type"] == "ConnectionError"
    assert "Traceback" in entry["exception"]


def test_resource_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
    monkeypatch.setenv("HOSTNAME", "worker-2")

    resource = build_resource("flight-worker")
    assert resource.attributes["service.name"] == "flight-worker"
    assert resource.attributes["service.instance.id"] == "worker-2"

    monkeypatch.setenv("OTEL_SERVICE_NAME", "flight-worker-b")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=w1")

    resource = build_resource("flight-worker")
    assert resource.attributes["service.name"] == "flight-worker-b"
    assert resource.attributes["service.instance.id"] == "w1"
