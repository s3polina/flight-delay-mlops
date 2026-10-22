import logging

from aio_pika.exceptions import DeliveryError
from faststream.rabbit import (
    ExchangeType,
    QueueType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)

from flight_delay_mlops.config import Settings, settings
from flight_delay_mlops.schemas import TaskMessage

logger = logging.getLogger("flight_delay_mlops")


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
    try:
        await broker.publish(
            task,
            queue=cfg.tasks_queue,
            persist=True,
            mandatory=True,
            message_id=str(task.task_id),
            timeout=timeout,
        )
    except DeliveryError:
        return False
    return True
