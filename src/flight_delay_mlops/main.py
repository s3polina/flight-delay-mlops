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

from flight_delay_mlops.config import settings
from flight_delay_mlops.database import check_database
from flight_delay_mlops.messaging import declare_topology, publish_task
from flight_delay_mlops.schemas import Flight, TaskAccepted, TaskMessage, TaskStatus
from flight_delay_mlops.storage import TaskStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("flight_delay_mlops")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    broker = RabbitBroker(settings.rabbit_url)
    await broker.connect()
    await declare_topology(broker)
    app.state.broker = broker
    app.state.store = await TaskStore.connect(settings.database_url)
    yield
    await app.state.store.close()
    await broker.stop()


app = FastAPI(
    title="Flight Delay MLOps",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    start_time = perf_counter()

    response = await call_next(request)

    response_time = (perf_counter() - start_time) * 1000

    logger.info(
        "request method=%s path=%s status_code=%s response_time_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        response_time,
    )

    return response


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

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unavailable",
                "response_time_ms": round(response_time, 2),
            },
        )


def overloaded_response() -> JSONResponse:
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

    try:
        accepted, task_status = await store.create_pending(
            task_id,
            flight.model_dump(mode="json"),
            created_at,
            publish=lambda: publish_task(broker, task, timeout=2.0),
        )
    except (asyncpg.PostgresError, AMQPError, OSError, TimeoutError):
        logger.exception("failed to enqueue task")
        return overloaded_response()

    if not accepted:
        logger.warning("queue is full, task rejected (load shedding)")
        return overloaded_response()

    return TaskAccepted(task_id=task_id, status=task_status)


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: UUID, request: Request) -> TaskStatus:
    store: TaskStore = request.app.state.store
    row = await store.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatus(**row)
