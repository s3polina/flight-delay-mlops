"""Общие настройки для скриптов EDA / обучения / регистрации."""

import os
from pathlib import Path

import mlflow
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from mlflow.data.pandas_dataset import PandasDataset

from flight_delay_mlops.features import RAW_FEATURES, TARGET

TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
EXPERIMENT_NAME = os.getenv("MLFLOW_EXPERIMENT_NAME", "flight-delay")
MODEL_NAME = os.getenv("MODEL_NAME", "flight-delay-classifier")

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "flights_2013.csv"
DATASET_NAME = "nycflights13-2013"
RAW_SOURCE = "PyPI package nycflights13 (nycflights13.flights)"

RANDOM_STATE = 42
TEST_SIZE = 0.2


def setup_mlflow() -> None:
    mlflow.set_tracking_uri(TRACKING_URI)
    mlflow.set_experiment(EXPERIMENT_NAME)


def load_data() -> pd.DataFrame:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"{DATA_PATH} не найден. Сначала запусти: uv run python ml/prepare_data.py"
        )
    return pd.read_csv(DATA_PATH)


def to_mlflow_dataset(df: pd.DataFrame, name: str) -> PandasDataset:
    """DataFrame -> MLflow Dataset.

    source - откуда взяты данные (файл подготовленного датасета),
    digest - хеш содержимого: MLflow считает его сам, при изменении данных он меняется.
    """
    return mlflow.data.from_pandas(
        df[RAW_FEATURES + [TARGET]],
        source=str(DATA_PATH),
        name=name,
        targets=TARGET,
    )


def new_figure(figsize: tuple[int, int]) -> tuple[Figure, Axes]:
    """Фигура без pyplot: не нужен GUI-бэкенд, ничего не копится в памяти."""
    fig = Figure(figsize=figsize)
    return fig, fig.subplots()
