import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from time import perf_counter

import asyncpg
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from flight_delay_mlops.database import check_database
from flight_delay_mlops.model import DELAY_THRESHOLD, LoadedModel, load_model
from flight_delay_mlops.schemas import (
    ModelInfo,
    Prediction,
    ProcessRequest,
    ProcessResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("flight_delay_mlops")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Модель загружается из MLflow ОДИН раз при старте приложения."""
    app.state.model = None
    try:
        # загрузка блокирующая (сеть + десериализация) -> в отдельном потоке
        app.state.model = await asyncio.to_thread(load_model)
    except Exception:
        # приложение стартует, но /process отвечает 503, пока модель не загружена
        logger.exception("failed to load model from MLflow")
    yield


app = FastAPI(
    title="Flight Delay MLOps",
    lifespan=lifespan,
)


def get_model(request: Request) -> LoadedModel:
    model = getattr(request.app.state, "model", None)
    if model is None:
        raise HTTPException(status_code=503, detail="Model is not loaded")
    return model


def model_info(model: LoadedModel) -> ModelInfo:
    return ModelInfo(
        name=model.name,
        alias=model.alias,
        version=model.version,
        run_id=model.run_id,
    )


@app.middleware("http")
async def log_requests(request: Request, call_next) -> Response:
    start_time = perf_counter()

    response = await call_next(request)

    response_time = (perf_counter() - start_time) * 1000

    logger.info(
        "request method=%s path=%s status_code=%s response_time_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        response_time,
    )

    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/version")
async def get_version() -> dict[str, str]:
    return {
        "version": version("flight-delay-mlops"),
    }


@app.get("/api/v1/health", response_model=None)
async def health() -> dict[str, str | float] | JSONResponse:
    start_time = perf_counter()

    try:
        database_version = await check_database()
        response_time = (perf_counter() - start_time) * 1000

        return {
            "status": "ok",
            "database": database_version,
            "response_time_ms": round(response_time, 2),
        }
    except (asyncpg.PostgresError, OSError):
        response_time = (perf_counter() - start_time) * 1000

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "database": "unavailable",
                "response_time_ms": round(response_time, 2),
            },
        )


@app.get("/api/v1/model")
async def get_model_info(request: Request) -> ModelInfo:
    """Какая версия модели сейчас обслуживает запросы."""
    return model_info(get_model(request))


@app.post("/process")
async def process(payload: ProcessRequest, request: Request) -> ProcessResponse:
    """Inference: вероятность задержки прилёта > 15 минут для каждого рейса."""
    model = get_model(request)

    df = pd.DataFrame([flight.model_dump() for flight in payload.flights])
    df["flight_date"] = df["flight_date"].astype(str)

    # predict - CPU-bound, не блокируем event loop
    probabilities = await asyncio.to_thread(model.predict_proba, df)

    return ProcessResponse(
        model=model_info(model),
        predictions=[
            Prediction(
                delay_probability=round(p, 4),
                is_delayed=p >= DELAY_THRESHOLD,
            )
            for p in probabilities
        ],
    )
