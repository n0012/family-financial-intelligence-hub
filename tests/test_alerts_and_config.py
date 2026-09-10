import asyncio
import unittest
from unittest.mock import MagicMock, patch
import os
from fastapi import HTTPException
import config
import alerts
from main import verify_chat_origin


class TestConfig(unittest.TestCase):
    def setUp(self):
        config.clear_secret_cache()

    def test_load_local_config_empty(self):
        with patch.dict(os.environ, {"CONFIG_FILE": "/nonexistent/config.yaml"}):
            cfg = config.load_local_config()
            self.assertIsInstance(cfg, dict)

    def test_resolve_secret_caching_and_fallback(self):
        config.clear_secret_cache()
        with patch.object(config, "secretmanager", None):
            with patch.dict(os.environ, {"TEST_SECRET_ENV": "my-secret-val"}):
                val1 = config.resolve_secret("nonexistent-sm-secret-12345", "TEST_SECRET_ENV")
                self.assertEqual(val1, "my-secret-val")
                # Mutate environment; cached value should still be returned
                with patch.dict(os.environ, {"TEST_SECRET_ENV": "mutated"}):
                    val2 = config.resolve_secret("nonexistent-sm-secret-12345", "TEST_SECRET_ENV")
                    self.assertEqual(val2, "my-secret-val")

    def test_get_decommissioned_ids_default(self):
        with patch.dict(os.environ, {"CONFIG_FILE": "/nonexistent/config.yaml"}, clear=True):
            ids = config.get_decommissioned_account_ids()
            self.assertIsInstance(ids, set)

    def test_get_excluded_institutions_env(self):
        with patch.object(config, "secretmanager", None):
            with patch.dict(os.environ, {"CONFIG_FILE": "/nonexistent/config.yaml", "EXCLUDED_INSTITUTIONS": "testbank, dummycorp"}):
                excluded = config.get_excluded_institutions()
                self.assertIn("testbank", excluded)
                self.assertIn("dummycorp", excluded)



class TestChatAuth(unittest.TestCase):
    def setUp(self):
        config.clear_secret_cache()

    def test_api_key_valid(self):
        with patch("main.resolve_secret", return_value="valid-secret-key"):
            res = verify_chat_origin(authorization=None, x_api_key="valid-secret-key")
            self.assertTrue(res)

    def test_api_key_invalid(self):
        with patch("main.resolve_secret", return_value="valid-secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                verify_chat_origin(authorization=None, x_api_key="wrong-key")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_chat_auth_disabled_non_prod_allowed(self):
        with patch("main.IS_PROD", False):
            with patch.dict(os.environ, {"CHAT_AUTH_DISABLED": "true"}):
                res = verify_chat_origin(authorization=None, x_api_key=None)
                self.assertTrue(res)

    def test_chat_auth_disabled_in_prod_rejected(self):
        with patch("main.IS_PROD", True):
            with patch.dict(os.environ, {"CHAT_AUTH_DISABLED": "true"}):
                with self.assertRaises(HTTPException) as ctx:
                    verify_chat_origin(authorization=None, x_api_key=None)
                self.assertEqual(ctx.exception.status_code, 401)

    @patch("main.id_token.verify_oauth2_token")
    def test_valid_google_chat_bearer_token(self, mock_verify):
        mock_verify.return_value = {
            "email": "chat@system.gserviceaccount.com",
            "iss": "https://accounts.google.com",
        }
        res = verify_chat_origin(authorization="Bearer valid-chat-token", x_api_key=None)
        self.assertTrue(res)

    @patch("main.id_token.verify_oauth2_token")
    def test_rejected_unauthorized_service_account(self, mock_verify):
        mock_verify.return_value = {
            "email": "attacker@evil-project.iam.gserviceaccount.com",
            "iss": "https://accounts.google.com",
        }
        with self.assertRaises(HTTPException) as ctx:
            verify_chat_origin(authorization="Bearer attacker-token", x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Unauthorized caller service account", ctx.exception.detail)

    @patch("main.id_token.verify_oauth2_token")
    def test_rejected_invalid_issuer(self, mock_verify):
        mock_verify.return_value = {
            "email": "chat@system.gserviceaccount.com",
            "iss": "https://untrusted-issuer.com",
        }
        with self.assertRaises(HTTPException) as ctx:
            verify_chat_origin(authorization="Bearer spoofed-token", x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid Google Chat token issuer", ctx.exception.detail)

    def test_missing_credentials_rejected(self):
        with patch("main.IS_PROD", True):
            with self.assertRaises(HTTPException) as ctx:
                verify_chat_origin(authorization=None, x_api_key=None)
            self.assertEqual(ctx.exception.status_code, 401)


class TestAlerts(unittest.TestCase):
    def test_build_chat_card_v2(self):
        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "title": "Subscription Price Hike: TestService",
                "detail": "Charge increased from $10 to $15",
                "suggested_fix": "Audit usage",
            },
            {
                "type": "HELOC_OPPORTUNITY",
                "severity": "INFO",
                "title": "HELOC Cost: $12.50/day",
                "detail": "Balance is $50,000",
                "suggested_fix": "Accelerate paydown",
            }
        ]
        payload = alerts.build_chat_card_v2(mock_alerts)
        self.assertIn("cardsV2", payload)
        self.assertEqual(len(payload["cardsV2"]), 1)
        card = payload["cardsV2"][0]["card"]
        self.assertEqual(card["header"]["title"], "Sage")
        widgets = card["sections"][0]["widgets"]
        self.assertEqual(len(widgets), 2)
        self.assertIn("TestService", widgets[0]["decoratedText"]["text"])

    def test_build_markdown_fallback(self):
        mock_alerts = [
            {
                "title": "Test Alert",
                "detail": "Something happened",
                "suggested_fix": "Do this",
            }
        ]
        md = alerts.build_markdown_fallback(mock_alerts)
        self.assertIn("Test Alert", md)
        self.assertIn("Do this", md)

    def test_execute_alert_scan_with_mock_bq(self):
        mock_bq = MagicMock()
        mock_bq.query.return_value.result.return_value = []
        res = asyncio.run(alerts.execute_alert_scan(
            bq_client=mock_bq,
            project_id="test-project",
            dataset_id="test_dataset",
            webhook_url=None
        ))
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["alert_count"], 0)
        self.assertFalse(res["webhook_dispatched"])


