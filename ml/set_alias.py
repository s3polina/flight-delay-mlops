"""Переключить alias на другую версию модели (то же самое можно сделать в UI).

Пример:
    uv run python ml/set_alias.py --alias champion --version 1

Приложение подхватит новую версию после перезапуска:
    docker compose restart app
"""

import argparse

from common import MODEL_NAME, setup_mlflow
from mlflow import MlflowClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", default="champion")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()

    setup_mlflow()
    client = MlflowClient()
    client.set_registered_model_alias(MODEL_NAME, args.alias, args.version)

    for found in client.search_model_versions(f"name = '{MODEL_NAME}'"):
        # search_model_versions не возвращает aliases - берём версию целиком
        v = client.get_model_version(MODEL_NAME, found.version)
        aliases = ", ".join(f"@{a}" for a in v.aliases) or "-"
        print(f"v{v.version}: {aliases}  ({v.description})")


if __name__ == "__main__":
    main()
