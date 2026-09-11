import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app import alerts, config
from app.main import verify_chat_origin


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
            with patch.dict(
                os.environ, {"CONFIG_FILE": "/nonexistent/config.yaml", "EXCLUDED_INSTITUTIONS": "testbank, dummycorp"}
            ):
                excluded = config.get_excluded_institutions()
                self.assertIn("testbank", excluded)
                self.assertIn("dummycorp", excluded)


class TestChatAuth(unittest.TestCase):
    def setUp(self):
        config.clear_secret_cache()

    def test_api_key_valid(self):
        with patch("app.main.resolve_secret", return_value="valid-secret-key"):
            res = verify_chat_origin(authorization=None, x_api_key="valid-secret-key")
            self.assertTrue(res)

    def test_api_key_invalid(self):
        with patch("app.main.resolve_secret", return_value="valid-secret-key"):
            with self.assertRaises(HTTPException) as ctx:
                verify_chat_origin(authorization=None, x_api_key="wrong-key")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_chat_auth_disabled_non_prod_allowed(self):
        with patch("app.main.IS_PROD", False):
            with patch.dict(os.environ, {"CHAT_AUTH_DISABLED": "true"}):
                res = verify_chat_origin(authorization=None, x_api_key=None)
                self.assertTrue(res)

    def test_chat_auth_disabled_in_prod_rejected(self):
        with patch("app.main.IS_PROD", True):
            with patch.dict(os.environ, {"CHAT_AUTH_DISABLED": "true"}):
                with self.assertRaises(HTTPException) as ctx:
                    verify_chat_origin(authorization=None, x_api_key=None)
                self.assertEqual(ctx.exception.status_code, 401)

    @patch("app.main.id_token.verify_oauth2_token")
    def test_valid_google_chat_bearer_token(self, mock_verify):
        mock_verify.return_value = {
            "email": "chat@system.gserviceaccount.com",
            "iss": "https://accounts.google.com",
        }
        res = verify_chat_origin(authorization="Bearer valid-chat-token", x_api_key=None)
        self.assertTrue(res)

    @patch("app.main.id_token.verify_oauth2_token")
    def test_rejected_unauthorized_service_account(self, mock_verify):
        mock_verify.return_value = {
            "email": "attacker@evil-project.iam.gserviceaccount.com",
            "iss": "https://accounts.google.com",
        }
        with self.assertRaises(HTTPException) as ctx:
            verify_chat_origin(authorization="Bearer attacker-token", x_api_key=None)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Unauthorized caller service account", ctx.exception.detail)

    @patch("app.main.id_token.verify_oauth2_token")
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
        with patch("app.main.IS_PROD", True):
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
            },
        ]
        payload = alerts.build_chat_card_v2(mock_alerts)
        self.assertIn("cardsV2", payload)
        self.assertEqual(len(payload["cardsV2"]), 1)
        card = payload["cardsV2"][0]["card"]
        self.assertEqual(card["header"]["title"], "FinSage")
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
        res = asyncio.run(
            alerts.execute_alert_scan(
                bq_client=mock_bq, project_id="test-project", dataset_id="test_dataset", webhook_url=None
            )
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["alert_count"], 0)
        self.assertFalse(res["webhook_dispatched"])

    def test_check_subscription_price_creep(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.merchant = "CloudStream"
        mock_row.latest_charge = 19.99
        mock_row.prior_charge = 15.99
        mock_row.price_increase_amount = 4.00
        mock_row.pct_increase = 25.0
        mock_row.estimated_annual_cost = 239.88
        mock_row.effective_date = "2026-09-01"
        mock_bq.query.return_value.result.return_value = [mock_row]

        found = alerts.check_subscription_price_creep(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "PRICE_CREEP")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "price_creep:cloudstream")
        self.assertIn("+25.0%", a["title"])
        self.assertIn("$15.99 to $19.99", a["detail"])
        self.assertIn("2026-09-01", a["detail"])

    def test_check_subscription_overlap(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.domain_name = "VIDEO_STREAMING"
        mock_row.functional_domain = "VIDEO_STREAMING"
        mock_row.active_service_count = 3
        mock_row.combined_annual_cost = 540.0
        mock_row.combined_monthly_cost = 45.0
        mock_row.active_services = "StreamA, StreamB, StreamC"
        mock_bq.query.return_value.result.return_value = [mock_row]

        found = alerts.check_subscription_overlap(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "SUBSCRIPTION_OVERLAP")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "overlap:video_streaming")
        self.assertIn("Video Streaming", a["title"])
        self.assertIn("StreamA, StreamB, StreamC", a["detail"])
        self.assertIn("$45.00/mo", a["detail"])

    def test_check_micro_transaction_leakage(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.merchant = "Starbucks"
        mock_row.category_name = "Coffee Shops"
        mock_row.frequency_90d = 42
        mock_row.avg_ticket = 6.50
        mock_row.total_spend_90d = 273.00
        mock_row.annualized_run_rate = 1092.00
        mock_bq.query.return_value.result.return_value = [mock_row]

        found = alerts.check_micro_transaction_leakage(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "MICRO_TRANSACTION_LEAKAGE")
        self.assertEqual(a["alert_key"], "micro:starbucks")
        self.assertIn("42 transactions", a["detail"])
        self.assertIn("$1,092.00/year", a["suggested_fix"])

    def test_extract_budget_caps(self):
        mems = [
            "Capped dining spend at $450/month with excess cash directed to HELOC payoff",
            "$600 monthly limit on groceries",
        ]
        caps = alerts.extract_budget_caps(mems)
        self.assertEqual(caps.get("dining"), 450.0)
        self.assertEqual(caps.get("groceries"), 600.0)

    def test_check_memory_budget_limits_exceeded(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.current_month = "2026-09"
        mock_row.current_month_dining = 512.40
        mock_bq.query.return_value.result.return_value = [mock_row]

        with patch("app.memory_service.retrieve_user_memories", return_value=["Capped dining spend at $450/month"]):
            found = alerts.check_memory_budget_limits(mock_bq, "proj", "ds", "test@example.com")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["type"], "BUDGET_CAP_EXCEEDED")
            self.assertEqual(found[0]["severity"], "WARNING")
            self.assertEqual(found[0]["alert_key"], "budget_cap:dining:2026-09")
            self.assertIn("$512.40", found[0]["detail"])

    def test_check_memory_budget_limits_pacing(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.current_month = "2026-09"
        mock_row.current_month_dining = 400.00  # 88.8% of $450 cap
        mock_bq.query.return_value.result.return_value = [mock_row]

        with patch("app.memory_service.retrieve_user_memories", return_value=["Capped dining spend at $450/month"]):
            found = alerts.check_memory_budget_limits(mock_bq, "proj", "ds", "test@example.com")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["type"], "BUDGET_CAP_PACING")
            self.assertEqual(found[0]["severity"], "INFO")
            self.assertEqual(found[0]["alert_key"], "budget_pacing:dining:2026-09")

    def test_check_memory_budget_limits_groceries(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.current_month = "2026-09"
        mock_row.current_month_groceries = 850.00
        mock_bq.query.return_value.result.return_value = [mock_row]

        with patch(
            "app.memory_service.retrieve_user_memories", return_value=["Budget for groceries is $700 per month"]
        ):
            found = alerts.check_memory_budget_limits(mock_bq, "proj", "ds", "test@example.com")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["type"], "BUDGET_CAP_EXCEEDED")
            self.assertEqual(found[0]["severity"], "WARNING")
            self.assertEqual(found[0]["alert_key"], "budget_cap:groceries:2026-09")
            self.assertIn("$850.00", found[0]["detail"])

    def test_get_active_suppressions(self):
        mock_bq = MagicMock()
        r1 = MagicMock()
        r1.alert_key = "overlap:streaming"
        r2 = MagicMock()
        r2.alert_key = "micro:starbucks"
        mock_bq.query.return_value.result.return_value = [r1, r2]

        suppressed = alerts.get_active_suppressions(mock_bq, "proj", "ds")
        self.assertEqual(suppressed, {"overlap:streaming", "micro:starbucks"})

    def test_filter_suppressed_alerts(self):
        raw = [
            {"type": "SUBSCRIPTION_OVERLAP", "alert_key": "overlap:streaming"},
            {"type": "PRICE_CREEP", "alert_key": "price_creep:netflix"},
        ]
        suppressed = {"overlap:streaming"}
        filtered = alerts.filter_suppressed_alerts(raw, suppressed)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["alert_key"], "price_creep:netflix")

    def test_suppress_alert(self):
        mock_bq = MagicMock()
        success = alerts.suppress_alert(
            bq=mock_bq,
            project_id="proj",
            dataset_id="ds",
            alert_key="price_creep:netflix",
            alert_type="PRICE_CREEP",
            days=14,
            reason="User snoozed",
        )
        self.assertTrue(success)
        mock_bq.query.assert_called_once()

    def test_build_chat_card_v2_with_snooze_buttons(self):
        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "alert_key": "price_creep:netflix",
                "title": "Subscription Price Hike: Netflix",
                "detail": "Increased from $15.49 to $19.99",
                "suggested_fix": "Audit usage",
            }
        ]
        payload = alerts.build_chat_card_v2(mock_alerts)
        card = payload["cardsV2"][0]["card"]
        widgets = card["sections"][0]["widgets"]
        self.assertEqual(len(widgets), 2)
        button_widget = widgets[1]
        self.assertIn("buttonList", button_widget)
        btn = button_widget["buttonList"]["buttons"][0]
        self.assertEqual(btn["text"], "💤 Snooze 7 Days")
        action_params = btn["onClick"]["action"]["parameters"]
        params_dict = {p["key"]: p["value"] for p in action_params}
        self.assertEqual(params_dict["action"], "snooze_alert")
        self.assertEqual(params_dict["alert_key"], "price_creep:netflix")

    def test_build_snooze_success_card(self):
        card_payload = alerts.build_snooze_success_card("overlap:streaming", "SUBSCRIPTION_OVERLAP", 7)
        self.assertEqual(card_payload["cardId"], "snoozeSuccess_overlap:streaming")
        card = card_payload["card"]
        self.assertEqual(card["header"]["subtitle"], "Alert Snoozed")
        text = card["sections"][0]["widgets"][0]["decoratedText"]["text"]
        self.assertIn("Subscription Overlap", text)
        self.assertIn("7 days", text)

    def test_snooze_spend_alert_tool(self):
        with patch("app.alerts.suppress_alert", return_value=True) as mock_suppress:
            with patch("app.alerts.bigquery.Client"):
                res = alerts.snooze_spend_alert("Netflix Price Hike", days=14)
                self.assertIn("Successfully snoozed", res)
                mock_suppress.assert_called_once()
                args, kwargs = mock_suppress.call_args
                self.assertEqual(kwargs.get("alert_key") or args[3], "netflix_price_hike")
                self.assertEqual(kwargs.get("days") or args[5], 14)

    def test_main_card_clicked_snooze_alert(self):
        from app import main

        card_event = {
            "type": "CARD_CLICKED",
            "action": {
                "actionMethodName": "snooze_alert",
                "parameters": [
                    {"key": "alert_key", "value": "price_creep:hulu"},
                    {"key": "alert_type", "value": "PRICE_CREEP"},
                    {"key": "days", "value": "7"},
                ],
            },
            "user": {"email": "user@example.com"},
        }
        with patch("app.main.suppress_alert", return_value=True) as mock_suppress:
            res = asyncio.run(main.google_chat_webhook(card_event))
            self.assertIn("cardsV2", res)
            self.assertIn("Price Creep", res.get("text", ""))
            mock_suppress.assert_called_once()


