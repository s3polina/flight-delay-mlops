import asyncio
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
from faststream import AckPolicy, Logger
from faststream.asgi import AsgiFastStream, AsgiResponse, get
from faststream.exceptions import RejectMessage
from faststream.rabbit import Channel, RabbitBroker
from pydantic import ValidationError

from flight_delay_mlops.config import settings
from flight_delay_mlops.messaging import declare_topology, tasks_queue
from flight_delay_mlops.metrics import MetricsWriter, rss_mb
from flight_delay_mlops.model import DELAY_THRESHOLD, LoadedModel, load_model
from flight_delay_mlops.schemas import TaskMessage
from flight_delay_mlops.storage import TaskResult, TaskStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("flight_delay_mlops.worker")

WORKER_ID = os.getenv("HOSTNAME", socket.gethostname())


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

broker = RabbitBroker(
    settings.rabbit_url,
    graceful_timeout=settings.graceful_timeout_sec,
    default_channel=Channel(prefetch_count=settings.prefetch_count or None),
)


def run_inference(model: LoadedModel, flight: dict[str, Any]) -> float:
    time.sleep(settings.inference_delay_sec)
    df = pd.DataFrame([flight])
    df["flight_date"] = df["flight_date"].astype(str)
    return model.predict_proba(df)[0]


def parse_task(body: Any) -> TaskMessage:
    if isinstance(body, bytes | str):
        return TaskMessage.model_validate_json(body)
    return TaskMessage.model_validate(body)


@broker.subscriber(
    tasks_queue(),
    ack_policy=AckPolicy.REJECT_ON_ERROR,
)
async def handle_task(body: Any, logger: Logger) -> None:
    try:
        task = parse_task(body)
    except ValidationError as exc:
        state.rejected += 1
        logger.warning("invalid task, rejecting to DLQ: %s", exc.errors()[:1])
        raise RejectMessage() from exc

    assert state.model is not None and state.store is not None
    state.in_flight += 1
    started_at = datetime.now(UTC)
    try:
        loop = asyncio.get_running_loop()
        flight = task.flight.model_dump(mode="json")
        probability = await loop.run_in_executor(
            state.executor, run_inference, state.model, flight
        )

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
        logger.warning("task %s failed, will be redelivered: %r", task.task_id, exc)
        raise RejectMessage(requeue=True) from exc
    finally:
        state.in_flight -= 1

    if saved:
        state.processed += 1
    else:
        state.duplicates += 1
        logger.info("duplicate delivery ignored task_id=%s", task.task_id)


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
        "worker ready id=%s model=v%s concurrency=%s prefetch=%s queue=%s",
        WORKER_ID,
        state.model.version,
        settings.worker_concurrency,
        settings.prefetch_count,
        settings.tasks_queue,
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
        "SIGTERM: stop consuming, waiting for %s in-flight task(s)", state.in_flight
    )


async def after_shutdown() -> None:
    logger.info(
        "graceful shutdown complete: in_flight=%s processed=%s",
        state.in_flight,
        state.processed,
    )
    if state.metrics:
        await state.metrics.stop()
    if state.executor:
        state.executor.shutdown(wait=True)
    if state.store:
        await state.store.close()


app = AsgiFastStream(
    broker,
    asgi_routes=[("/health", health)],
    on_startup=[on_startup],
    after_startup=[after_startup],
    on_shutdown=[on_shutdown],
    after_shutdown=[after_shutdown],
)
