import asyncio
import contextlib
import csv
import logging
import os
import random
import signal
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from faststream.rabbit import RabbitBroker
from opentelemetry.context import Context

from flight_delay_mlops.config import Settings, settings
from flight_delay_mlops.logs import bind_task_id, setup_logging
from flight_delay_mlops.messaging import declare_topology, make_broker, publish_task
from flight_delay_mlops.metrics import MetricsWriter, rss_mb
from flight_delay_mlops.schemas import Flight, TaskMessage
from flight_delay_mlops.telemetry import (
    meter,
    setup_telemetry,
    shutdown_telemetry,
    tracer,
)

SERVICE_NAME = "flight-source"

logger = logging.getLogger("flight_delay_mlops.source")

DATA_PATH = Path(os.getenv("SOURCE_DATA_PATH", "data/flights_2013.csv"))
MAX_TASKS = int(os.getenv("SOURCE_MAX_TASKS", "0"))
DURATION_SEC = float(os.getenv("SOURCE_DURATION_SEC", "0"))

published_tasks = meter.create_counter(
    "source.tasks.published",
    unit="{task}",
    description="Tasks accepted by the broker",
)
broker_rejections = meter.create_counter(
    "source.broker.rejections",
    unit="{rejection}",
    description="Publishes rejected by the broker (nack, queue is full)",
)
publish_errors = meter.create_counter(
    "source.publish.errors",
    unit="{error}",
    description="Publishes failed because the broker is unavailable",
)
backoff_wait = meter.create_counter(
    "source.backoff.wait",
    unit="s",
    description="Total time the source waited for free space in the queue",
)


class SourceStats:
    def __init__(self) -> None:
        self.published = 0
        self.rejected = 0
        self.errors = 0
        self.backoff_sec = 0.0
        self.wait_total_sec = 0.0
        self.throttled = False

    def snapshot(self) -> dict[str, float]:
        return {
            "published": self.published,
            "rejected": self.rejected,
            "errors": self.errors,
            "backoff_sec": round(self.backoff_sec, 3),
            "rss_mb": round(rss_mb(), 1),
        }


def read_tasks(path: Path, run_label: str) -> Iterator[TaskMessage]:
    with path.open(newline="") as f:
        for row_number, row in enumerate(csv.DictReader(f)):
            flight = Flight(
                flight_date=row["flight_date"],
                sched_dep_time=int(row["sched_dep_time"]),
                sched_arr_time=int(row["sched_arr_time"]),
                carrier=row["carrier"],
                origin=row["origin"],
                dest=row["dest"],
                distance=float(row["distance"]),
            )
            yield TaskMessage(
                task_id=uuid5(NAMESPACE_URL, f"{path.name}:{run_label}:{row_number}"),
                source=run_label,
                created_at=datetime.now(UTC),
                flight=flight,
            )


def backoff_delay(attempt: int, cfg: Settings = settings) -> float:
    cap = min(cfg.backoff_max_sec, cfg.backoff_initial_sec * 2**attempt)
    return random.uniform(cap / 2, cap)


async def wait_for_capacity(
    attempt: int, stats: SourceStats, stop: asyncio.Event, cfg: Settings
) -> None:
    delay = backoff_delay(attempt, cfg)
    stats.backoff_sec = delay
    with tracer.start_as_current_span(
        "source.wait_for_capacity",
        attributes={"backoff.attempt": attempt + 1, "backoff.delay_sec": delay},
    ):
        started = time.perf_counter()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        waited = time.perf_counter() - started
    stats.wait_total_sec += waited
    backoff_wait.add(waited)


async def publish_with_backoff(
    broker: RabbitBroker,
    task: TaskMessage,
    stats: SourceStats,
    stop: asyncio.Event,
    cfg: Settings = settings,
) -> bool:
    attempt = 0
    with (
        bind_task_id(task.task_id),
        tracer.start_as_current_span(
            "source.publish_task",
            context=Context(),
            attributes={"task.id": str(task.task_id), "source.run": cfg.run_label},
        ) as span,
    ):
        while not stop.is_set():
            task.created_at = datetime.now(UTC)
            try:
                accepted = await publish_task(broker, task, cfg)
            except Exception as exc:
                stats.errors += 1
                publish_errors.add(1)
                span.record_exception(exc)
                logger.exception(
                    "publish failed, broker unavailable", extra={"attempt": attempt + 1}
                )
            else:
                if accepted:
                    stats.published += 1
                    stats.backoff_sec = 0.0
                    published_tasks.add(1)
                    span.set_attribute("source.publish_attempts", attempt + 1)
                    if stats.throttled and attempt == 0:
                        stats.throttled = False
                        logger.info(
                            "queue has free space again, source back to full speed",
                            extra=stats.snapshot(),
                        )
                    return True

                stats.rejected += 1
                broker_rejections.add(1)
                span.add_event("broker.rejected", {"attempt": attempt + 1})
                if not stats.throttled:
                    stats.throttled = True
                    logger.warning(
                        "task queue is full, broker rejected publish: source slows down",
                        extra={
                            "queue": cfg.tasks_queue,
                            "max_length": cfg.queue_max_length,
                        },
                    )
                else:
                    logger.debug(
                        "broker rejected publish", extra={"attempt": attempt + 1}
                    )

            await wait_for_capacity(attempt, stats, stop, cfg)
            attempt += 1

        span.add_event("source.stopped")
        return False


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        if not stop.is_set():
            logger.info("graceful shutdown started", extra={"signal": sig.name})
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop, sig)

    stats = SourceStats()
    metrics = MetricsWriter("source", socket.gethostname(), stats.snapshot)

    broker = make_broker()
    await broker.connect()
    await declare_topology(broker)
    metrics.start()

    started = time.monotonic()
    logger.info(
        "source started",
        extra={
            "file": DATA_PATH.name,
            "run": settings.run_label,
            "queue": settings.tasks_queue,
            "max_length": settings.queue_max_length or "unbounded",
        },
    )
    try:
        for task in read_tasks(DATA_PATH, settings.run_label):
            if stop.is_set():
                break
            if MAX_TASKS and stats.published >= MAX_TASKS:
                break
            if DURATION_SEC and time.monotonic() - started >= DURATION_SEC:
                break
            if not await publish_with_backoff(broker, task, stats, stop):
                break
            if stats.published % 1000 == 0:
                logger.info("progress", extra=stats.snapshot())
    finally:
        await metrics.stop()
        await broker.stop()
        elapsed = time.monotonic() - started
        logger.info(
            "graceful shutdown finished" if stop.is_set() else "source finished",
            extra={
                **stats.snapshot(),
                "wait_total_sec": round(stats.wait_total_sec, 3),
                "elapsed_sec": round(elapsed, 1),
                "avg_rate": round(stats.published / elapsed, 1) if elapsed else 0,
            },
        )
        shutdown_telemetry()


def run() -> None:
    setup_logging(SERVICE_NAME)
    setup_telemetry(SERVICE_NAME)
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("source crashed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
