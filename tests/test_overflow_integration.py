import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika
import pytest
import pytest_asyncio
from faststream.rabbit import RabbitBroker
from testcontainers.community.rabbitmq import RabbitMqContainer

from flight_delay_mlops import source
from flight_delay_mlops.config import settings
from flight_delay_mlops.messaging import (
    dead_letter_queue,
    declare_topology,
    publish_task,
    tasks_queue,
)
from flight_delay_mlops.schemas import Flight, TaskMessage

pytestmark = pytest.mark.integration

QUEUE_LIMIT = 5


@pytest.fixture(scope="module")
def rabbit_url():
    with RabbitMqContainer("rabbitmq:4-management") as rabbit:
        host = rabbit.get_container_host_ip()
        port = rabbit.get_exposed_port(5672)
        yield f"amqp://guest:guest@{host}:{port}/"


@pytest_asyncio.fixture
async def env(rabbit_url):
    suffix = uuid4().hex[:8]
    cfg = replace(
        settings,
        rabbit_url=rabbit_url,
        tasks_queue=f"it.tasks.{suffix}",
        dead_letter_exchange=f"it.dlx.{suffix}",
        dead_letter_queue=f"it.dlq.{suffix}",
        queue_max_length=QUEUE_LIMIT,
        delivery_limit=2,
        backoff_initial_sec=0.05,
        backoff_max_sec=0.2,
    )
    broker = RabbitBroker(rabbit_url)
    await broker.connect()
    await declare_topology(broker, cfg)
    yield broker, cfg
    await broker.stop()


def make_task() -> TaskMessage:
    return TaskMessage(
        task_id=uuid4(),
        source="it",
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


async def depth(cfg, queue) -> int:
    connection = await aio_pika.connect(cfg.rabbit_url)
    async with connection:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue.name, passive=True)
        return declared.declaration_result.message_count


@pytest.mark.asyncio
async def test_full_queue_rejects_publish_and_source_waits(env) -> None:
    broker, cfg = env

    results = [
        await publish_task(broker, make_task(), cfg) for _ in range(QUEUE_LIMIT * 2)
    ]
    accepted = sum(results)

    assert QUEUE_LIMIT <= accepted <= QUEUE_LIMIT + 1
    assert results[-1] is False
    assert await depth(cfg, tasks_queue(cfg)) == accepted

    stats = source.SourceStats()
    pending = asyncio.create_task(
        source.publish_with_backoff(broker, make_task(), stats, asyncio.Event(), cfg)
    )
    await asyncio.sleep(0.5)
    assert not pending.done()
    assert stats.rejected >= 2

    queue = await broker.declare_queue(tasks_queue(cfg))
    for _ in range(2):
        message = await queue.get(no_ack=False)
        await message.ack()

    assert await asyncio.wait_for(pending, timeout=5) is True
    assert stats.published == 1
    assert await depth(cfg, tasks_queue(cfg)) == accepted - 1


@pytest.mark.asyncio
async def test_poison_message_goes_to_dlq(env) -> None:
    broker, cfg = env
    await publish_task(broker, make_task(), cfg)

    queue = await broker.declare_queue(tasks_queue(cfg))
    message = await queue.get(no_ack=False)
    await message.reject(requeue=False)

    await asyncio.sleep(0.5)
    assert await depth(cfg, tasks_queue(cfg)) == 0
    assert await depth(cfg, dead_letter_queue(cfg)) == 1


@pytest.mark.asyncio
async def test_failing_message_goes_to_dlq_after_delivery_limit(env) -> None:
    broker, cfg = env
    await publish_task(broker, make_task(), cfg)
    queue = await broker.declare_queue(tasks_queue(cfg))

    deliveries = 0
    for _ in range(cfg.delivery_limit + 2):
        message = await queue.get(no_ack=False, fail=False)
        if message is None:
            break
        deliveries += 1
        await message.reject(requeue=True)
        await asyncio.sleep(0.2)

    await asyncio.sleep(0.5)
    assert deliveries == cfg.delivery_limit + 1
    assert await depth(cfg, tasks_queue(cfg)) == 0
    assert await depth(cfg, dead_letter_queue(cfg)) == 1
