"""Шаг 0. Скачать сырые данные и подготовить датасет.

Источник: пакет nycflights13 - все рейсы из аэропортов Нью-Йорка
(JFK, LGA, EWR) за 2013 год, ~337 тыс. строк.

Запуск: uv run python ml/prepare_data.py
"""

import json
from importlib.util import find_spec
from pathlib import Path

import pandas as pd
from common import DATA_PATH, RAW_SOURCE

DELAY_THRESHOLD_MINUTES = 15


def load_raw_flights() -> pd.DataFrame:
    """Читаем файл с данными из установленного пакета nycflights13.

    `import nycflights13` не используем: пакет 2020 года импортирует
    pkg_resources, которого нет в новых окружениях. find_spec находит
    папку пакета, не выполняя его код.
    """
    spec = find_spec("nycflights13")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("Пакет nycflights13 не установлен: uv sync --all-groups")
    package_dir = Path(next(iter(spec.submodule_search_locations)))
    return pd.read_csv(package_dir / "data" / "flights.csv.zip")


def main() -> None:
    raw = load_raw_flights()
    n_raw = len(raw)

    # Рейсы без времени прилёта - отменённые/перенаправленные, для них нет целевой
    n_no_arrival = int(raw["arr_delay"].isna().sum())
    df = raw.dropna(subset=["arr_delay"]).copy()

    df["flight_date"] = pd.to_datetime(df[["year", "month", "day"]]).dt.strftime(
        "%Y-%m-%d"
    )
    df["distance"] = df["distance"].astype(float)
    df["is_delayed"] = (df["arr_delay"] > DELAY_THRESHOLD_MINUTES).astype(int)

    columns = [
        "flight_date",
        "sched_dep_time",
        "sched_arr_time",
        "carrier",
        "origin",
        "dest",
        "distance",
        "arr_delay",
        "is_delayed",
    ]
    df = df[columns].sort_values(["flight_date", "sched_dep_time"])

    DATA_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(DATA_PATH, index=False)

    stats = {
        "raw_source": RAW_SOURCE,
        "rows_raw": n_raw,
        "rows_without_arrival_dropped": n_no_arrival,
        "rows_prepared": len(df),
        "delay_threshold_minutes": DELAY_THRESHOLD_MINUTES,
        "positive_rate": round(float(df["is_delayed"].mean()), 4),
    }
    DATA_PATH.with_name("prepare_stats.json").write_text(json.dumps(stats, indent=2))

    print(json.dumps(stats, indent=2))
    print(f"Saved to {DATA_PATH}")


if __name__ == "__main__":
    main()
