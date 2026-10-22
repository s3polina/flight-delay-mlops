from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from flight_delay_mlops.model import LoadedModel

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
