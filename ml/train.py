"""Шаг 2. Эксперименты: baseline и несколько вариантов модели.

Каждая конфигурация = отдельный Run в эксперименте `flight-delay`.
В каждый Run пишем: датасеты (train/test), параметры, метрики,
диагностические графики и саму модель.

Запуск:
    uv run python ml/train.py                     # все конфигурации
    uv run python ml/train.py --config hgb_tuned  # одна
"""

import argparse

import mlflow
import mlflow.sklearn
import pandas as pd
from common import (
    DATASET_NAME,
    RANDOM_STATE,
    TEST_SIZE,
    load_data,
    new_figure,
    setup_mlflow,
    to_mlflow_dataset,
)
from mlflow.models import infer_signature
from sklearn.calibration import calibration_curve
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    PrecisionRecallDisplay,
    RocCurveDisplay,
    accuracy_score,
    average_precision_score,
    classification_report,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import (
    FunctionTransformer,
    OneHotEncoder,
    OrdinalEncoder,
    StandardScaler,
)

from flight_delay_mlops.features import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    RAW_FEATURES,
    TARGET,
    add_features,
)

THRESHOLD = 0.5

# Описание экспериментов: что меняем от Run к Run
CONFIGS = {
    # Baseline: простая линейная модель на 3 числовых признаках
    "baseline_logreg": {
        "model": "logreg",
        "numeric": ["dep_hour", "distance", "month"],
        "categorical": [],
        "params": {"C": 1.0, "class_weight": None},
    },
    # + все признаки, one-hot для категорий, учёт дисбаланса классов
    "logreg_onehot": {
        "model": "logreg",
        "numeric": NUMERIC_FEATURES,
        "categorical": CATEGORICAL_FEATURES,
        "params": {"C": 1.0, "class_weight": "balanced"},
    },
    # Нелинейная модель: градиентный бустинг с параметрами по умолчанию
    "hgb_default": {
        "model": "hgb",
        "numeric": NUMERIC_FEATURES,
        "categorical": CATEGORICAL_FEATURES,
        "params": {},
    },
    # Бустинг с подобранными гиперпараметрами
    "hgb_tuned": {
        "model": "hgb",
        "numeric": NUMERIC_FEATURES,
        "categorical": CATEGORICAL_FEATURES,
        "params": {
            "learning_rate": 0.1,
            "max_iter": 500,
            "max_leaf_nodes": 63,
            "l2_regularization": 1.0,
            "class_weight": "balanced",
        },
    },
}


def build_pipeline(config: dict) -> Pipeline:
    numeric, categorical = config["numeric"], config["categorical"]

    if config["model"] == "logreg":
        preprocess = ColumnTransformer(
            [
                ("num", StandardScaler(), numeric),
                ("cat", OneHotEncoder(handle_unknown="ignore"), categorical),
            ]
        )
        model = LogisticRegression(max_iter=1000, **config["params"])
    else:
        preprocess = ColumnTransformer(
            [
                ("num", "passthrough", numeric),
                (
                    "cat",
                    OrdinalEncoder(
                        handle_unknown="use_encoded_value", unknown_value=-1
                    ),
                    categorical,
                ),
            ]
        )
        cat_idx = list(range(len(numeric), len(numeric) + len(categorical)))
        model = HistGradientBoostingClassifier(
            categorical_features=cat_idx,
            random_state=RANDOM_STATE,
            **config["params"],
        )

    # Всё, включая расчёт признаков, внутри одного Pipeline:
    # модель из Registry принимает "сырые" поля рейса
    return Pipeline(
        [
            ("features", FunctionTransformer(add_features)),
            ("preprocess", preprocess),
            ("model", model),
        ]
    )


def compute_metrics(y_true: pd.Series, proba) -> dict[str, float]:
    pred = (proba >= THRESHOLD).astype(int)
    return {
        "roc_auc": roc_auc_score(y_true, proba),  # основная метрика
        "pr_auc": average_precision_score(y_true, proba),
        "f1": f1_score(y_true, pred),
        "precision": precision_score(y_true, pred, zero_division=0),
        "recall": recall_score(y_true, pred),
        "accuracy": accuracy_score(y_true, pred),
        "log_loss": log_loss(y_true, proba),
    }


