import asyncio
import contextvars
import json
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from faststream import AckPolicy
from faststream.asgi import AsgiFastStream, AsgiResponse, get
from faststream.exceptions import RejectMessage
from faststream.rabbit import Channel
from faststream.rabbit.annotations import RabbitMessage
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError

from flight_delay_mlops.config import settings
from flight_delay_mlops.logs import setup_logging
from flight_delay_mlops.messaging import declare_topology, make_broker, tasks_queue
from flight_delay_mlops.metrics import MetricsWriter, rss_mb
from flight_delay_mlops.model import DELAY_THRESHOLD, LoadedModel, load_model
from flight_delay_mlops.schemas import TaskMessage
from flight_delay_mlops.storage import TaskResult, TaskStore
from flight_delay_mlops.telemetry import (
    inference_buckets,
    meter,
    setup_telemetry,
    shutdown_telemetry,
    tracer,
)

SERVICE_NAME = "flight-worker"

setup_logging(SERVICE_NAME)
setup_telemetry(SERVICE_NAME)
logger = logging.getLogger("flight_delay_mlops.worker")

WORKER_ID = os.getenv("HOSTNAME", socket.gethostname())

tasks_processed = meter.create_counter(
    "worker.tasks.processed",
    unit="{task}",
    description="Processed task deliveries by result: ok, duplicate, error, dlq",
)
inference_duration = meter.create_histogram(
    "worker.inference.duration",
    unit="s",
    description="Model inference duration",
    explicit_bucket_boundaries_advisory=inference_buckets(settings.inference_delay_sec),
)
tasks_in_flight = meter.create_up_down_counter(
    "worker.tasks.in_flight",
    unit="{task}",
    description="Tasks currently being processed",
)
redeliveries = meter.create_counter(
    "worker.redeliveries",
    unit="{delivery}",
    description="Repeated deliveries of the same task",
)


@dataclass
class WorkerState:
    model: LoadedModel | None = None
    store: TaskStore | None = None
    executor: ThreadPoolExecutor | None = None
    metrics: MetricsWriter | None = None
    in_flight: int = 0
    processed: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed: int = 0


state = WorkerState()

broker = make_broker(
    graceful_timeout=settings.graceful_timeout_sec,
    default_channel=Channel(prefetch_count=settings.prefetch_count or None),
)


def run_inference(model: LoadedModel, flight: dict[str, Any]) -> float:
    with tracer.start_as_current_span(
        "model.inference",
        attributes={
            "model.name": model.name,
            "model.alias": model.alias,
            "model.version": model.version,
        },
    ):
        started = time.perf_counter()
        time.sleep(settings.inference_delay_sec)
        df = pd.DataFrame([flight])
        df["flight_date"] = df["flight_date"].astype(str)
        probability = model.predict_proba(df)[0]
        inference_duration.record(
            time.perf_counter() - started, {"model.version": model.version}
        )
        return probability


def parse_task(body: Any) -> TaskMessage:
    if isinstance(body, bytes | str):
        return TaskMessage.model_validate_json(body)
    return TaskMessage.model_validate(body)


def delivery_attempt(message: Any) -> int:
    count = message.headers.get("x-delivery-count")
    if count is not None:
        return int(count) + 1
    return 2 if getattr(message.raw_message, "redelivered", False) else 1


