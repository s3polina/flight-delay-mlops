"""Шаг 1. Краткий EDA. Все графики и выводы сохраняются как артефакты MLflow.

Запуск: uv run python ml/eda.py
"""

import json

import mlflow
import pandas as pd
from common import (
    DATA_PATH,
    DATASET_NAME,
    load_data,
    new_figure,
    setup_mlflow,
    to_mlflow_dataset,
)
from matplotlib.figure import Figure

from flight_delay_mlops.features import add_features

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def bar_rate(series: pd.Series, title: str, xlabel: str, overall: float) -> Figure:
    fig, ax = new_figure((9, 4))
    series.plot.bar(ax=ax, color="#4C72B0")
    ax.axhline(overall, color="red", linestyle="--", label=f"в среднем {overall:.0%}")
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("доля задержанных рейсов")
    ax.legend()
    fig.tight_layout()
    return fig


def main() -> None:
    setup_mlflow()
    df = load_data()
    features = add_features(df)
    df = pd.concat([df, features[["month", "day_of_week", "dep_hour"]]], axis=1)
    overall = df["is_delayed"].mean()

    with mlflow.start_run(run_name="eda"):
        mlflow.set_tag("stage", "eda")
        mlflow.log_input(to_mlflow_dataset(df, DATASET_NAME), context="eda")

        # 1. Баланс классов
        fig, ax = new_figure((5, 4))
        df["is_delayed"].value_counts().sort_index().plot.bar(
            ax=ax, color=["#55A868", "#C44E52"]
        )
        ax.set_xticklabels(["вовремя (0)", "задержан >15 мин (1)"], rotation=0)
        ax.set_title("Баланс классов")
        fig.tight_layout()
        mlflow.log_figure(fig, "eda/01_class_balance.png")

        # 2. Распределение задержки прилёта
        fig, ax = new_figure((9, 4))
        df["arr_delay"].clip(-60, 180).plot.hist(bins=80, ax=ax, color="#4C72B0")
        ax.axvline(15, color="red", linestyle="--", label="порог 15 мин")
        ax.set_title("Задержка прилёта, мин (обрезано до [-60, 180])")
        ax.legend()
        fig.tight_layout()
        mlflow.log_figure(fig, "eda/02_arr_delay_hist.png")

        # 3-6. Доля задержек в разрезах
        by_hour = df.groupby("dep_hour")["is_delayed"].mean()
        by_month = df.groupby("month")["is_delayed"].mean()
        by_dow = df.groupby("day_of_week")["is_delayed"].mean()
        by_dow.index = [DAY_NAMES[i] for i in by_dow.index]
        by_carrier = df.groupby("carrier")["is_delayed"].mean().sort_values()
        by_origin = df.groupby("origin")["is_delayed"].mean()

        plots = [
            (by_hour, "по плановому часу вылета", "час", "eda/03_rate_by_hour.png"),
            (by_month, "по месяцам", "месяц", "eda/04_rate_by_month.png"),
            (by_dow, "по дням недели", "день недели", "eda/05_rate_by_day_of_week.png"),
            (
                by_carrier,
                "по авиакомпаниям",
                "авиакомпания",
                "eda/06_rate_by_carrier.png",
            ),
        ]
        for series, title, xlabel, path in plots:
            fig = bar_rate(series, f"Доля задержек {title}", xlabel, overall)
            mlflow.log_figure(fig, path)

        # 7. Задержки сильно "кучкуются" по дням (погода, сбои) - важно для разбиения
        daily = df.groupby("flight_date")["is_delayed"].mean()
        fig, ax = new_figure((11, 4))
        daily.index = pd.to_datetime(daily.index)
        daily.plot(ax=ax, color="#4C72B0", linewidth=0.8)
        ax.set_title("Доля задержанных рейсов по дням")
        fig.tight_layout()
        mlflow.log_figure(fig, "eda/07_rate_by_day.png")

        # Таблицы и краткие выводы
        mlflow.log_table(
            df.describe(include="all").T.reset_index(), "eda/describe.json"
        )
        mlflow.log_dict(by_carrier.round(4).to_dict(), "eda/rate_by_carrier.json")

        stats_path = DATA_PATH.with_name("prepare_stats.json")
        if stats_path.exists():
            mlflow.log_artifact(str(stats_path), "eda")

        summary = {
            "rows": len(df),
            "positive_rate": round(float(overall), 4),
            "worst_hour": int(by_hour.idxmax()),
            "best_hour": int(by_hour.idxmin()),
            "worst_month": int(by_month.idxmax()),
            "best_month": int(by_month.idxmin()),
            "worst_carrier": by_carrier.idxmax(),
            "best_carrier": by_carrier.idxmin(),
            "rate_by_origin": by_origin.round(4).to_dict(),
            "daily_rate_min": round(float(daily.min()), 4),
            "daily_rate_max": round(float(daily.max()), 4),
        }
        mlflow.log_metrics(
            {
                "positive_rate": summary["positive_rate"],
                "daily_rate_max": summary["daily_rate_max"],
            }
        )

        conclusions = f"""# EDA: задержки рейсов nycflights13 (2013)

- Строк: {summary["rows"]}, доля задержанных (>15 мин): {overall:.1%}.
  Классы несбалансированы -> accuracy не годится как основная метрика,
  используем ROC-AUC (основная) и PR-AUC / F1 / recall (вспомогательные).
- Час вылета - сильный признак: задержки накапливаются к вечеру
  (меньше всего в {summary["best_hour"]}:00, больше всего в {summary["worst_hour"]}:00).
- Сезонность: хуже всего месяц {summary["worst_month"]}, лучше всего {summary["best_month"]}.
- Авиакомпании заметно различаются: от {summary["best_carrier"]} до {summary["worst_carrier"]}.
- Задержки кучкуются по дням (доля за день от {summary["daily_rate_min"]:.0%}
  до {summary["daily_rate_max"]:.0%}) - погода и сбои. Поэтому признаки month+day
  помогают модели при случайном разбиении; в проде нужно разбиение по времени.
- Признаки берём только известные до вылета (dep_delay, air_time не используем,
  иначе будет утечка целевой переменной).
"""
        mlflow.log_text(conclusions, "eda/conclusions.md")
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
