#!/usr/bin/env python
"""
Cloud Pub/Sub Pull Subscriber Worker for Google Chat.

Enables 100% zero-ingress Google Chat bot operation:
- No HTTP listener
- No public IP / URL
- No incoming network ports
Pulls Chat event messages from Google Cloud Pub/Sub, processes them with the Advisor,
and responds asynchronously via the Google Chat REST API.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import threading

from google.cloud import pubsub_v1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("monarch-gemini.chat_worker")


def process_message(message: pubsub_v1.subscriber.message.Message, loop: asyncio.AbstractEventLoop):
    """Callback invoked by Pub/Sub client when a message is received."""
    from app.main import google_chat_webhook

    try:
        raw_data = message.data.decode("utf-8")
        logger.info(f"Received Pub/Sub message ID: {message.message_id}")
        payload = json.loads(raw_data)

        # Dispatch to google_chat_webhook with is_pubsub_override=True
        future = asyncio.run_coroutine_threadsafe(
            google_chat_webhook(payload, is_pubsub_override=True),
            loop,
        )
        res = future.result(timeout=60)
        logger.info(f"Processed message {message.message_id}: {res}")
        message.ack()
    except Exception as e:
        logger.exception(f"Error processing message {message.message_id}: {e}")
        # Acknowledge to avoid poison-pill retry loops, or nack if transient
        message.ack()


def run_worker(project_id: str, subscription_name: str):
    """Runs the Pub/Sub streaming pull subscriber."""
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(project_id, subscription_name)

    logger.info(f"Starting Google Chat Pub/Sub Pull Worker on {subscription_path} (Zero Ingress Mode)")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    flow_control = pubsub_v1.types.FlowControl(max_messages=10)
    streaming_pull_future = subscriber.subscribe(
        subscription_path,
        callback=lambda msg: process_message(msg, loop),
        flow_control=flow_control,
    )

    stop_event = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        streaming_pull_future.cancel()
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        streaming_pull_future.cancel()
        try:
            streaming_pull_future.result(timeout=5)
        except Exception:
            pass
        subscriber.close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=3)
        logger.info("Worker stopped successfully.")


class BackgroundChatWorker:
    """Manages an embedded Pub/Sub streaming pull subscriber running in a background thread."""

    def __init__(self, project_id: str, subscription_name: str = "monarch-chat-sub"):
        self.project_id = project_id
        self.subscription_name = subscription_name
        self._subscriber = None
        self._streaming_pull_future = None
        self._loop = None
        self._loop_thread = None

    def start(self):
        try:
            self._subscriber = pubsub_v1.SubscriberClient()
            subscription_path = self._subscriber.subscription_path(self.project_id, self.subscription_name)
            logger.info(f"Starting embedded Google Chat Pub/Sub Pull Worker on {subscription_path} (Zero Ingress Mode)")

            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._loop_thread.start()

            flow_control = pubsub_v1.types.FlowControl(max_messages=10)
            self._streaming_pull_future = self._subscriber.subscribe(
                subscription_path,
                callback=lambda msg: process_message(msg, self._loop),
                flow_control=flow_control,
            )
            logger.info("Embedded Google Chat Pub/Sub Pull Worker started successfully.")
        except Exception as e:
            logger.error(f"Failed to start embedded Pub/Sub Chat Worker: {e}")

    def stop(self):
        logger.info("Stopping embedded Google Chat Pub/Sub Pull Worker...")
        if self._streaming_pull_future:
            self._streaming_pull_future.cancel()
            try:
                self._streaming_pull_future.result(timeout=5)
            except Exception:
                pass
        if self._subscriber:
            self._subscriber.close()
        if self._loop and self._loop_thread:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=3)
        logger.info("Embedded Google Chat Pub/Sub Pull Worker stopped.")


def start_chat_worker_background(
    project_id: str, subscription_name: str = "monarch-chat-sub"
) -> BackgroundChatWorker | None:
    """Helper to start the chat worker daemon in the background."""
    worker = BackgroundChatWorker(project_id, subscription_name)
    worker.start()
    return worker


def main() -> int:
    parser = argparse.ArgumentParser(prog="chat_worker", description="Zero-ingress Google Chat Pub/Sub Pull Worker")
    parser.add_argument(
        "--project",
        default=os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "family-finance-hub"),
        help="GCP Project ID",
    )
    parser.add_argument(
        "--subscription",
        default=os.getenv("CHAT_SUBSCRIPTION", "monarch-chat-sub"),
        help="Pub/Sub subscription name",
    )
    args = parser.parse_args()

    run_worker(args.project, args.subscription)
    return 0


if __name__ == "__main__":
    sys.exit(main())
