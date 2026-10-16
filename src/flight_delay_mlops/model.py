"""Загрузка модели из MLflow Model Registry."""

import logging
import os
from dataclasses import dataclass
from typing import Any

import mlflow
import numpy as np
import pandas as pd
from mlflow import MlflowClient

logger = logging.getLogger("flight_delay_mlops")

MODEL_NAME = os.getenv("MODEL_NAME", "flight-delay-classifier")
MODEL_ALIAS = os.getenv("MODEL_ALIAS", "champion")
DELAY_THRESHOLD = float(os.getenv("DELAY_THRESHOLD", "0.5"))


@dataclass(frozen=True)
class LoadedModel:
    model: Any  # mlflow.pyfunc.PyFuncModel
    name: str
    alias: str
    version: str
    run_id: str

    def predict_proba(self, df: pd.DataFrame) -> list[float]:
        """Вероятность задержки (класс 1) для каждой строки."""
        proba = np.asarray(self.model.predict(df))
        return [float(p) for p in proba[:, 1]]


def load_model(name: str = MODEL_NAME, alias: str = MODEL_ALIAS) -> LoadedModel:
    """Найти версию по alias и загрузить именно её.

    Сначала резолвим alias -> конкретный номер версии, затем грузим
    models:/<name>/<version>. Так приложение точно знает, какую версию
    оно обслуживает, даже если alias потом переключат.

    MLFLOW_TRACKING_URI берётся из переменной окружения.
    """
    client = MlflowClient()
    model_version = client.get_model_version_by_alias(name, alias)

    model = mlflow.pyfunc.load_model(f"models:/{name}/{model_version.version}")

    logger.info(
        "model loaded name=%s alias=%s version=%s run_id=%s",
        name,
        alias,
        model_version.version,
        model_version.run_id,
    )
    return LoadedModel(
        model=model,
        name=name,
        alias=alias,
        version=str(model_version.version),
        run_id=model_version.run_id or "",
    )
