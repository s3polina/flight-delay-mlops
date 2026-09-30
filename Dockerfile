FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --group web --no-install-project

COPY src ./src

RUN uv sync --frozen --no-dev --group web

RUN useradd --create-home appuser

USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "flight_delay_mlops.main:app", "--host", "0.0.0.0", "--port", "8000"]