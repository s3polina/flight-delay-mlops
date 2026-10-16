"""Признаки для модели задержки рейсов.

Модуль используется и при обучении (внутри sklearn Pipeline),
и в приложении, поэтому преобразования всегда одинаковые.
"""

import pandas as pd

# Что модель получает на вход: только то, что известно ДО вылета
RAW_FEATURES = [
    "flight_date",  # дата рейса, "YYYY-MM-DD"
    "sched_dep_time",  # плановое время вылета, HHMM (например 1530)
    "sched_arr_time",  # плановое время прилёта, HHMM
    "carrier",  # код авиакомпании
    "origin",  # аэропорт вылета
    "dest",  # аэропорт прилёта
    "distance",  # расстояние, мили
]

TARGET = "is_delayed"  # 1, если прилёт задержан больше чем на 15 минут

NUMERIC_FEATURES = ["month", "day", "day_of_week", "dep_hour", "arr_hour", "distance"]
CATEGORICAL_FEATURES = ["carrier", "origin", "dest"]


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Сырые поля рейса -> признаки модели."""
    flight_date = pd.to_datetime(df["flight_date"])

    features = pd.DataFrame(index=df.index)
    features["month"] = flight_date.dt.month
    features["day"] = flight_date.dt.day
    features["day_of_week"] = flight_date.dt.dayofweek
    features["dep_hour"] = df["sched_dep_time"] // 100
    features["arr_hour"] = df["sched_arr_time"] // 100
    features["distance"] = df["distance"].astype(float)

    for column in CATEGORICAL_FEATURES:
        features[column] = df[column].astype(str)

    return features
