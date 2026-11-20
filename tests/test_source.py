import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from flight_delay_mlops import source
from flight_delay_mlops.config import settings
from flight_delay_mlops.schemas import Flight, TaskMessage

FAST = replace(settings, backoff_initial_sec=0.01, backoff_max_sec=0.04)


def task() -> TaskMessage:
    return TaskMessage(
        task_id=uuid4(),
        source="test",
        created_at=datetime.now(UTC),
        flight=Flight(
            flight_date="2013-12-20",
            sched_dep_time=1830,
            sched_arr_time=2135,
            carrier="EV",
            origin="EWR",
            dest="ATL",
            distance=746,
        ),
    )


def test_backoff_grows_and_is_capped() -> None:
    cfg = replace(settings, backoff_initial_sec=0.05, backoff_max_sec=2.0)
    for attempt in range(10):
        cap = min(2.0, 0.05 * 2**attempt)
        delay = source.backoff_delay(attempt, cfg)
        assert cap / 2 <= delay <= cap
    assert source.backoff_delay(20, cfg) <= 2.0


@pytest.mark.asyncio
async def test_rejected_task_is_retried_until_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [False, False, False, True]
    attempts: list[TaskMessage] = []

    async def fake_publish(broker, t, cfg=settings, timeout=None) -> bool:
        attempts.append(t)
        return answers[len(attempts) - 1]

    monkeypatch.setattr(source, "publish_task", fake_publish)
    stats = source.SourceStats()
    t = task()

    ok = await source.publish_with_backoff(None, t, stats, asyncio.Event(), FAST)

    assert ok is True
    assert stats.published == 1
    assert stats.rejected == 3
    assert {a.task_id for a in attempts} == {t.task_id}


@pytest.mark.asyncio
async def test_stop_interrupts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def always_full(broker, t, cfg=settings, timeout=None) -> bool:
        return False

    monkeypatch.setattr(source, "publish_task", always_full)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.1, stop.set)

    ok = await source.publish_with_backoff(
        None, task(), source.SourceStats(), stop, replace(FAST, backoff_max_sec=10)
    )

    assert ok is False


def test_read_tasks_is_lazy_and_deterministic(tmp_path) -> None:
    csv_path = tmp_path / "flights.csv"
    csv_path.write_text(
        "flight_date,sched_dep_time,sched_arr_time,carrier,origin,dest,distance,"
        "arr_delay,is_delayed\n"
        "2013-01-01,515,819,UA,EWR,IAH,1400.0,11,0\n"
        "2013-01-01,529,830,UA,LGA,IAH,1416.0,20,1\n"
    )

    tasks = source.read_tasks(csv_path, "run1")
    first = next(tasks)

    assert first.flight.carrier == "UA"
    again = next(source.read_tasks(csv_path, "run1"))
    assert first.task_id == again.task_id
    assert len(list(tasks)) == 1


@pytest.mark.asyncio
async def test_each_task_has_own_trace_with_wait_spans(
    monkeypatch: pytest.MonkeyPatch, spans
) -> None:
    answers = iter([False, True, True])

    async def fake_publish(broker, t, cfg=settings, timeout=None) -> bool:
        return next(answers)

    monkeypatch.setattr(source, "publish_task", fake_publish)
    stats = source.SourceStats()

    assert await source.publish_with_backoff(None, task(), stats, asyncio.Event(), FAST)
    assert await source.publish_with_backoff(None, task(), stats, asyncio.Event(), FAST)

    finished = spans.get_finished_spans()
    roots = [s for s in finished if s.name == "source.publish_task"]
    waits = [s for s in finished if s.name == "source.wait_for_capacity"]

    assert len(roots) == 2
    assert all(r.parent is None for r in roots)
    assert roots[0].context.trace_id != roots[1].context.trace_id
    assert len(waits) == 1
    assert waits[0].parent.span_id == roots[0].context.span_id
    assert [e.name for e in roots[0].events] == ["broker.rejected"]
    assert stats.wait_total_sec > 0