class TestJobCLI(unittest.TestCase):
    def test_run_sync_delegation(self):
        from app import job

        with patch(
            "app.monarch_service.execute_sync", return_value={"status": "success", "synced_counts": {}}
        ) as mock_sync:
            res = asyncio.run(job.run_sync(days_back=45))
            mock_sync.assert_called_once_with(days_back=45)
            self.assertEqual(res["status"], "success")

    def test_run_alerts_delegation(self):
        from app import job

        with patch("app.alerts.execute_alert_scan", return_value={"status": "success", "alert_count": 0}) as mock_scan:
            res = asyncio.run(job.run_alerts())
            mock_scan.assert_called_once()
            self.assertEqual(res["status"], "success")

    def test_job_main_sync_cli(self):
        from app import job

        with patch("sys.argv", ["job", "sync", "--days-back", "15"]):
            with patch("app.job.run_sync", return_value={"status": "success"}) as mock_run_sync:
                exit_code = job.main()
                self.assertEqual(exit_code, 0)
                mock_run_sync.assert_called_once_with(15)

    def test_job_main_alerts_cli(self):
        from app import job

        with patch("sys.argv", ["job", "alerts"]):
            with patch("app.job.run_alerts", return_value={"status": "success"}) as mock_run_alerts:
                exit_code = job.main()
                self.assertEqual(exit_code, 0)
                mock_run_alerts.assert_called_once()


class TestChatWorker(unittest.TestCase):
    def test_process_message_dispatches_and_acks(self):
        import json

        from app import chat_worker

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
            with patch("app.main.google_chat_webhook", return_value={"status": "ok"}) as mock_handler:
                chat_worker.process_message(mock_msg, loop)
                mock_msg.ack.assert_called_once()
                mock_handler.assert_called_once_with(sample_event, is_pubsub_override=True)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)
            loop.close()

    def test_background_chat_worker_lifecycle(self):
        from unittest.mock import MagicMock

        from app import chat_worker

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
