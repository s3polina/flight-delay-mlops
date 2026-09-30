import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from flight_delay_mlops.main import app


@pytest.mark.asyncio
async def test_healthz() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_version() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/version")

    assert response.status_code == 200

    data = response.json()

    assert "version" in data
    assert data["version"]


@pytest.mark.asyncio
async def test_health_database_available(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_check_database() -> str:
        return "PostgreSQL 17"

    monkeypatch.setattr(
        "flight_delay_mlops.main.check_database",
        mock_check_database,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "ok"
    assert data["database"] == "PostgreSQL 17"
    assert "response_time_ms" in data


@pytest.mark.asyncio
async def test_health_database_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_check_database() -> str:
        raise asyncpg.PostgresError("Database unavailable")

    monkeypatch.setattr(
        "flight_delay_mlops.main.check_database",
        mock_check_database,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503

    data = response.json()

    assert data["status"] == "error"
    assert data["database"] == "unavailable"
    assert "response_time_ms" in data
