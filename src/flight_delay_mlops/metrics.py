import asyncio
import contextlib
import csv
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from flight_delay_mlops.config import settings

_process = psutil.Process()


def rss_mb() -> float:
    return _process.memory_info().rss / 2**20


class MetricsWriter:
    def __init__(
        self,
        role: str,
        process_id: str,
        snapshot: Callable[[], dict[str, Any]],
        interval_sec: float = 1.0,
    ) -> None:
        directory = Path(settings.metrics_dir) / settings.run_label
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{role}_{process_id}.csv"
        self.snapshot = snapshot
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.write_row()

    def write_row(self) -> None:
        row = {"ts": round(time.time(), 3), **self.snapshot()}
        is_new = not self.path.exists()
        with self.path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row))
            if is_new:
                writer.writeheader()
            writer.writerow(row)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_sec)
            self.write_row()
