"""Шаг 3. Зарегистрировать модель из Run в Model Registry.

Каждый вызов создаёт НОВУЮ версию зарегистрированной модели
`flight-delay-classifier` (v1, v2, ...). Версия хранит ссылку на Run,
из которого она получена (а Run - на датасеты, параметры и метрики).

Примеры:
    # зарегистрировать лучший по ROC-AUC Run и сделать его champion
    uv run python ml/register.py --best --alias champion

    # зарегистрировать конкретный Run по имени
    uv run python ml/register.py --run-name logreg_onehot --alias challenger
"""

import argparse

import mlflow
from common import EXPERIMENT_NAME, MODEL_NAME, setup_mlflow
from mlflow import MlflowClient

MAIN_METRIC = "roc_auc"


def find_run(run_name: str | None, best: bool):
    order = f"metrics.{MAIN_METRIC} DESC" if best else "attributes.start_time DESC"
    query = "tags.stage = 'training'"
    if run_name:
        query += f" and attributes.run_name = '{run_name}'"

    runs = mlflow.search_runs(
        experiment_names=[EXPERIMENT_NAME],
        filter_string=query,
        order_by=[order],
        max_results=1,
        output_format="list",
    )
    if not runs:
        raise SystemExit(f"Не найден Run по запросу: {query}")
    return runs[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-name", help="имя Run (берётся самый свежий)")
    group.add_argument(
        "--best", action="store_true", help=f"Run с лучшим {MAIN_METRIC}"
    )
    parser.add_argument("--alias", help="какой alias повесить на новую версию")
    args = parser.parse_args()

    setup_mlflow()
    client = MlflowClient()

    run = find_run(args.run_name, args.best)
    model_uri = run.data.tags["model_uri"]
    score = run.data.metrics[MAIN_METRIC]

    version = mlflow.register_model(
        model_uri,
        MODEL_NAME,
        tags={"run_name": run.info.run_name, MAIN_METRIC: f"{score:.4f}"},
    )
    client.update_model_version(
        MODEL_NAME,
        version.version,
        description=f"Run '{run.info.run_name}' ({run.info.run_id}), "
        f"{MAIN_METRIC}={score:.4f}",
    )

    if args.alias:
        client.set_registered_model_alias(MODEL_NAME, args.alias, version.version)

    print(
        f"Registered {MODEL_NAME} v{version.version} from run "
        f"'{run.info.run_name}' ({MAIN_METRIC}={score:.4f})"
        + (f", alias @{args.alias}" if args.alias else "")
    )


if __name__ == "__main__":
    main()
