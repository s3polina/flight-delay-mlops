from importlib.metadata import version
from time import perf_counter

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from flight_delay_mlops.database import check_database

app = FastAPI(
    title="Flight Delay MLOps",
)


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
