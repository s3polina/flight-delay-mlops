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

from flight_delay_mlops.config import Settings, settings
from flight_delay_mlops.messaging import declare_topology, publish_task
from flight_delay_mlops.metrics import MetricsWriter, rss_mb
from flight_delay_mlops.schemas import Flight, TaskMessage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("flight_delay_mlops.source")

DATA_PATH = Path(os.getenv("SOURCE_DATA_PATH", "data/flights_2013.csv"))
MAX_TASKS = int(os.getenv("SOURCE_MAX_TASKS", "0"))
DURATION_SEC = float(os.getenv("SOURCE_DURATION_SEC", "0"))


class SourceStats:
    def __init__(self) -> None:
        self.published = 0
        self.rejected = 0
        self.errors = 0
        self.backoff_sec = 0.0

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


async def publish_with_backoff(
    broker: RabbitBroker,
    task: TaskMessage,
    stats: SourceStats,
    stop: asyncio.Event,
    cfg: Settings = settings,
) -> bool:
    attempt = 0
    while not stop.is_set():
        task.created_at = datetime.now(UTC)
        try:
            accepted = await publish_task(broker, task, cfg)
        except Exception:
            stats.errors += 1
            logger.exception("publish failed (broker unavailable?)")
            accepted = False

        if accepted:
            stats.published += 1
            stats.backoff_sec = 0.0
            return True

        stats.rejected += 1
        delay = backoff_delay(attempt, cfg)
        stats.backoff_sec = delay
        attempt += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
    return False


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    stats = SourceStats()
    metrics = MetricsWriter("source", socket.gethostname(), stats.snapshot)

    broker = RabbitBroker(settings.rabbit_url)
    await broker.connect()
    await declare_topology(broker)
    metrics.start()

    started = time.monotonic()
    logger.info(
        "source started file=%s run=%s queue=%s max_length=%s",
        DATA_PATH,
        settings.run_label,
        settings.tasks_queue,
        settings.queue_max_length or "unbounded",
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
                logger.info("progress %s", stats.snapshot())
    finally:
        await metrics.stop()
        await broker.stop()
        elapsed = time.monotonic() - started
        logger.info(
            "source finished in %.1fs: %s, avg rate %.1f tasks/s",
            elapsed,
            stats.snapshot(),
            stats.published / elapsed if elapsed else 0,
        )


if __name__ == "__main__":
    asyncio.run(main())
