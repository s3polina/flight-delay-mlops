import asyncio
import contextlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from faststream.rabbit import TestRabbitBroker

from flight_delay_mlops import worker
from flight_delay_mlops.config import settings
from flight_delay_mlops.storage import TaskResult


class FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.results: dict = {}
        self.calls = 0
        self.fail = fail

    async def save_result(self, result: TaskResult) -> bool:
        self.calls += 1
        if self.fail:
            raise ConnectionError("database is down")
        if result.task_id in self.results:
            return False
        self.results[result.task_id] = result
        return True


@pytest.fixture
def worker_state(fake_model, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(worker, "settings", replace(settings, inference_delay_sec=0))
    store = FakeStore()
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(
        worker,
        "state",
        worker.WorkerState(model=fake_model, store=store, executor=executor),
    )
    yield worker.state
    executor.shutdown()


@pytest.mark.asyncio
async def test_valid_task_is_processed_and_saved(worker_state, make_task) -> None:
    task = make_task()

    async with TestRabbitBroker(worker.broker) as br:
        await br.publish(task, queue=settings.tasks_queue)

        worker.handle_task.mock.assert_called_once()

    assert worker_state.processed == 1
    (result,) = worker_state.store.results.values()
    assert str(result.task_id) == task["task_id"]
    assert result.delay_probability == pytest.approx(0.7)
    assert result.is_delayed is True
    assert result.model_version == "2"
    df = worker_state.model.model.calls[0]
    assert df.loc[0, "carrier"] == "EV"


@pytest.mark.asyncio
async def test_redelivery_does_not_duplicate_result(worker_state, make_task) -> None:
    task = make_task()

    async with TestRabbitBroker(worker.broker) as br:
        await br.publish(task, queue=settings.tasks_queue)
        await br.publish(task, queue=settings.tasks_queue)

    assert len(worker_state.store.results) == 1
    assert worker_state.processed == 1
    assert worker_state.duplicates == 1


@pytest.mark.asyncio
async def test_invalid_message_is_rejected_without_processing(worker_state) -> None:
    async with TestRabbitBroker(worker.broker) as br:
        await br.publish({"task_id": "not-a-uuid"}, queue=settings.tasks_queue)

    assert worker_state.rejected == 1
    assert worker_state.store.calls == 0
    assert worker_state.model.model.calls == []


@pytest.mark.asyncio
async def test_processing_error_is_not_acked(worker_state, make_task) -> None:
    worker_state.store = FakeStore(fail=True)

    async with TestRabbitBroker(worker.broker) as br:
        with contextlib.suppress(ConnectionError):
            await br.publish(make_task(), queue=settings.tasks_queue)

    assert worker_state.failed == 1
    assert worker_state.processed == 0
    assert worker_state.in_flight == 0


@pytest.mark.asyncio
async def test_inference_does_not_block_event_loop(
    worker_state, make_task, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "settings", replace(settings, inference_delay_sec=0.5))
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    async with TestRabbitBroker(worker.broker) as br:
        ticker_task = asyncio.create_task(ticker())
        started = time.perf_counter()
        await br.publish(make_task(), queue=settings.tasks_queue)
        elapsed = time.perf_counter() - started
        ticker_task.cancel()

    assert elapsed >= 0.5
    assert ticks >= 5
    assert worker.health_payload()["processed"] == 1
