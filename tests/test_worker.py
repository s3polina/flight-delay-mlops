import asyncio
import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from faststream.rabbit import TestRabbitBroker
from opentelemetry.trace import StatusCode

from flight_delay_mlops import worker
from flight_delay_mlops.config import settings
from flight_delay_mlops.logs import ContextFilter
from flight_delay_mlops.messaging import publish_task
from flight_delay_mlops.schemas import TaskMessage
from flight_delay_mlops.storage import TaskResult
from flight_delay_mlops.telemetry import inference_buckets


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


class ContextRecords(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(ContextFilter("flight-worker"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def context_records():
    handler = ContextRecords()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler.records
    root.removeHandler(handler)


def span_named(spans, suffix: str):
    return next(s for s in spans.get_finished_spans() if s.name.endswith(suffix))


@pytest.mark.asyncio
async def test_inference_and_db_spans_belong_to_task_trace(
    worker_state, make_task, spans
) -> None:
    async with TestRabbitBroker(worker.broker) as br:
        await br.publish(make_task(), queue=settings.tasks_queue)

    process = span_named(spans, " process")
    inference = span_named(spans, "model.inference")
    db = span_named(spans, "db.save_result")

    assert inference.parent.span_id == process.context.span_id
    assert db.parent.span_id == process.context.span_id
    assert inference.context.trace_id == process.context.trace_id
    assert inference.attributes["model.alias"] == "champion"
    assert inference.attributes["model.version"] == "2"


@pytest.mark.asyncio
async def test_task_id_and_trace_id_are_added_to_handler_logs(
    worker_state, make_task, context_records
) -> None:
    task = TaskMessage.model_validate(make_task())

    async with TestRabbitBroker(worker.broker) as br:
        assert await publish_task(br, task) is True

    worker_logs = [r for r in context_records if r.name == "flight_delay_mlops.worker"]
    assert worker_logs
    assert {r.task_id for r in worker_logs} == {str(task.task_id)}
    assert all(r.trace_id for r in worker_logs)


@pytest.mark.asyncio
async def test_last_failed_attempt_is_logged_and_traced_as_dlq(
    worker_state, make_task, spans, caplog
) -> None:
    worker_state.store = FakeStore(fail=True)
    limit = worker.settings.delivery_limit

    async with TestRabbitBroker(worker.broker) as br:
        with contextlib.suppress(ConnectionError):
            await br.publish(
                make_task(),
                queue=settings.tasks_queue,
                headers={"x-delivery-count": limit},
            )

    messages = [r.getMessage() for r in caplog.records]
    assert "task redelivered" in messages
    assert "task processing failed" in messages
    assert "task sent to DLQ: delivery limit reached" in messages
    failed = next(
        r for r in caplog.records if r.getMessage() == "task processing failed"
    )
    assert failed.exc_info is not None
    assert failed.attempt == limit + 1

    process = span_named(spans, " process")
    assert process.status.status_code is StatusCode.ERROR
    assert process.attributes["messaging.delivery.attempt"] == limit + 1
    event_names = {e.name for e in process.events}
    assert {"exception", "redelivery", "dead_lettered"} <= event_names


def test_inference_buckets_surround_configured_delay() -> None:
    buckets = inference_buckets(0.2)

    assert buckets == sorted(buckets)
    assert buckets[0] < 0.2 < buckets[-1]
    assert sum(1 for b in buckets if 0.2 <= b <= 0.3) >= 3
