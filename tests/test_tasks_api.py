from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from flight_delay_mlops import main
from flight_delay_mlops.config import Settings, settings
from flight_delay_mlops.main import app
from flight_delay_mlops.storage import TaskStore

FLIGHT = {
    "flight_date": "2013-12-20",
    "sched_dep_time": 1830,
    "sched_arr_time": 2135,
    "carrier": "EV",
    "origin": "EWR",
    "dest": "ATL",
    "distance": 746.0,
}


class FakeTransaction:
    def __init__(self, conn: "FakeConn") -> None:
        self.conn = conn

    async def start(self) -> None:
        self.conn.pending = {}

    async def commit(self) -> None:
        self.conn.db.update(self.conn.pending)
        self.conn.commits += 1

    async def rollback(self) -> None:
        self.conn.pending = {}
        self.conn.rollbacks += 1


class FakeConn:
    def __init__(self) -> None:
        self.db: dict = {}
        self.pending: dict = {}
        self.commits = 0
        self.rollbacks = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def fetchval(self, query: str, task_id, *args):
        if query.strip().startswith("INSERT"):
            if task_id in self.db:
                return None
            self.pending[task_id] = "PENDING"
            return task_id
        return self.db.get(task_id)


class FakePool:
    def __init__(self) -> None:
        self.conn = FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def store() -> TaskStore:
    store = TaskStore(FakePool())
    app.state.store = store
    app.state.broker = object()
    yield store
    del app.state.store, app.state.broker


def mock_broker(monkeypatch: pytest.MonkeyPatch, accept: bool) -> list:
    published: list = []

    async def fake_publish_task(broker, task, cfg=settings, timeout=None) -> bool:
        published.append(task)
        return accept

    monkeypatch.setattr(main, "publish_task", fake_publish_task)
    return published


async def post_task(headers: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/api/v1/tasks", json=FLIGHT, headers=headers or {})


@pytest.mark.asyncio
async def test_task_accepted(store: TaskStore, monkeypatch: pytest.MonkeyPatch) -> None:
    published = mock_broker(monkeypatch, accept=True)

    response = await post_task()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    assert len(published) == 1
    assert str(published[0].task_id) == body["task_id"]
    assert store.pool.conn.db == {published[0].task_id: "PENDING"}


@pytest.mark.asyncio
async def test_overload_returns_503_and_leaves_no_pending(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_broker(monkeypatch, accept=False)

    response = await post_task()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(settings.retry_after_sec)
    assert store.pool.conn.db == {}
    assert store.pool.conn.rollbacks == 1


@pytest.mark.asyncio
async def test_idempotency_key_does_not_create_second_task(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = mock_broker(monkeypatch, accept=True)
    headers = {"Idempotency-Key": "order-42"}

    first = await post_task(headers)
    second = await post_task(headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(published) == 1


@pytest.mark.asyncio
async def test_invalid_flight_is_rejected(store: TaskStore) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/tasks", json={"carrier": "EV"})

    assert response.status_code == 422


def test_retry_after_is_queue_drain_time() -> None:
    cfg = Settings(queue_max_length=100, inference_delay_sec=0.2, worker_concurrency=4)
    assert cfg.retry_after_sec == 5
