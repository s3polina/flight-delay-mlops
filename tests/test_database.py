from unittest.mock import AsyncMock, patch

import pytest

from flight_delay_mlops.database import DATABASE_URL, check_database


@pytest.mark.asyncio
async def test_check_database_returns_version() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = "PostgreSQL 17"

    with patch(
        "flight_delay_mlops.database.asyncpg.connect",
        new=AsyncMock(return_value=connection),
    ) as connect:
        result = await check_database()

    connect.assert_awaited_once_with(DATABASE_URL)
    connection.fetchval.assert_awaited_once_with("SELECT version()")
    connection.close.assert_awaited_once()

    assert result == "PostgreSQL 17"


@pytest.mark.asyncio
async def test_check_database_closes_connection_on_query_error() -> None:
    connection = AsyncMock()
    connection.fetchval.side_effect = RuntimeError("database error")

    with (
        patch(
            "flight_delay_mlops.database.asyncpg.connect",
            new=AsyncMock(return_value=connection),
        ),
        pytest.raises(RuntimeError, match="database error"),
    ):
        await check_database()

    connection.close.assert_awaited_once()
