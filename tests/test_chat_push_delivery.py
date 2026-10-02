"""
Pub/Sub push delivery: the service scales to zero with CPU throttled outside requests,
so slow Chat answers must be posted before the push request is acknowledged.
"""

import asyncio
import base64
import json
import time
import unittest
from unittest.mock import patch

from app import main


def _chat_event(text: str) -> dict:
    return {
        "type": "MESSAGE",
        "message": {
            "name": "spaces/test/messages/msg123.msg123",
            "text": text,
            "sender": {"displayName": "FinSage User", "email": "user@example.com"},
        },
        "space": {"name": "spaces/test"},
    }


def _push_envelope(event: dict) -> dict:
    data = base64.b64encode(json.dumps(event).encode("utf-8")).decode("utf-8")
    return {"message": {"data": data, "messageId": "1"}, "subscription": "projects/p/subscriptions/monarch-chat-push"}


def _slow_brain(*_args, **_kwargs):
    time.sleep(0.3)
    return {"answer": "Your top subscription is Netflix.", "sql": None, "suggestions": []}


class TestChatPushDelivery(unittest.TestCase):
    def setUp(self):
        self.posted: list[str] = []
        patches = [
            patch.object(main, "CHAT_SYNC_BUDGET_SECONDS", 0.05),
            patch("app.main.ask_gemini_brain", side_effect=_slow_brain),
            patch("app.main.get_session_history", return_value=[]),
            patch("app.main.save_session_history"),
            patch("app.main.post_to_chat_thread", side_effect=lambda text, *a, **k: self.posted.append(text)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_push_delivery_posts_final_answer_before_returning(self):
        resp = asyncio.run(main.google_chat_webhook(_push_envelope(_chat_event("what are my top subscriptions?"))))

        self.assertEqual(resp, {"status": "ok"})
        self.assertEqual(len(self.posted), 2)
        self.assertIn("Analyzing", self.posted[0])
        self.assertIn("Netflix", self.posted[1])

    def test_lifespan_does_not_start_pull_worker_on_cloud_run_by_default(self):
        async def run_lifespan():
            async with main.lifespan(main.app):
                pass

        with (
            patch.dict("os.environ", {"K_SERVICE": "monarch-gemini-wrapper"}, clear=False),
            patch("app.chat_worker.start_chat_worker_background") as start_worker,
        ):
            asyncio.run(run_lifespan())
            start_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
