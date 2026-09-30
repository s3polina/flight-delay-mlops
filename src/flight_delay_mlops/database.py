import os

import asyncpg

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/flight_delay",
)


async def check_database() -> str:
    connection = await asyncpg.connect(DATABASE_URL)

    try:
        database_version = await connection.fetchval("SELECT version()")
        return database_version
    finally:
        await connection.close()
