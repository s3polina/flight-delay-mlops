import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           UUID PRIMARY KEY,
    status            TEXT NOT NULL CHECK (status IN ('PENDING', 'DONE')),
    source            TEXT NOT NULL,
    payload           JSONB NOT NULL,
    delay_probability DOUBLE PRECISION,
    is_delayed        BOOLEAN,
    model_version     TEXT,
    worker            TEXT,
    created_at        TIMESTAMPTZ NOT NULL,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tasks_source_idx ON tasks (source);
"""

SCHEMA_LOCK_ID = 465572


@dataclass(frozen=True)
class TaskResult:
    task_id: UUID
    source: str
    payload: dict[str, Any]
    delay_probability: float
    is_delayed: bool
    model_version: str
    worker: str
    created_at: datetime
    started_at: datetime
    finished_at: datetime


class TaskStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, max_size: int = 10) -> "TaskStore":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=max_size)
        store = cls(pool)
        await store.init_schema()
        return store

    async def close(self) -> None:
        await self.pool.close()

    async def init_schema(self) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_ID)
            await conn.execute(SCHEMA)

    async def create_pending(
        self,
        task_id: UUID,
        payload: dict[str, Any],
        created_at: datetime,
        publish: Callable[[], Awaitable[bool]],
    ) -> tuple[bool, str]:
        async with self.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                inserted = await conn.fetchval(
                    """
                    INSERT INTO tasks (task_id, status, source, payload, created_at)
                    VALUES ($1, 'PENDING', 'api', $2::jsonb, $3)
                    ON CONFLICT (task_id) DO NOTHING
                    RETURNING task_id
                    """,
                    task_id,
                    json.dumps(payload),
                    created_at,
                )
                if inserted is None:
                    await tx.rollback()
                    status = await conn.fetchval(
                        "SELECT status FROM tasks WHERE task_id = $1", task_id
                    )
                    return True, status

                if not await publish():
                    await tx.rollback()
                    return False, ""
            except BaseException:
                await tx.rollback()
                raise
            await tx.commit()
            return True, "PENDING"

    async def save_result(self, result: TaskResult) -> bool:
        row = await self.pool.fetchval(
            """
            INSERT INTO tasks (
                task_id, status, source, payload, delay_probability, is_delayed,
                model_version, worker, created_at, started_at, finished_at
            )
            VALUES ($1, 'DONE', $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (task_id) DO UPDATE SET
                status            = 'DONE',
                delay_probability = EXCLUDED.delay_probability,
                is_delayed        = EXCLUDED.is_delayed,
                model_version     = EXCLUDED.model_version,
                worker            = EXCLUDED.worker,
                started_at        = EXCLUDED.started_at,
                finished_at       = EXCLUDED.finished_at
            WHERE tasks.status = 'PENDING'
            RETURNING task_id
            """,
            result.task_id,
            result.source,
            json.dumps(result.payload),
            result.delay_probability,
            result.is_delayed,
            result.model_version,
            result.worker,
            result.created_at,
            result.started_at,
            result.finished_at,
        )
        return row is not None

    async def get(self, task_id: UUID) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT task_id, status, delay_probability, is_delayed, model_version,
                   created_at, finished_at
            FROM tasks WHERE task_id = $1
            """,
            task_id,
        )
        return dict(row) if row else None

    async def count_pending(self) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM tasks WHERE status = 'PENDING'"
        )