class TestJobCLI(unittest.TestCase):
    def test_run_sync_delegation(self):
        import job
        with patch("monarch_service.execute_sync", return_value={"status": "success", "synced_counts": {}}) as mock_sync:
            res = asyncio.run(job.run_sync(days_back=45))
            mock_sync.assert_called_once_with(days_back=45)
            self.assertEqual(res["status"], "success")

    def test_run_alerts_delegation(self):
        import job
        with patch("alerts.execute_alert_scan", return_value={"status": "success", "alert_count": 0}) as mock_scan:
            res = asyncio.run(job.run_alerts())
            mock_scan.assert_called_once()
            self.assertEqual(res["status"], "success")

    def test_job_main_sync_cli(self):
        import job
        with patch("sys.argv", ["job", "sync", "--days-back", "15"]):
            with patch("job.run_sync", return_value={"status": "success"}) as mock_run_sync:
                exit_code = job.main()
                self.assertEqual(exit_code, 0)
                mock_run_sync.assert_called_once_with(15)

    def test_job_main_alerts_cli(self):
        import job
        with patch("sys.argv", ["job", "alerts"]):
            with patch("job.run_alerts", return_value={"status": "success"}) as mock_run_alerts:
                exit_code = job.main()
                self.assertEqual(exit_code, 0)
                mock_run_alerts.assert_called_once()


class TestChatWorker(unittest.TestCase):
    def test_process_message_dispatches_and_acks(self):
        import chat_worker
        import json

        sample_event = {
            "type": "MESSAGE",
            "message": {"text": "hello advisor", "name": "spaces/AAA/messages/111"},
            "space": {"name": "spaces/AAA"},
        }
        mock_msg = MagicMock()
        mock_msg.message_id = "msg-12345"
        mock_msg.data = json.dumps(sample_event).encode("utf-8")

        loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()

        try:
            with patch("main.google_chat_webhook", return_value={"status": "ok"}) as mock_handler:
                chat_worker.process_message(mock_msg, loop)
                mock_msg.ack.assert_called_once()
                mock_handler.assert_called_once_with(sample_event, is_pubsub_override=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()

    def test_background_chat_worker_lifecycle(self):
        import chat_worker
        from unittest.mock import MagicMock

        mock_subscriber = MagicMock()
        mock_future = MagicMock()
        mock_subscriber.subscribe.return_value = mock_future

        with patch("google.cloud.pubsub_v1.SubscriberClient", return_value=mock_subscriber):
            worker = chat_worker.start_chat_worker_background("test-proj", "test-sub")
            self.assertIsNotNone(worker)
            mock_subscriber.subscribe.assert_called_once()
            worker.stop()
            mock_future.cancel.assert_called_once()
            mock_subscriber.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