def log_diagnostics(pipe: Pipeline, X_test, y_test, proba) -> None:
    pred = (proba >= THRESHOLD).astype(int)

    fig, ax = new_figure((5, 5))
    RocCurveDisplay.from_predictions(y_test, proba, ax=ax)
    ax.plot([0, 1], [0, 1], "k--", linewidth=0.8)
    mlflow.log_figure(fig, "diagnostics/roc_curve.png")

    fig, ax = new_figure((5, 5))
    PrecisionRecallDisplay.from_predictions(y_test, proba, ax=ax)
    mlflow.log_figure(fig, "diagnostics/pr_curve.png")

    fig, ax = new_figure((5, 5))
    ConfusionMatrixDisplay.from_predictions(y_test, pred, normalize="true", ax=ax)
    ax.set_title(f"Confusion matrix (threshold={THRESHOLD})")
    mlflow.log_figure(fig, "diagnostics/confusion_matrix.png")

    prob_true, prob_pred = calibration_curve(y_test, proba, n_bins=10)
    fig, ax = new_figure((5, 5))
    ax.plot(prob_pred, prob_true, marker="o", label="модель")
    ax.plot([0, 1], [0, 1], "k--", linewidth=0.8, label="идеально")
    ax.set_xlabel("предсказанная вероятность")
    ax.set_ylabel("фактическая доля задержек")
    ax.set_title("Калибровка")
    ax.legend()
    mlflow.log_figure(fig, "diagnostics/calibration.png")

    # Важность исходных полей: насколько падает ROC-AUC, если перемешать поле
    sample = X_test.sample(5000, random_state=RANDOM_STATE)
    importance = permutation_importance(
        pipe,
        sample,
        y_test.loc[sample.index],
        scoring="roc_auc",
        n_repeats=3,
        random_state=RANDOM_STATE,
    )
    imp = pd.Series(importance.importances_mean, index=RAW_FEATURES).sort_values()
    fig, ax = new_figure((6, 4))
    imp.plot.barh(ax=ax, color="#4C72B0")
    ax.set_title("Permutation importance (падение ROC-AUC)")
    fig.tight_layout()
    mlflow.log_figure(fig, "diagnostics/permutation_importance.png")

    mlflow.log_dict(
        classification_report(y_test, pred, output_dict=True, zero_division=0),
        "diagnostics/classification_report.json",
    )


def run_experiment(name: str, config: dict, splits: dict) -> None:
    X_train, X_test, y_train, y_test = (
        splits["X_train"],
        splits["X_test"],
        splits["y_train"],
        splits["y_test"],
    )

    with mlflow.start_run(run_name=name) as run:
        mlflow.set_tag("stage", "training")

        # Dataset tracking: связываем Run с данными, на которых он обучен/оценён
        train_ds = to_mlflow_dataset(splits["train_df"], f"{DATASET_NAME}-train")
        test_ds = to_mlflow_dataset(splits["test_df"], f"{DATASET_NAME}-test")
        mlflow.log_input(train_ds, context="training")
        mlflow.log_input(test_ds, context="testing")

        mlflow.log_params(
            {
                "model_type": config["model"],
                "numeric_features": ",".join(config["numeric"]),
                "categorical_features": ",".join(config["categorical"]),
                "threshold": THRESHOLD,
                "test_size": TEST_SIZE,
                "random_state": RANDOM_STATE,
                **{f"model__{k}": v for k, v in config["params"].items()},
            }
        )

        pipe = build_pipeline(config)
        pipe.fit(X_train, y_train)

        proba_test = pipe.predict_proba(X_test)[:, 1]
        proba_train = pipe.predict_proba(X_train)[:, 1]

        # Модель: pyfunc отдаёт predict_proba -> [P(вовремя), P(задержка)]
        model_info = mlflow.sklearn.log_model(
            pipe,
            name="model",
            signature=infer_signature(
                X_test.head(100), pipe.predict_proba(X_test.head(100))
            ),
            input_example=X_test.head(3),
            pyfunc_predict_fn="predict_proba",
            # skops (формат по умолчанию) не умеет в нашу функцию add_features
            serialization_format=mlflow.sklearn.SERIALIZATION_FORMAT_CLOUDPICKLE,
        )

        # Метрики привязаны к Run, к модели (model_id) и к датасету
        test_metrics = compute_metrics(y_test, proba_test)
        mlflow.log_metrics(test_metrics, model_id=model_info.model_id, dataset=test_ds)
        mlflow.log_metric(
            "train_roc_auc",
            roc_auc_score(y_train, proba_train),
            model_id=model_info.model_id,
            dataset=train_ds,
        )

        log_diagnostics(pipe, X_test, y_test, proba_test)
        mlflow.set_tag("model_uri", model_info.model_uri)

        print(
            f"{name:16s} run_id={run.info.run_id} "
            f"roc_auc={test_metrics['roc_auc']:.4f} pr_auc={test_metrics['pr_auc']:.4f} "
            f"f1={test_metrics['f1']:.4f}"
        )


def make_splits() -> dict:
    df = load_data()
    train_df, test_df = train_test_split(
        df, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=df[TARGET]
    )
    return {
        "train_df": train_df,
        "test_df": test_df,
        "X_train": train_df[RAW_FEATURES],
        "X_test": test_df[RAW_FEATURES],
        "y_train": train_df[TARGET],
        "y_test": test_df[TARGET],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=list(CONFIGS), help="одна конфигурация")
    args = parser.parse_args()

    setup_mlflow()
    splits = make_splits()

    names = [args.config] if args.config else list(CONFIGS)
    for name in names:
        run_experiment(name, CONFIGS[name], splits)


if __name__ == "__main__":
    main()
