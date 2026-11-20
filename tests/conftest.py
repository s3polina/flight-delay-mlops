from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from flight_delay_mlops.model import LoadedModel
from flight_delay_mlops.telemetry import setup_telemetry

FLIGHT = {
    "flight_date": "2013-12-20",
    "sched_dep_time": 1830,
    "sched_arr_time": 2135,
    "carrier": "EV",
    "origin": "EWR",
    "dest": "ATL",
    "distance": 746.0,
}


class FakePyfunc:
    def __init__(self, probability: float = 0.7) -> None:
        self.probability = probability
        self.calls: list[pd.DataFrame] = []

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        self.calls.append(df)
        p = np.full(len(df), self.probability)
        return np.column_stack([1 - p, p])


@pytest.fixture
def fake_model() -> LoadedModel:
    return LoadedModel(
        model=FakePyfunc(),
        name="flight-delay-classifier",
        alias="champion",
        version="2",
        run_id="abc123",
    )


@pytest.fixture
def make_task():
    def factory(**overrides) -> dict:
        task = {
            "task_id": str(uuid4()),
            "source": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "flight": FLIGHT,
        }
        task.update(overrides)
        return task

    return factory


@pytest.fixture(scope="session")
def span_exporter() -> InMemorySpanExporter:
    setup_telemetry("flight-tests")
    provider = trace.get_tracer_provider()
    if not isinstance(provider, SdkTracerProvider):
        pytest.skip("OpenTelemetry SDK is disabled")
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def spans(span_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    span_exporter.clear()
    return span_exporter