def mark_failed(span: trace.Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


@broker.subscriber(
    tasks_queue(),
    ack_policy=AckPolicy.REJECT_ON_ERROR,
)
async def handle_task(body: Any, message: RabbitMessage) -> None:
    span = trace.get_current_span()
    attempt = delivery_attempt(message)
    max_attempts = settings.delivery_limit + 1
    span.set_attribute("messaging.delivery.attempt", attempt)
    span.set_attribute("worker.id", WORKER_ID)
    if attempt > 1:
        redeliveries.add(1)
        span.add_event("redelivery", {"attempt": attempt})
        logger.warning(
            "task redelivered",
            extra={"attempt": attempt, "max_attempts": max_attempts},
        )

    try:
        task = parse_task(body)
    except ValidationError as exc:
        state.rejected += 1
        tasks_processed.add(1, {"result": "dlq"})
        mark_failed(span, exc)
        span.add_event("dead_lettered", {"reason": "invalid_message"})
        logger.error(
            "invalid task message, sending to DLQ",
            extra={
                "reason": "invalid_message",
                "errors": [
                    {"loc": error["loc"], "type": error["type"]}
                    for error in exc.errors()[:3]
                ],
            },
        )
        raise RejectMessage() from exc

    assert state.model is not None and state.store is not None
    span.set_attribute("task.id", str(task.task_id))
    started_at = datetime.now(UTC)
    queue_wait_sec = (started_at - task.created_at).total_seconds()
    span.set_attribute("task.queue_wait_sec", round(queue_wait_sec, 3))

    state.in_flight += 1
    tasks_in_flight.add(1)
    try:
        loop = asyncio.get_running_loop()
        flight = task.flight.model_dump(mode="json")
        context = contextvars.copy_context()
        probability = await loop.run_in_executor(
            state.executor, context.run, run_inference, state.model, flight
        )

        with tracer.start_as_current_span(
            "db.save_result",
            attributes={"db.system.name": "postgresql", "db.operation.name": "upsert"},
        ):
            saved = await state.store.save_result(
                TaskResult(
                    task_id=task.task_id,
                    source=task.source,
                    payload=flight,
                    delay_probability=probability,
                    is_delayed=probability >= DELAY_THRESHOLD,
                    model_version=state.model.version,
                    worker=WORKER_ID,
                    created_at=task.created_at,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            )
    except Exception as exc:
        state.failed += 1
        final = attempt >= max_attempts
        tasks_processed.add(1, {"result": "dlq" if final else "error"})
        mark_failed(span, exc)
        logger.exception(
            "task processing failed",
            extra={
                "attempt": attempt,
                "max_attempts": max_attempts,
                "will_retry": not final,
            },
        )
        if final:
            span.add_event("dead_lettered", {"reason": "delivery_limit"})
            logger.error(
                "task sent to DLQ: delivery limit reached",
                extra={"reason": "delivery_limit", "attempt": attempt},
            )
        raise RejectMessage(requeue=True) from exc
    finally:
        state.in_flight -= 1
        tasks_in_flight.add(-1)

    if saved:
        state.processed += 1
        tasks_processed.add(1, {"result": "ok"})
        logger.info(
            "task processed",
            extra={
                "attempt": attempt,
                "queue_wait_sec": round(queue_wait_sec, 3),
                "model_version": state.model.version,
            },
        )
    else:
        state.duplicates += 1
        tasks_processed.add(1, {"result": "duplicate"})
        span.add_event("duplicate_ignored")
        logger.info("duplicate delivery ignored", extra={"attempt": attempt})


def health_payload() -> dict[str, Any]:
    return {
        "status": "ok" if state.model is not None else "starting",
        "worker": WORKER_ID,
        "model_version": state.model.version if state.model else None,
        "in_flight": state.in_flight,
        "processed": state.processed,
        "duplicates": state.duplicates,
        "rejected_to_dlq": state.rejected,
        "failed": state.failed,
        "prefetch_count": settings.prefetch_count,
        "concurrency": settings.worker_concurrency,
        "rss_mb": round(rss_mb(), 1),
    }


@get
async def health(scope: Any) -> AsgiResponse:
    return AsgiResponse(
        json.dumps(health_payload()).encode(),
        status_code=200 if state.model is not None else 503,
        headers={"Content-Type": "application/json"},
    )


async def on_startup() -> None:
    state.model = await asyncio.to_thread(load_model)
    state.store = await TaskStore.connect(
        settings.database_url, max_size=settings.worker_concurrency + 2
    )
    state.executor = ThreadPoolExecutor(
        max_workers=settings.worker_concurrency, thread_name_prefix="inference"
    )
    await broker.connect()
    await declare_topology(broker)
    logger.info(
        "worker ready",
        extra={
            "worker_id": WORKER_ID,
            "model_alias": state.model.alias,
            "model_version": state.model.version,
            "concurrency": settings.worker_concurrency,
            "prefetch": settings.prefetch_count,
            "queue": settings.tasks_queue,
        },
    )


async def after_startup() -> None:
    state.metrics = MetricsWriter(
        "worker",
        WORKER_ID,
        lambda: {
            "processed": state.processed,
            "in_flight": state.in_flight,
            "duplicates": state.duplicates,
            "rss_mb": round(rss_mb(), 1),
        },
    )
    state.metrics.start()


async def on_shutdown() -> None:
    logger.info(
        "graceful shutdown started: stop consuming, waiting for in-flight tasks",
        extra={"in_flight": state.in_flight},
    )


async def after_shutdown() -> None:
    if state.metrics:
        await state.metrics.stop()
    if state.executor:
        state.executor.shutdown(wait=True)
    if state.store:
        await state.store.close()
    logger.info(
        "graceful shutdown finished",
        extra={"in_flight": state.in_flight, "processed": state.processed},
    )
    shutdown_telemetry()


app = AsgiFastStream(
    broker,
    asgi_routes=[("/health", health)],
    on_startup=[on_startup],
    after_startup=[after_startup],
    on_shutdown=[on_shutdown],
    after_shutdown=[after_shutdown],
)
