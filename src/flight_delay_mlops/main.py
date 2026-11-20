import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from time import perf_counter
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import asyncpg
from aio_pika.exceptions import AMQPError
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from faststream.rabbit import RabbitBroker
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.trace import Status, StatusCode
from starlette.routing import Match

from flight_delay_mlops.config import settings
from flight_delay_mlops.database import check_database
from flight_delay_mlops.logs import bind_task_id, setup_logging
from flight_delay_mlops.messaging import declare_topology, make_broker, publish_task
from flight_delay_mlops.schemas import Flight, TaskAccepted, TaskMessage, TaskStatus
from flight_delay_mlops.storage import TaskStore
from flight_delay_mlops.telemetry import (
    API_DURATION_BUCKETS,
    meter,
    setup_telemetry,
    shutdown_telemetry,
)

SERVICE_NAME = "flight-api"
HEALTH_PATHS = {"/healthz", "/api/v1/health"}

setup_logging(SERVICE_NAME)
setup_telemetry(SERVICE_NAME)
logger = logging.getLogger("flight_delay_mlops.api")

http_requests = meter.create_counter(
    "api.requests",
    unit="{request}",
    description="HTTP requests by route, method and status code",
)
http_duration = meter.create_histogram(
    "api.request.duration",
    unit="s",
    description="HTTP request duration",
    explicit_bucket_boundaries_advisory=API_DURATION_BUCKETS,
)
overloaded_responses = meter.create_counter(
    "api.overloaded.responses",
    unit="{response}",
    description="503 responses because the task queue is full or unavailable",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    broker: RabbitBroker = make_broker()
    await broker.connect()
    await declare_topology(broker)
    app.state.broker = broker
    app.state.store = await TaskStore.connect(settings.database_url)
    logger.info("api started", extra={"queue": settings.tasks_queue})
    yield
    logger.info("graceful shutdown started")
    await app.state.store.close()
    await broker.stop()
    logger.info("graceful shutdown finished")
    shutdown_telemetry()


app = FastAPI(
    title="Flight Delay MLOps",
    lifespan=lifespan,
)
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="healthz,api/v1/health",
    meter_provider=NoOpMeterProvider(),
)


def route_template(request: Request) -> str:
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is Match.FULL:
            return getattr(route, "path", "unmatched")
    return "unmatched"


@app.middleware("http")
async def observe_requests(request: Request, call_next) -> Response:
    start_time = perf_counter()
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration = perf_counter() - start_time
        route = route_template(request)
        attributes = {
            "http.request.method": request.method,
            "http.route": route,
            "http.response.status_code": status_code,
        }
        http_requests.add(1, attributes)
        http_duration.record(duration, attributes)
        logger.log(
            logging.DEBUG if request.url.path in HEALTH_PATHS else logging.INFO,
            "http request",
            extra={
                "method": request.method,
                "route": route,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/version")
async def get_version() -> dict[str, str]:
    return {
        "version": version("flight-delay-mlops"),
    }


@app.get("/api/v1/health", response_model=None)
async def health() -> dict[str, str | float] | JSONResponse:
    start_time = perf_counter()

    try:
        database_version = await check_database()
        response_time = (perf_counter() - start_time) * 1000

        return {
            "status": "ok",
            "database": database_version,
            "response_time_ms": round(response_time, 2),
        }
    except (asyncpg.PostgresError, OSError):
        response_time = (perf_counter() - start_time) * 1000
        logger.warning("database health check failed", exc_info=True)

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unavailable",
                "response_time_ms": round(response_time, 2),
            },
        )


def overloaded_response(reason: str) -> JSONResponse:
    overloaded_responses.add(1, {"reason": reason})
    span = trace.get_current_span()
    span.add_event("task.rejected", {"reason": reason})
    span.set_status(Status(StatusCode.ERROR, f"503: {reason}"))
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "Service is overloaded: task queue is full. Retry later.",
        },
        headers={"Retry-After": str(settings.retry_after_sec)},
    )


@app.post(
    "/api/v1/tasks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskAccepted,
    responses={503: {"description": "Очередь заполнена, повторите после Retry-After"}},
)
async def create_task(
    flight: Flight,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskAccepted | JSONResponse:
    task_id = (
        uuid5(NAMESPACE_URL, f"api:{idempotency_key}") if idempotency_key else uuid4()
    )
    created_at = datetime.now(UTC)
    task = TaskMessage(
        task_id=task_id, source="api", created_at=created_at, flight=flight
    )

    broker: RabbitBroker = request.app.state.broker
    store: TaskStore = request.app.state.store

    with bind_task_id(task_id):
        span = trace.get_current_span()
        span.set_attribute("task.id", str(task_id))
        try:
            accepted, task_status = await store.create_pending(
                task_id,
                flight.model_dump(mode="json"),
                created_at,
                publish=lambda: publish_task(broker, task, timeout=2.0),
            )
        except (asyncpg.PostgresError, AMQPError, OSError, TimeoutError) as exc:
            span.record_exception(exc)
            logger.exception(
                "failed to enqueue task, responding 503",
                extra={"status_code": 503, "retry_after_sec": settings.retry_after_sec},
            )
            return overloaded_response("enqueue_error")

        if not accepted:
            logger.warning(
                "task queue is full, responding 503",
                extra={"status_code": 503, "retry_after_sec": settings.retry_after_sec},
            )
            return overloaded_response("queue_full")

        logger.info("task accepted", extra={"task_status": task_status})
        return TaskAccepted(task_id=task_id, status=task_status)


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: UUID, request: Request) -> TaskStatus:
    store: TaskStore = request.app.state.store
    with bind_task_id(task_id):
        row = await store.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatus(**row)
