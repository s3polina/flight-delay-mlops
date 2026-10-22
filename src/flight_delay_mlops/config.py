import math
import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    rabbit_url: str = os.getenv("RABBIT_URL", "amqp://guest:guest@localhost:5672/")
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5433/flight_delay",
    )

    tasks_queue: str = os.getenv("TASKS_QUEUE", "flight.tasks")
    queue_max_length: int = _env_int("QUEUE_MAX_LENGTH", 100)
    delivery_limit: int = _env_int("DELIVERY_LIMIT", 3)
    dead_letter_exchange: str = os.getenv("DEAD_LETTER_EXCHANGE", "flight.dlx")
    dead_letter_queue: str = os.getenv("DEAD_LETTER_QUEUE", "flight.tasks.dlq")

    inference_delay_sec: float = _env_float("INFERENCE_DELAY_SEC", 0.2)
    worker_concurrency: int = _env_int("WORKER_CONCURRENCY", 4)
    prefetch_count: int = _env_int("PREFETCH_COUNT", _env_int("WORKER_CONCURRENCY", 4))
    graceful_timeout_sec: float = _env_float("GRACEFUL_TIMEOUT_SEC", 30.0)

    backoff_initial_sec: float = _env_float("BACKOFF_INITIAL_SEC", 0.05)
    backoff_max_sec: float = _env_float("BACKOFF_MAX_SEC", 2.0)

    retry_after_min_sec: int = _env_int("RETRY_AFTER_MIN_SEC", 1)
    retry_after_max_sec: int = _env_int("RETRY_AFTER_MAX_SEC", 60)

    metrics_dir: str = os.getenv("METRICS_DIR", "experiments/results")
    run_label: str = os.getenv("RUN_LABEL", "manual")

    @property
    def retry_after_sec(self) -> int:
        drain_time = (
            self.queue_max_length * self.inference_delay_sec / self.worker_concurrency
        )
        return max(
            self.retry_after_min_sec,
            min(self.retry_after_max_sec, math.ceil(drain_time)),
        )


settings = Settings()
