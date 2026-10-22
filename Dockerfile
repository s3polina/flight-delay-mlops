FROM python:3.12-slim

WORKDIR /app

ENV OMP_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "uv>=0.9"

ENV UV_HTTP_TIMEOUT=300

COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --group web --no-install-project

COPY src ./src

RUN uv sync --frozen --no-dev --group web

RUN chmod -R a+rX /app/src \
    && useradd --create-home appuser \
    && mkdir -p /app/experiments/results \
    && chown -R appuser /app/experiments

USER appuser

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "flight_delay_mlops.main:app", "--host", "0.0.0.0", "--port", "8000"]
