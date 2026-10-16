import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from flight_delay_mlops.features import add_features
from flight_delay_mlops.main import app, lifespan
from flight_delay_mlops.model import LoadedModel

FLIGHT = {
    "flight_date": "2013-12-20",
    "sched_dep_time": 1830,
    "sched_arr_time": 2135,
    "carrier": "EV",
    "origin": "EWR",
    "dest": "ATL",
    "distance": 746,
}


class FakePyfunc:
    """Ведёт себя как pyfunc-модель: predict -> [P(0), P(1)] для каждой строки."""

    def __init__(self) -> None:
        self.calls: list[pd.DataFrame] = []

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        self.calls.append(df)
        p = np.linspace(0.2, 0.8, len(df))
        return np.column_stack([1 - p, p])


def fake_loaded_model() -> LoadedModel:
    return LoadedModel(
        model=FakePyfunc(),
        name="flight-delay-classifier",
        alias="champion",
        version="2",
        run_id="abc123",
    )


@pytest.fixture
def loaded_model():
    model = fake_loaded_model()
    app.state.model = model
    yield model
    app.state.model = None


async def post_process(payload: dict):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post("/process", json=payload)


@pytest.mark.asyncio
async def test_process_returns_predictions(loaded_model: LoadedModel) -> None:
    response = await post_process({"flights": [FLIGHT, FLIGHT]})

    assert response.status_code == 200
    data = response.json()

    assert data["model"] == {
        "name": "flight-delay-classifier",
        "alias": "champion",
        "version": "2",
        "run_id": "abc123",
    }
    assert data["predictions"] == [
        {"delay_probability": 0.2, "is_delayed": False},
        {"delay_probability": 0.8, "is_delayed": True},
    ]

    df = loaded_model.model.calls[0]
    assert list(df.columns) == list(FLIGHT)
    assert df.loc[0, "flight_date"] == "2013-12-20"


@pytest.mark.asyncio
async def test_process_without_model_returns_503() -> None:
    app.state.model = None

    response = await post_process({"flights": [FLIGHT]})

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_process_validates_input(loaded_model: LoadedModel) -> None:
    assert (await post_process({"flights": []})).status_code == 422
    bad = {**FLIGHT, "sched_dep_time": 2500}
    assert (await post_process({"flights": [bad]})).status_code == 422


@pytest.mark.asyncio
async def test_model_info(loaded_model: LoadedModel) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/model")

    assert response.status_code == 200
    assert response.json()["version"] == "2"


@pytest.mark.asyncio
async def test_lifespan_loads_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def mock_load_model() -> LoadedModel:
        calls.append(1)
        return fake_loaded_model()

    monkeypatch.setattr("flight_delay_mlops.main.load_model", mock_load_model)

    async with lifespan(app):
        assert app.state.model.version == "2"

    assert len(calls) == 1
    app.state.model = None


@pytest.mark.asyncio
async def test_lifespan_survives_mlflow_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_load_model() -> LoadedModel:
        raise ConnectionError("MLflow unavailable")

    monkeypatch.setattr("flight_delay_mlops.main.load_model", mock_load_model)

    async with lifespan(app):
        assert app.state.model is None


def test_add_features() -> None:
    features = add_features(pd.DataFrame([FLIGHT]))

    assert features.loc[0, "month"] == 12
    assert features.loc[0, "day"] == 20
    assert features.loc[0, "day_of_week"] == 4  # пятница
    assert features.loc[0, "dep_hour"] == 18
    assert features.loc[0, "arr_hour"] == 21
    assert features.loc[0, "carrier"] == "EV"
