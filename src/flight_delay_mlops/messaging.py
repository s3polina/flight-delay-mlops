import logging
from typing import Any

from aio_pika.exceptions import DeliveryError
from faststream import BaseMiddleware
from faststream.message import StreamMessage
from faststream.rabbit import (
    ExchangeType,
    QueueType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)
from faststream.rabbit.opentelemetry import RabbitTelemetryMiddleware
from opentelemetry.metrics import NoOpMeterProvider

from flight_delay_mlops.config import Settings, settings
from flight_delay_mlops.logs import bind_task_id
from flight_delay_mlops.schemas import TaskMessage

TASK_ID_HEADER = "task_id"


class TaskContextMiddleware(BaseMiddleware):
    async def consume_scope(self, call_next: Any, msg: StreamMessage[Any]) -> Any:
        task_id = msg.headers.get(TASK_ID_HEADER) or msg.message_id
        with bind_task_id(task_id):
            return await call_next(msg)


def make_broker(cfg: Settings = settings, **kwargs: Any) -> RabbitBroker:
    return RabbitBroker(
        cfg.rabbit_url,
        middlewares=(
            RabbitTelemetryMiddleware(meter_provider=NoOpMeterProvider()),
            TaskContextMiddleware,
        ),
        logger=logging.getLogger("flight_delay_mlops.broker"),
        log_level=logging.DEBUG,
        **kwargs,
    )


def tasks_queue(cfg: Settings = settings) -> RabbitQueue:
    arguments: dict[str, int | str] = {
        "x-dead-letter-exchange": cfg.dead_letter_exchange,
        "x-dead-letter-routing-key": cfg.dead_letter_queue,
        "x-delivery-limit": cfg.delivery_limit,
    }
    if cfg.queue_max_length > 0:
        arguments["x-max-length"] = cfg.queue_max_length
        arguments["x-overflow"] = "reject-publish"

    return RabbitQueue(
        cfg.tasks_queue,
        durable=True,
        queue_type=QueueType.QUORUM,
        arguments=arguments,
    )


def dead_letter_exchange(cfg: Settings = settings) -> RabbitExchange:
    return RabbitExchange(
        cfg.dead_letter_exchange, type=ExchangeType.DIRECT, durable=True
    )


def dead_letter_queue(cfg: Settings = settings) -> RabbitQueue:
    return RabbitQueue(
        cfg.dead_letter_queue,
        durable=True,
        queue_type=QueueType.QUORUM,
        routing_key=cfg.dead_letter_queue,
    )


async def declare_topology(broker: RabbitBroker, cfg: Settings = settings) -> None:
    exchange = await broker.declare_exchange(dead_letter_exchange(cfg))
    dlq = await broker.declare_queue(dead_letter_queue(cfg))
    await dlq.bind(exchange, routing_key=cfg.dead_letter_queue)
    await broker.declare_queue(tasks_queue(cfg))


async def publish_task(
    broker: RabbitBroker,
    task: TaskMessage,
    cfg: Settings = settings,
    timeout: float | None = None,
) -> bool:
    task_id = str(task.task_id)
    try:
        await broker.publish(
            task,
            queue=cfg.tasks_queue,
            persist=True,
            mandatory=True,
            message_id=task_id,
            correlation_id=task_id,
            headers={TASK_ID_HEADER: task_id},
            timeout=timeout,
        )
    except DeliveryError:
        return False
    return True
