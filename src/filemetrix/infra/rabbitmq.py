import json
import os
import logging
import pika
from typing import Any

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/%2F")
JOB_EXCHANGE = os.getenv("JOB_EXCHANGE", "filemetrix.jobs")
JOB_QUEUE = os.getenv("JOB_QUEUE", "filemetrix_job_queue")


def _connection_params():
    return pika.URLParameters(RABBITMQ_URL)


def publish_job(message: dict[str, Any]):
    params = _connection_params()
    conn = pika.BlockingConnection(params)
    try:
        ch = conn.channel()
        ch.exchange_declare(exchange=JOB_EXCHANGE, exchange_type="fanout", durable=True)
        ch.queue_declare(queue=JOB_QUEUE, durable=True)
        ch.queue_bind(queue=JOB_QUEUE, exchange=JOB_EXCHANGE)
        body = json.dumps(message)
        ch.basic_publish(
            exchange=JOB_EXCHANGE,
            routing_key="",
            body=body,
            properties=pika.BasicProperties(delivery_mode=2),
        )
        logging.info(f"Published job to RabbitMQ exchange {JOB_EXCHANGE}")
    finally:
        conn.close()


def consume_jobs(on_message_callback):
    """Start consuming jobs from the queue and call on_message_callback(message_dict, channel, method, properties)
    This is a blocking call intended for worker processes."""
    params = _connection_params()
    conn = pika.BlockingConnection(params)
    ch = conn.channel()
    ch.exchange_declare(exchange=JOB_EXCHANGE, exchange_type="fanout", durable=True)
    ch.queue_declare(queue=JOB_QUEUE, durable=True)
    ch.queue_bind(queue=JOB_QUEUE, exchange=JOB_EXCHANGE)

    def _callback(ch, method, properties, body):
        try:
            msg = json.loads(body)
        except Exception as e:
            logging.exception("Failed to parse job message")
            ch.basic_ack(delivery_tag=method.delivery_tag)
            return
        on_message_callback(msg, ch, method, properties)

    ch.basic_qos(prefetch_count=1)
    ch.basic_consume(queue=JOB_QUEUE, on_message_callback=_callback)
    logging.info("Started consuming jobs")
    try:
        ch.start_consuming()
    finally:
        conn.close()

