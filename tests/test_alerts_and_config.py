import asyncio
import os
import unittest
from types import SimpleNamespace
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

    @patch.dict(os.environ, {"CHAT_AUDIENCE": "https://chat-service.a.run.app"})
    @patch("app.main.id_token.verify_oauth2_token")
    def test_valid_google_chat_bearer_token(self, mock_verify):
        mock_verify.return_value = {
            "email": "chat@system.gserviceaccount.com",
            "iss": "https://accounts.google.com",
        }
        res = verify_chat_origin(authorization="Bearer valid-chat-token", x_api_key=None)
        self.assertTrue(res)

    @patch.dict(os.environ, {"CHAT_AUDIENCE": "https://chat-service.a.run.app"})
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

    @patch.dict(os.environ, {"CHAT_AUDIENCE": "https://chat-service.a.run.app"})
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

    def test_bearer_token_missing_chat_audience_fails_closed(self):
        with patch.dict(os.environ, {"CHAT_AUDIENCE": "", "CLOUD_RUN_URL": ""}, clear=True):
            with patch("app.main.resolve_secret", return_value=None):
                with self.assertRaises(HTTPException) as ctx:
                    verify_chat_origin(authorization="Bearer any-token", x_api_key=None)
                self.assertEqual(ctx.exception.status_code, 401)
                self.assertIn("Chat audience configuration missing", ctx.exception.detail)

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

    # BigQuery Row objects raise AttributeError for absent columns. SimpleNamespace does
    # too, so getattr() defaults in the alert checks are actually exercised. A bare
    # MagicMock auto-creates every attribute, which silently defeats those defaults.
    def test_check_subscription_price_creep(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            merchant="CloudStream",
            disposition="CANCELLABLE",
            latest_charge=19.99,
            prior_charge=15.99,
            price_increase_amount=4.00,
            pct_increase=25.0,
            annual_impact=48.00,
            estimated_annual_cost=239.88,
            effective_date="2026-09-01",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_subscription_price_creep(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "PRICE_CREEP")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "price_creep:cloudstream")
        self.assertIn("+25.0%", a["title"])
        self.assertIn("$15.99 to $19.99", a["detail"])
        self.assertIn("2026-09-01", a["detail"])
        # Savings must be the annualised increase, not the full plan cost.
        self.assertIn("$48.00", a["suggested_fix"])
        self.assertNotIn("239.88", a["suggested_fix"])

    def test_price_creep_reshoppable_avoids_cancel_advice(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            merchant="Example Mutual",
            disposition="RESHOPPABLE",
            latest_charge=536.70,
            prior_charge=500.00,
            price_increase_amount=36.70,
            pct_increase=7.3,
            annual_impact=440.40,
            estimated_annual_cost=6440.40,
            effective_date="2026-09-01",
        )
        mock_bq.query.return_value.result.return_value = [row]

        a = alerts.check_subscription_price_creep(mock_bq, "proj", "ds")[0]
        fix = a["suggested_fix"].lower()
        self.assertNotIn("cancel/rotate", fix)
        self.assertIn("quotes", fix)

    def test_check_subscription_overlap(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            domain_name="VIDEO_STREAMING",
            active_service_count=3,
            combined_annual_cost=540.0,
            combined_monthly_cost=45.0,
            consolidation_savings_monthly=25.0,
            consolidation_savings_annual=300.0,
            active_services="StreamA, StreamB, StreamC",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_subscription_overlap(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "SUBSCRIPTION_OVERLAP")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "overlap:video_streaming")
        self.assertIn("Video Streaming", a["title"])
        self.assertIn("StreamA, StreamB, StreamC", a["detail"])
        self.assertIn("$45.00/mo", a["detail"])
        # Recoverable amount keeps one service, so it is never the whole domain total.
        self.assertIn("$25.00/month", a["suggested_fix"])
        self.assertNotIn("$45.00/month", a["suggested_fix"])

    def test_check_utility_seasonal_spike(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            merchant="Example Energy",
            spend_month="2026-08-01",
            month_total=420.07,
            seasonal_avg=300.00,
            seasonal_stddev=30.00,
            years_observed=3,
            variance_vs_season=120.07,
            variance_pct=40.0,
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_utility_seasonal_spike(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "UTILITY_SEASONAL_SPIKE")
        self.assertEqual(a["alert_key"], "utility_season:example_energy:2026-08")
        self.assertIn("seasonal", a["detail"].lower())
        # A regulated utility must never be handed a cancel-to-save recommendation.
        fix = a["suggested_fix"].lower()
        self.assertNotIn("cancel/rotate", fix)
        self.assertIn("not a cancellable plan", fix)

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

    def test_generate_daily_brief_synopsis(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.brief_date = "2026-09-11"
        mock_row.day_of_month = 11
        mock_row.liquid_balance = 14250.00
        mock_row.fixed_burn = 4120.00
        mock_row.heloc_name = "Primary HELOC"
        mock_row.heloc_balance = 325096.16
        mock_row.heloc_apr = 0.0675
        mock_row.daily_interest_cost = 60.12
        mock_row.monthly_interest_cost = 1828.67
        mock_row.mtd_spend = 1420.50
        mock_row.mtd_count = 18
        mock_bq.query.return_value.result.return_value = [mock_row]

        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "alert_key": "price_creep:xcel",
                "title": "Subscription Price Hike: Xcel Energy",
                "detail": "Increased from $229.61 to $420.07",
                "suggested_fix": "Audit usage",
            },
            {
                "type": "FOOD_LEAKAGE",
                "severity": "WARNING",
                "alert_key": "food_leakage:2026-09",
                "title": "High Dining/Delivery Ratio (68.4% of food budget)",
                "detail": "Dining accounted for $249.39",
                "suggested_fix": "Cook at home",
            },
        ]

        synopsis = alerts.generate_daily_brief_synopsis(mock_bq, "proj", "ds", mock_alerts)
        self.assertEqual(synopsis["date"], "2026-09-11")
        self.assertEqual(synopsis["day_of_month"], 11)
        self.assertEqual(synopsis["liquid_balance"], 14250.00)
        self.assertEqual(synopsis["fixed_burn"], 4120.00)
        self.assertEqual(synopsis["coverage_ratio"], 3.46)
        self.assertEqual(synopsis["heloc_balance"], 325096.16)
        self.assertEqual(synopsis["daily_interest_cost"], 60.12)
        self.assertEqual(synopsis["monthly_interest_cost"], 1828.67)
        self.assertEqual(synopsis["mtd_spend"], 1420.50)
        self.assertEqual(synopsis["daily_burn_rate"], 129.14)

        self.assertIn("Liquid Cash", synopsis["posture_text"])
        self.assertIn("$14,250.00", synopsis["posture_text"])
        self.assertIn("HELOC Carry", synopsis["posture_text"])
        self.assertIn("$60.12/day", synopsis["posture_text"])
        self.assertIn("Month-to-Date Spend", synopsis["posture_text"])

        # Check focus attention items
        focus_str = " ".join(synopsis["focus_items"])
        self.assertIn("Price Hike", focus_str)
        self.assertIn("Xcel Energy", focus_str)
        self.assertIn("Food Pacing", focus_str)
        self.assertIn("HELOC Paydown", focus_str)

    def test_build_chat_card_v2_with_synopsis(self):
        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "alert_key": "price_creep:xcel",
                "title": "Subscription Price Hike: Xcel Energy",
                "detail": "Increased from $229.61 to $420.07",
                "suggested_fix": "Audit usage",
            }
        ]
        mock_synopsis = {
            "date": "2026-09-11",
            "posture_text": "🏦 <b>Liquid Cash:</b> $14,250.00<br>💳 <b>HELOC Carry:</b> $60.12/day",
            "focus_items": [
                "🔍 <b>Price Hike:</b> Xcel Energy increased +82.9%. Audit usage.",
                "💳 <b>HELOC Paydown:</b> Running at $60.12/day. Sweep checking surplus.",
            ],
        }

        payload = alerts.build_chat_card_v2(mock_alerts, synopsis=mock_synopsis)
        self.assertIn("cardsV2", payload)
        card = payload["cardsV2"][0]["card"]
        self.assertEqual(card["header"]["title"], "FinSage")
        self.assertEqual(card["header"]["subtitle"], "Daily Synopsis & Spend Advisory")

        sections = card["sections"]
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["header"], "🌅 Morning Financial Synopsis")
        self.assertEqual(sections[1]["header"], "Daily Optimization Opportunities")

        synopsis_widgets = sections[0]["widgets"]
        self.assertEqual(len(synopsis_widgets), 2)
        self.assertEqual(synopsis_widgets[0]["decoratedText"]["topLabel"], "DAILY POSTURE SNAPSHOT • 2026-09-11")
        self.assertIn("Liquid Cash", synopsis_widgets[0]["decoratedText"]["text"])
        self.assertEqual(synopsis_widgets[1]["decoratedText"]["topLabel"], "WHAT TO PAY ATTENTION TO TODAY")
        self.assertIn("Xcel Energy", synopsis_widgets[1]["decoratedText"]["text"])

        alert_widgets = sections[1]["widgets"]
        self.assertEqual(len(alert_widgets), 2)  # text + snooze button
        self.assertIn("Xcel Energy", alert_widgets[0]["decoratedText"]["text"])

    def test_build_markdown_fallback_with_synopsis(self):
        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "title": "Subscription Price Hike: Xcel Energy",
                "detail": "Increased from $229.61 to $420.07",
                "suggested_fix": "Audit usage",
            }
        ]
        mock_synopsis = {
            "date": "2026-09-11",
            "posture_md": "• **Liquid Reserves**: $14,250.00\n• **HELOC Daily Carry**: $60.12/day",
            "focus_items_md": [
                "**Price Hike**: Xcel Energy. Audit usage.",
                "**HELOC Paydown**: Running at $60.12/day.",
            ],
        }

        md = alerts.build_markdown_fallback(mock_alerts, synopsis=mock_synopsis)
        self.assertIn("🌅 FinSage Morning Brief — 2026-09-11", md)
        self.assertIn("Daily Posture Snapshot:", md)
        self.assertIn("$14,250.00", md)
        self.assertIn("What to Pay Attention to Today:", md)
        self.assertIn("Xcel Energy", md)
        self.assertIn("Daily Optimization Opportunities", md)

    def test_get_daily_morning_brief_tool(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.brief_date = "2026-09-11"
        mock_row.day_of_month = 11
        mock_row.liquid_balance = 10000.00
        mock_row.fixed_burn = 3000.00
        mock_row.heloc_name = "HELOC"
        mock_row.heloc_balance = 100000.00
        mock_row.heloc_apr = 0.07
        mock_row.daily_interest_cost = 19.18
        mock_row.monthly_interest_cost = 583.33
        mock_row.mtd_spend = 800.00
        mock_row.mtd_count = 10
        mock_bq.query.return_value.result.return_value = [mock_row]

        with patch("app.alerts.collect_all_alerts", return_value=[]):
            with patch("app.alerts.bigquery.Client", return_value=mock_bq):
                res = alerts.get_daily_morning_brief()
                self.assertIn("FinSage Morning Brief", res)
                self.assertIn("Daily Posture Snapshot", res)
                self.assertIn("$10,000.00", res)

    def test_extract_budget_caps_hardening(self):
        # 1. Comma formatted and annual conversion
        memories = [
            "Our food budget is $9,000 per year for dining and groceries.",
            "Annual grocery budget is $12,000.",
        ]
        caps = alerts.extract_budget_caps(memories)
        self.assertEqual(caps.get("dining"), 750.0)
        self.assertEqual(caps.get("groceries"), 1000.0)

        # 2. Monthly limits with commas and decimals
        memories2 = [
            "Dining cap is $850.00/month",
            "Supermarket grocery limit: $1,250",
        ]
        caps2 = alerts.extract_budget_caps(memories2)
        self.assertEqual(caps2.get("dining"), 850.0)
        self.assertEqual(caps2.get("groceries"), 1250.0)

        # 3. Incidental numbers without budget context should NOT match
        memories3 = [
            "We put $2,500 on the HELOC and ate some food yesterday.",
            "Bought groceries for $45.20 at the store.",
        ]
        caps3 = alerts.extract_budget_caps(memories3)
        self.assertEqual(caps3, {})

    def test_generate_daily_brief_synopsis_error_handling(self):
        mock_bq = MagicMock()
        mock_bq.query.side_effect = RuntimeError("BigQuery connection reset")

        synopsis = alerts.generate_daily_brief_synopsis(mock_bq, "test-proj", "test-ds", alerts=[])
        self.assertTrue(synopsis["is_error"])
        self.assertIn("BigQuery live data unavailable", synopsis["posture_text"])
        self.assertIn("Data Degraded", synopsis["focus_items"][0])
        # Never swallow error into "All Systems Normal"
        self.assertNotIn("All Systems Normal", synopsis["focus_items"][0])

    def test_generate_daily_brief_synopsis_negative_checking_and_early_month_burn(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.brief_date = "2026-09-02"
        mock_row.day_of_month = 2  # Early month: Day 2 <= 3
        mock_row.liquid_balance = -450.25  # Negative checking
        mock_row.fixed_burn = 4000.00
        mock_row.heloc_name = "HELOC"
        mock_row.heloc_balance = 50000.00
        mock_row.heloc_apr = 0.08
        mock_row.daily_interest_cost = 10.96
        mock_row.monthly_interest_cost = 333.33
        mock_row.mtd_spend = 120.00
        mock_row.mtd_count = 3
        mock_bq.query.return_value.result.return_value = [mock_row]

        synopsis = alerts.generate_daily_brief_synopsis(mock_bq, "test-proj", "test-ds", alerts=[])
        self.assertFalse(synopsis["is_error"])
        # Check negative liquid warning
        self.assertIn("OVERDRAWN", synopsis["posture_text"])
        self.assertIn("Overdrawn Checking", " ".join(synopsis["focus_items"]))
        # Early-month burn rate pacing
        self.assertIn("pacing calibrating", synopsis["posture_text"])

    def test_snooze_spend_alert_clamping(self):
        mock_bq = MagicMock()
        with patch("app.alerts.bigquery.Client", return_value=mock_bq):
            with patch("app.alerts.suppress_alert", return_value=True) as mock_suppress:
                # Test upper clamping (99999 -> 90)
                res_high = alerts.snooze_spend_alert("price_creep:netflix", days=99999)
                self.assertIn("snoozed alert 'price_creep:netflix' for 90 days", res_high)
                mock_suppress.assert_called_with(
                    bq=mock_bq,
                    project_id=config.BQ_PROJECT_ID,
                    dataset_id=config.BQ_DATASET_ID,
                    alert_key="price_creep:netflix",
                    alert_type="USER_REQUESTED",
                    days=90,
                    reason="Snoozed by user request via Gemini chat for 90 days",
                )

                # Test lower clamping (-5 -> 1)
                res_low = alerts.snooze_spend_alert("price_creep:netflix", days=-5)
                self.assertIn("snoozed alert 'price_creep:netflix' for 1 days", res_low)

    def test_snooze_signature_cryptographic_verification(self):
        import time

        from app.monarch_service import generate_snooze_signature, verify_snooze_signature

        alert_key = "price_creep:club_greenwood"
        days = 7
        now_ts = int(time.time())

        sig = generate_snooze_signature(alert_key, days, now_ts)
        self.assertTrue(len(sig) == 64)

        # 1. Valid signature
        is_valid, msg = verify_snooze_signature(alert_key, days, now_ts, sig)
        self.assertTrue(is_valid)
        self.assertEqual(msg, "Valid")

        # 2. Tampered alert key
        is_valid, msg = verify_snooze_signature("price_creep:other_vendor", days, now_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("signature mismatch", msg)

        # 3. Tampered days
        is_valid, msg = verify_snooze_signature(alert_key, 14, now_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("signature mismatch", msg)

        # 4. Expired signature (e.g. 10 days old when limit is 7 days)
        old_ts = now_ts - (86400 * 10)
        old_sig = generate_snooze_signature(alert_key, days, old_ts)
        is_valid, msg = verify_snooze_signature(alert_key, days, old_ts, old_sig)
        self.assertFalse(is_valid)
        self.assertIn("expired", msg)

    def test_build_chat_card_v2_degraded_state_and_snooze_hmac(self):
        mock_alerts = [
            {
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "alert_key": "price_creep:xcel",
                "title": "Subscription Price Hike: Xcel Energy",
                "detail": "Increased from $229.61 to $420.07",
                "suggested_fix": "Audit usage",
            }
        ]
        mock_synopsis = {
            "date": "2026-09-11",
            "is_error": True,
            "posture_text": "⚠️ <b>Account Posture:</b> BigQuery live data unavailable.",
            "focus_items": ["⚠️ <b>Data Degraded:</b> Live BigQuery posture query failed."],
        }

        payload = alerts.build_chat_card_v2(mock_alerts, synopsis=mock_synopsis)
        sections = payload["cardsV2"][0]["card"]["sections"]
        self.assertEqual(sections[0]["header"], "⚠️ Morning Financial Synopsis (Data Degraded)")
        self.assertEqual(sections[0]["widgets"][0]["decoratedText"]["startIcon"]["knownIcon"], "ERROR")

        # Verify snooze button parameters include HMAC signature
        snooze_btn_params = sections[1]["widgets"][1]["buttonList"]["buttons"][0]["onClick"]["action"]["parameters"]
        param_dict = {p["key"]: p["value"] for p in snooze_btn_params}
        self.assertEqual(param_dict["alert_key"], "price_creep:xcel")
        self.assertEqual(param_dict["days"], "7")
        self.assertIn("ts", param_dict)
        self.assertIn("sig", param_dict)
        self.assertEqual(len(param_dict["sig"]), 64)

    def test_check_duplicate_charges(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            t1_id="txn_101",
            t2_id="txn_102",
            account_id="acct_checking_01",
            account_name="Primary Checking",
            merchant="Merchant X",
            category_name="Shopping",
            amount=49.99,
            t1_date="2026-09-08",
            t2_date="2026-09-08",
            days_apart=0,
            alert_key="duplicate:txn_101:txn_102",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_duplicate_charges(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "DUPLICATE_CHARGE")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "duplicate:txn_101:txn_102")
        self.assertIn("Potential Duplicate Charge: Merchant X ($49.99)", a["title"])
        self.assertIn("same day", a["detail"])
        self.assertIn("double-billed", a["suggested_fix"].lower())

    def test_check_duplicate_charges_days_apart_and_error(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            t1_id="txn_201",
            t2_id="txn_202",
            account_id="acct_checking_01",
            account_name="Primary Checking",
            merchant="Restaurant Y",
            category_name="Dining",
            amount=85.50,
            t1_date="2026-09-06",
            t2_date="2026-09-08",
            days_apart=2,
            alert_key="duplicate:txn_201:txn_202",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_duplicate_charges(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        self.assertIn("2 day(s) apart", found[0]["detail"])

        mock_bq.query.side_effect = Exception("BQ connection timeout")
        err_alerts = alerts.check_duplicate_charges(mock_bq, "proj", "ds")
        self.assertEqual(len(err_alerts), 1)
        self.assertEqual(err_alerts[0]["type"], "QUERY_ERROR")

    def test_check_new_subscriptions(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            merchant="SuperSaaS",
            category_name="Software",
            functional_domain="SOFTWARE_SAAS",
            disposition="CANCELLABLE",
            first_seen="2026-09-01",
            latest_seen="2026-09-01",
            charge_count=1,
            avg_charge=19.99,
            total_spend=19.99,
            is_recurring_flagged=True,
            days_since_first_charge=9,
            alert_key="new_sub:supersaas",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_new_subscriptions(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "NEW_SUBSCRIPTION_DETECTED")
        self.assertEqual(a["severity"], "WARNING")
        self.assertEqual(a["alert_key"], "new_sub:supersaas")
        self.assertIn("New Subscription Detected: SuperSaaS ($19.99/mo)", a["title"])
        self.assertIn("9 days ago", a["detail"])
        self.assertIn("auto-converting free trial", a["suggested_fix"].lower())

    def test_check_new_subscriptions_error(self):
        mock_bq = MagicMock()
        mock_bq.query.side_effect = Exception("Query execution error")
        err_alerts = alerts.check_new_subscriptions(mock_bq, "proj", "ds")
        self.assertEqual(len(err_alerts), 1)
        self.assertEqual(err_alerts[0]["type"], "QUERY_ERROR")

    def test_check_annual_bill_radar_reshoppable(self):
        mock_bq = MagicMock()
        row = SimpleNamespace(
            merchant="Acme Auto Insurance",
            category_name="Auto Insurance",
            functional_domain="INSURANCE_AUTO",
            disposition="RESHOPPABLE",
            cadence_type="SEMI_ANNUAL",
            prior_charge_amount=650.00,
            prior_charge_date="2026-03-25",
            predicted_renewal_date="2026-09-23",
            days_until_renewal=12,
            alert_key="annual_bill:acme_auto_insurance:2026",
        )
        mock_bq.query.return_value.result.return_value = [row]

        found = alerts.check_annual_bill_radar(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        a = found[0]
        self.assertEqual(a["type"], "ANNUAL_BILL_RADAR")
        self.assertEqual(a["severity"], "WARNING")  # >= 250
        self.assertEqual(a["alert_key"], "annual_bill:acme_auto_insurance:2026")
        self.assertIn("Upcoming Semi Annual Bill: Acme Auto Insurance (~$650.00)", a["title"])
        self.assertIn("due in ~12 days", a["detail"])
        self.assertIn("Shop competing insurance/contract rates", a["suggested_fix"])

    def test_check_annual_bill_radar_cancellable_and_timing_variations(self):
        mock_bq = MagicMock()
        row_today = SimpleNamespace(
            merchant="Annual Cloud Backup",
            category_name="Software",
            functional_domain="CLOUD_STORAGE",
            disposition="CANCELLABLE",
            cadence_type="ANNUAL",
            prior_charge_amount=99.00,
            prior_charge_date="2025-09-10",
            predicted_renewal_date="2026-09-10",
            days_until_renewal=0,
            alert_key="annual_bill:annual_cloud_backup:2026",
        )
        mock_bq.query.return_value.result.return_value = [row_today]

        found = alerts.check_annual_bill_radar(mock_bq, "proj", "ds")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "INFO")  # < 250
        self.assertIn("due today", found[0]["detail"])
        self.assertIn("sufficient liquidity", found[0]["suggested_fix"])

        mock_bq.query.side_effect = Exception("Query error")
        err_alerts = alerts.check_annual_bill_radar(mock_bq, "proj", "ds")
        self.assertEqual(len(err_alerts), 1)
        self.assertEqual(err_alerts[0]["type"], "QUERY_ERROR")

    def test_collect_all_alerts_pr7a_integration(self):
        mock_bq = MagicMock()
        with (
            patch(
                "app.alerts.check_subscription_price_creep",
                return_value=[{"type": "PRICE_CREEP", "alert_key": "k1"}],
            ),
            patch(
                "app.alerts.check_duplicate_charges",
                return_value=[{"type": "DUPLICATE_CHARGE", "alert_key": "k2"}],
            ),
            patch(
                "app.alerts.check_new_subscriptions",
                return_value=[{"type": "NEW_SUBSCRIPTION_DETECTED", "alert_key": "k3"}],
            ),
            patch(
                "app.alerts.check_annual_bill_radar",
                return_value=[{"type": "ANNUAL_BILL_RADAR", "alert_key": "k4"}],
            ),
            patch("app.alerts.check_food_efficiency", return_value=[]),
            patch("app.alerts.check_heloc_daily_cost", return_value=[]),
            patch("app.alerts.check_subscription_overlap", return_value=[]),
            patch("app.alerts.check_utility_seasonal_spike", return_value=[]),
            patch("app.alerts.check_micro_transaction_leakage", return_value=[]),
            patch("app.alerts.check_memory_budget_limits", return_value=[]),
            patch("app.alerts.get_active_suppressions", return_value={"k2"}),
        ):
            all_alerts = alerts.collect_all_alerts(mock_bq, "proj", "ds")
            keys = [a["alert_key"] for a in all_alerts]
            self.assertIn("k1", keys)
            self.assertNotIn("k2", keys)  # k2 suppressed
            self.assertIn("k3", keys)
            self.assertIn("k4", keys)

    def test_generate_daily_brief_synopsis_with_pr7a_anomalies(self):
        mock_bq = MagicMock()
        checking_row = SimpleNamespace(liquid_checking_balance=5000.0)
        burn_row = SimpleNamespace(fixed_monthly_burn=3000.0)
        heloc_row = SimpleNamespace(display_name="HELOC", current_balance=10000.0, interest_rate=8.5)
        mtd_row = SimpleNamespace(mtd_spend=1200.0, mtd_count=20)
        mock_bq.query.return_value.result.side_effect = [
            [checking_row],
            [burn_row],
            [heloc_row],
            [mtd_row],
        ]

        active_alerts = [
            {
                "type": "DUPLICATE_CHARGE",
                "title": "Potential Duplicate Charge: Merchant A ($25.00)",
                "detail": "Two identical charges posted same day.",
                "suggested_fix": "Verify with merchant.",
                "alert_key": "dup:1:2",
            },
            {
                "type": "NEW_SUBSCRIPTION_DETECTED",
                "title": "New Subscription Detected: Service B ($14.99/mo)",
                "detail": "First charged 5 days ago.",
                "suggested_fix": "Cancel if trial.",
                "alert_key": "new_sub:b",
            },
            {
                "type": "ANNUAL_BILL_RADAR",
                "title": "Upcoming Annual Bill: Policy C (~$500.00)",
                "detail": "Expected due in ~10 days.",
                "suggested_fix": "Shop competing rates.",
                "alert_key": "annual:c:2026",
            },
        ]

        synopsis = alerts.generate_daily_brief_synopsis(mock_bq, "proj", "ds", alerts=active_alerts)
        self.assertFalse(synopsis["is_error"])
        focus_md = "\n".join(synopsis["focus_items_md"])
        self.assertIn("Duplicate Charge", focus_md)
        self.assertIn("Merchant A", focus_md)
        self.assertIn("New Recurring Plan", focus_md)
        self.assertIn("Service B", focus_md)
        self.assertIn("Annual Bill Radar", focus_md)
        self.assertIn("Policy C", focus_md)


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

    def test_generate_daily_brief_synopsis_thermometer_and_posture_badges(self):
        mock_bq = MagicMock()
        mock_row = MagicMock()
        mock_row.brief_date = "2026-09-12"
        mock_row.day_of_month = 12
        mock_row.liquid_balance = 12000.00
        mock_row.fixed_burn = 3500.00
        mock_row.heloc_name = "HELOC"
        mock_row.heloc_balance = 85000.00
        mock_row.heloc_apr = 0.08
        mock_row.daily_interest_cost = 18.63
        mock_row.monthly_interest_cost = 566.67
        mock_row.mtd_spend = 1440.00
        mock_row.mtd_count = 18
        mock_bq.query.return_value.result.return_value = [mock_row]

        synopsis = alerts.generate_daily_brief_synopsis(mock_bq, "test-proj", "test-ds", alerts=[])
        self.assertFalse(synopsis["is_error"])
        self.assertEqual(synopsis["days_in_month"], 30)
        self.assertEqual(synopsis["month_pct"], 40.0)
        self.assertEqual(synopsis["status_code"], "DEBT_CARRY")
        self.assertEqual(synopsis["status_badge"], "⚡ DEBT CARRY")
        self.assertIn("█", synopsis["pacing_thermometer"])
        self.assertIn("░", synopsis["pacing_thermometer"])
        self.assertIn("Day 12/30 (40% elapsed)", synopsis["pacing_thermometer"])
        self.assertIn("Projected: $3,600.00", synopsis["pacing_thermometer"])
        self.assertIn("🧭 <b>Posture:</b> ⚡ DEBT CARRY", synopsis["posture_text"])

    def test_build_chat_card_v2_with_thermometer_and_categorized_alerts(self):
        mock_alerts = [
            {
                "type": "MICRO_TRANSACTION_LEAKAGE",
                "title": "Frequent Small Purchases: Coffee",
                "detail": "12 transactions totaling $72",
                "suggested_fix": "Batch coffee visits",
                "alert_key": "micro_coffee",
            },
            {
                "type": "DUPLICATE_CHARGE",
                "title": "Duplicate Charge: $45.00 at Grocery Store",
                "detail": "Identical charges posted 1 day apart",
                "suggested_fix": "Request refund from merchant",
                "alert_key": "dup_123",
            },
            {
                "type": "PRICE_CREEP",
                "title": "Price Hike: SaaS Tool",
                "detail": "Increased from $10 to $15",
                "suggested_fix": "Audit usage",
                "alert_key": "creep_saas",
            },
        ]
        mock_synopsis = {
            "date": "2026-09-12",
            "status_badge": "🟢 ON TRACK",
            "posture_text": "🧭 <b>Posture:</b> 🟢 ON TRACK<br>🏦 <b>Liquid Cash:</b> $12,000.00",
            "pacing_thermometer": "████░░░░░░ Day 12/30 (40% elapsed)<br>MTD Outflow: $1,440.00",
            "focus_items": ["✅ <b>All Systems Normal:</b> Spend is tracking normally."],
        }

        payload = alerts.build_chat_card_v2(mock_alerts, synopsis=mock_synopsis)
        card = payload["cardsV2"][0]["card"]
        sections = card["sections"]
        self.assertEqual(len(sections), 2)

        # Synopsis section widgets: Posture snapshot, Pacing Thermometer, Focus items
        syn_widgets = sections[0]["widgets"]
        self.assertEqual(len(syn_widgets), 3)
        self.assertEqual(syn_widgets[1]["decoratedText"]["topLabel"], "MONTH-TO-DATE SPEND PACING THERMOMETER")
        self.assertIn("Day 12/30", syn_widgets[1]["decoratedText"]["text"])

        # Alert section: Sorted by priority so DUPLICATE_CHARGE appears before PRICE_CREEP and MICRO_TRANSACTION_LEAKAGE
        alert_widgets = sections[1]["widgets"]
        # DUPLICATE_CHARGE (widget 0 = text, widget 1 = snooze)
        self.assertEqual(alert_widgets[0]["decoratedText"]["topLabel"], "⚡ URGENT ANOMALY • DUPLICATE CHARGE")
        self.assertIn("Duplicate Charge", alert_widgets[0]["decoratedText"]["text"])
        # PRICE_CREEP (widget 2 = text, widget 3 = snooze)
        self.assertEqual(alert_widgets[2]["decoratedText"]["topLabel"], "🔄 RECURRING SPEND • PRICE HIKE")
        # MICRO_TRANSACTION_LEAKAGE (widget 4 = text, widget 5 = snooze)
        self.assertEqual(alert_widgets[4]["decoratedText"]["topLabel"], "☕ LIFESTYLE LEAK • CONVENIENCE HABIT")

    def test_generate_executive_digest_weekly_and_monthly(self):
        mock_bq = MagicMock()

        # Summary query row
        mock_summary = MagicMock()
        mock_summary.total_spend = 1250.75
        mock_summary.txn_count = 22
        mock_summary.avg_ticket = 56.85
        mock_summary.liquid_balance = 8500.00
        mock_summary.fixed_burn = 3200.00
        mock_summary.heloc_name = "HELOC"
        mock_summary.heloc_balance = 60000.00
        mock_summary.heloc_apr = 0.0825
        mock_summary.daily_interest_cost = 13.56
        mock_summary.monthly_interest_cost = 412.50

        # Category query rows
        mock_cat1 = MagicMock(category_name="Groceries", total=450.25, count=6)
        mock_cat2 = MagicMock(category_name="Dining Out", total=310.50, count=8)

        # Merchant query rows
        mock_m1 = MagicMock(merchant="Whole Foods", total=275.50, count=3)
        mock_m2 = MagicMock(merchant="Costco", total=174.75, count=2)

        mock_bq.query.side_effect = [
            MagicMock(result=MagicMock(return_value=[mock_summary])),
            MagicMock(result=MagicMock(return_value=[mock_cat1, mock_cat2])),
            MagicMock(result=MagicMock(return_value=[mock_m1, mock_m2])),
        ]

        # Weekly digest
        weekly_digest = alerts.generate_executive_digest(mock_bq, "test-proj", "test-ds", period="WEEKLY")
        self.assertFalse(weekly_digest["is_error"])
        self.assertEqual(weekly_digest["period"], "WEEKLY")
        self.assertEqual(weekly_digest["period_days"], 7)
        self.assertEqual(weekly_digest["total_spend"], 1250.75)
        self.assertEqual(weekly_digest["txn_count"], 22)
        self.assertEqual(weekly_digest["coverage_ratio"], 2.66)
        self.assertEqual(len(weekly_digest["top_categories"]), 2)
        self.assertEqual(weekly_digest["top_categories"][0]["category_name"], "Groceries")
        self.assertEqual(len(weekly_digest["top_merchants"]), 2)
        self.assertEqual(weekly_digest["top_merchants"][0]["merchant"], "Whole Foods")
        self.assertTrue(len(weekly_digest["cfo_takeaways"]) > 0)

        # Monthly digest
        mock_bq.query.side_effect = [
            MagicMock(result=MagicMock(return_value=[mock_summary])),
            MagicMock(result=MagicMock(return_value=[mock_cat1])),
            MagicMock(result=MagicMock(return_value=[mock_m1])),
        ]
        monthly_digest = alerts.generate_executive_digest(mock_bq, "test-proj", "test-ds", period="MONTHLY")
        self.assertEqual(monthly_digest["period"], "MONTHLY")
        self.assertEqual(monthly_digest["period_title"], "Monthly")

    def test_build_executive_digest_card_and_markdown(self):
        mock_digest = {
            "period": "WEEKLY",
            "period_title": "Weekly",
            "period_label": "Trailing 7 Days (Sep 5 – Sep 12, 2026)",
            "period_days": 7,
            "total_spend": 1250.75,
            "txn_count": 22,
            "avg_ticket": 56.85,
            "daily_run_rate": 178.68,
            "liquid_balance": 8500.00,
            "fixed_burn": 3200.00,
            "coverage_ratio": 2.7,
            "heloc_name": "HELOC",
            "heloc_balance": 60000.00,
            "daily_interest_cost": 13.56,
            "period_interest_cost": 94.92,
            "top_categories": [
                {"category_name": "Groceries", "total": 450.25, "count": 6, "pct": 36.0},
                {"category_name": "Dining Out", "total": 310.50, "count": 8, "pct": 24.8},
            ],
            "top_merchants": [
                {"merchant": "Whole Foods", "total": 275.50, "count": 3},
            ],
            "cfo_takeaways": [
                "Household spend totaled $1,250.75 (22 transactions, ~$178.68/day).",
                "Highest category concentration: **Groceries** consumed $450.25 (36.0% of total outflow).",
                "💳 **Actionable Sweep**: Liquid reserves ($8,500.00) exceed required buffer. Sweep surplus to HELOC.",
            ],
            "is_error": False,
        }

        # Card v2
        card_res = alerts.build_executive_digest_card(mock_digest)
        self.assertIn("cardsV2", card_res)
        card = card_res["cardsV2"][0]["card"]
        self.assertEqual(card["header"]["title"], "FinSage")
        self.assertEqual(card["header"]["subtitle"], "Weekly Executive CFO Digest")
        self.assertEqual(len(card["sections"]), 3)
        self.assertIn("Executive CFO Weekly Overview", card["sections"][0]["header"])
        self.assertIn("Outflow Concentration", card["sections"][1]["header"])
        self.assertIn("Strategic CFO Takeaways", card["sections"][2]["header"])

        # Markdown
        md = alerts.build_executive_digest_markdown(mock_digest)
        self.assertIn("## 📊 FinSage Executive CFO Digest: Weekly Recap", md)
        self.assertIn("Total Spend**: $1,250.75", md)
        self.assertIn("Groceries**: $450.25", md)
        self.assertIn("Whole Foods**: $275.50", md)
        self.assertIn("Strategic CFO Takeaways", md)

    def test_get_executive_cfo_digest_tool(self):
        mock_bq = MagicMock()
        mock_summary = MagicMock(
            total_spend=900.00,
            txn_count=12,
            avg_ticket=75.00,
            liquid_balance=5000.00,
            fixed_burn=2500.00,
            heloc_name="HELOC",
            heloc_balance=40000.00,
            heloc_apr=0.08,
            daily_interest_cost=8.77,
            monthly_interest_cost=266.67,
        )
        mock_bq.query.side_effect = [
            MagicMock(result=MagicMock(return_value=[mock_summary])),
            MagicMock(result=MagicMock(return_value=[])),
            MagicMock(result=MagicMock(return_value=[])),
        ]

        with patch("app.alerts.bigquery.Client", return_value=mock_bq):
            res = alerts.get_executive_cfo_digest(period="weekly")
            self.assertIn("Executive CFO Digest", res)
            self.assertIn("$900.00", res)

    def test_fastapi_advisor_digest_endpoint(self):
        from fastapi.testclient import TestClient

        from app.main import app, verify_api_key

        client = TestClient(app)
        auth_headers = {"X-API-Key": "test-api-key"}

        mock_digest = {
            "period": "WEEKLY",
            "period_title": "Weekly",
            "total_spend": 1250.00,
            "txn_count": 15,
            "is_error": False,
        }

        app.dependency_overrides[verify_api_key] = lambda: "test-api-key"
        try:
            with patch("app.main.get_bq_client", return_value=MagicMock()):
                with patch("app.main.generate_executive_digest", return_value=mock_digest):
                    resp = client.get("/advisor/digest?period=weekly", headers=auth_headers)
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    self.assertEqual(data["period"], "WEEKLY")
                    self.assertEqual(data["total_spend"], 1250.00)

                    resp_m = client.get("/advisor/digest?period=monthly", headers=auth_headers)
                    self.assertEqual(resp_m.status_code, 200)
        finally:
            app.dependency_overrides.clear()

    def test_chat_webhook_digest_command(self):
        from app.main import google_chat_webhook

        sample_event = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/test/messages/msg123",
                "text": "@FinSage /digest weekly",
                "sender": {"displayName": "Nick", "email": "nick@example.com"},
            },
            "space": {"name": "spaces/test"},
        }

        mock_digest = {
            "period": "WEEKLY",
            "period_title": "Weekly",
            "period_label": "Trailing 7 Days",
            "total_spend": 1200.00,
            "txn_count": 10,
            "avg_ticket": 120.00,
            "daily_run_rate": 171.43,
            "liquid_balance": 9000.00,
            "coverage_ratio": 3.0,
            "heloc_balance": 0.0,
            "daily_interest_cost": 0.0,
            "period_interest_cost": 0.0,
            "top_categories": [],
            "top_merchants": [],
            "cfo_takeaways": ["Spend is well managed."],
            "is_error": False,
        }

        with patch("app.main.generate_executive_digest", return_value=mock_digest):
            res = asyncio.run(google_chat_webhook(sample_event))
            self.assertIn("cardsV2", res)
            self.assertIn("Executive CFO Digest", res["text"])

    def test_job_run_digest(self):
        from app import job

        mock_digest = {
            "period": "WEEKLY",
            "total_spend": 1000.0,
            "is_error": False,
        }

        with patch("app.alerts.generate_executive_digest", return_value=mock_digest):
            with patch("app.alerts.resolve_secret", return_value="https://chat.googleapis.com/v1/spaces/XYZ/messages"):
                with patch("app.bq_service.get_bq_client", return_value=MagicMock()):
                    with patch("requests.post") as mock_post:
                        mock_post.return_value.status_code = 200
                        res = asyncio.run(job.run_digest("weekly"))
                        self.assertEqual(res["status"], "success")
                        self.assertTrue(res["webhook_dispatched"])
                        mock_post.assert_called_once()

    def test_check_paycheck_surplus_sweep_opportunity_detected(self):
        from app.alerts import check_paycheck_surplus_sweep

        mock_bq = MagicMock()
        mock_row = MagicMock(
            evaluation_date="2026-09-10",
            latest_income_id="tx_123",
            latest_income_date="2026-09-08",
            employer_or_source="Acme Corp Payroll",
            latest_income_amount=4500.00,
            liquid_balance=12000.00,
            monthly_fixed_burn=3500.00,
            upcoming_30d_lump_sums=500.00,
            safe_reserve_buffer=4525.00,
            safe_surplus=7475.00,
            heloc_name="First Tech HELOC",
            heloc_balance=45000.00,
            heloc_apr=0.0825,
            recommended_sweep_amount=7475.00,
            daily_interest_saved=1.69,
            monthly_interest_saved=51.39,
            annual_interest_saved=616.69,
            alert_key="paycheck_sweep:2026-09-08:7475",
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        alerts = check_paycheck_surplus_sweep(mock_bq, "test-p", "test-d")
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a["type"], "PAYCHECK_SURPLUS_SWEEP")
        self.assertEqual(a["severity"], "ACTION_REQUIRED")
        self.assertIn("Transfer $7,475.00 to First Tech HELOC", a["title"])
        self.assertIn("Acme Corp Payroll", a["detail"])
        self.assertIn("First Tech HELOC ($45,000.00 at 8.25% APR)", a["detail"])
        self.assertIn("$1.69/day ($51.39/mo, $616.69/yr guaranteed risk-free return)", a["suggested_fix"])

    def test_check_paycheck_surplus_sweep_no_alert_when_under_threshold(self):
        from app.alerts import check_paycheck_surplus_sweep

        mock_bq = MagicMock()
        mock_row = MagicMock(
            evaluation_date="2026-09-10",
            latest_income_id="tx_123",
            latest_income_date="2026-09-08",
            employer_or_source="Acme Corp Payroll",
            latest_income_amount=1000.00,
            liquid_balance=4000.00,
            monthly_fixed_burn=3500.00,
            upcoming_30d_lump_sums=0.0,
            safe_reserve_buffer=4025.00,
            safe_surplus=150.00,
            heloc_name="First Tech HELOC",
            heloc_balance=45000.00,
            heloc_apr=0.0825,
            recommended_sweep_amount=150.00,  # Below $250 floor
            daily_interest_saved=0.03,
            monthly_interest_saved=1.03,
            annual_interest_saved=12.38,
            alert_key="paycheck_sweep:2026-09-08:150",
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        alerts = check_paycheck_surplus_sweep(mock_bq, "test-p", "test-d")
        self.assertEqual(len(alerts), 0)

    def test_check_paycheck_surplus_sweep_no_alert_when_heloc_zero(self):
        from app.alerts import check_paycheck_surplus_sweep

        mock_bq = MagicMock()
        mock_row = MagicMock(
            evaluation_date="2026-09-10",
            latest_income_id="tx_123",
            latest_income_date="2026-09-08",
            employer_or_source="Acme Corp Payroll",
            latest_income_amount=5000.00,
            liquid_balance=15000.00,
            monthly_fixed_burn=3500.00,
            upcoming_30d_lump_sums=0.0,
            safe_reserve_buffer=4025.00,
            safe_surplus=10975.00,
            heloc_name="First Tech HELOC",
            heloc_balance=0.0,  # No debt to pay down
            heloc_apr=0.0825,
            recommended_sweep_amount=0.0,
            daily_interest_saved=0.0,
            monthly_interest_saved=0.0,
            annual_interest_saved=0.0,
            alert_key="paycheck_sweep:2026-09-08:0",
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        alerts = check_paycheck_surplus_sweep(mock_bq, "test-p", "test-d")
        self.assertEqual(len(alerts), 0)

    def test_get_paycheck_surplus_analysis_markdown(self):
        from app.alerts import get_paycheck_surplus_analysis

        mock_bq = MagicMock()
        mock_row = MagicMock(
            evaluation_date="2026-09-10",
            latest_income_id="tx_123",
            latest_income_date="2026-09-08",
            employer_or_source="Acme Corp Payroll",
            latest_income_amount=4500.00,
            liquid_balance=12000.00,
            monthly_fixed_burn=3500.00,
            upcoming_30d_lump_sums=500.00,
            safe_reserve_buffer=4525.00,
            safe_surplus=7475.00,
            heloc_name="First Tech HELOC",
            heloc_balance=45000.00,
            heloc_apr=0.0825,
            recommended_sweep_amount=7475.00,
            daily_interest_saved=1.69,
            monthly_interest_saved=51.39,
            annual_interest_saved=616.69,
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        md = get_paycheck_surplus_analysis(mock_bq, "test-p", "test-d")
        self.assertIn("### ⚡ Paycheck Surplus Sweep & Debt Paydown Analysis", md)
        self.assertIn("Current Checking Balance**: $12,000.00", md)
        self.assertIn("Recent Paycheck Deposit**: $4,500.00 from Acme Corp Payroll", md)
        self.assertIn("Required Reserve Buffer**: $4,525.00", md)
        self.assertIn("Recommended Principal Sweep**: **$7,475.00**", md)
        self.assertIn("Effective Return**: Guaranteed **8.25% APR**", md)

    def test_daily_synopsis_includes_paycheck_sweep_focus_item(self):
        from app.alerts import generate_daily_brief_synopsis

        mock_bq = MagicMock()
        mock_row = MagicMock(
            brief_date="2026-09-12",
            day_of_month=12,
            liquid_balance=12000.00,
            fixed_burn=3500.00,
            heloc_name="HELOC",
            heloc_balance=50000.00,
            heloc_apr=0.08,
            daily_interest_cost=10.96,
            monthly_interest_cost=333.33,
            mtd_spend=1000.00,
            mtd_count=10,
        )
        mock_bq.query.return_value.result.return_value = [mock_row]
        alerts = [
            {
                "type": "PAYCHECK_SURPLUS_SWEEP",
                "title": "⚡ Paycheck Sweep Opportunity: Transfer $7,475.00 to First Tech HELOC",
                "detail": "Safe checking surplus detected.",
                "suggested_fix": "Transfer $7,475.00 from Checking to First Tech HELOC to save $1.69/day.",
            }
        ]

        synopsis = generate_daily_brief_synopsis(mock_bq, "test-p", "test-d", alerts=alerts)
        focus_items_str = " ".join(synopsis["focus_items"])
        self.assertIn("Paycheck Sweep", focus_items_str)
        self.assertIn("Transfer $7,475.00 to First Tech HELOC", focus_items_str)

    def test_fastapi_advisor_surplus_sweep_endpoint(self):
        from fastapi.testclient import TestClient

        from app.main import app, verify_api_key

        client = TestClient(app)
        app.dependency_overrides[verify_api_key] = lambda: "test-key"

        mock_alert = {
            "type": "PAYCHECK_SURPLUS_SWEEP",
            "title": "⚡ Paycheck Sweep Opportunity: Transfer $5,000.00 to HELOC",
            "detail": "Surplus detected",
            "suggested_fix": "Transfer to HELOC",
        }

        try:
            with patch("app.main.check_paycheck_surplus_sweep", return_value=[mock_alert]):
                with patch("app.main.get_bq_client", return_value=MagicMock()):
                    resp = client.get("/advisor/surplus-sweep", headers={"X-API-Key": "test-key"})
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    self.assertEqual(data["status"], "success")
                    self.assertTrue(data["has_sweep_opportunity"])
                    self.assertEqual(len(data["alerts"]), 1)
        finally:
            app.dependency_overrides.pop(verify_api_key, None)

    def test_chat_webhook_sweep_command(self):
        from app.main import google_chat_webhook

        sample_event = {
            "type": "MESSAGE",
            "space": {"name": "spaces/test_space", "type": "DM"},
            "message": {
                "name": "spaces/test_space/messages/msg_sweep",
                "text": "/sweep",
                "sender": {"displayName": "FinSage User", "email": "user@example.com"},
            },
        }

        with patch(
            "app.main.get_paycheck_surplus_analysis", return_value="### ⚡ Paycheck Surplus Sweep\nSweep $5,000"
        ):
            with patch("app.main.get_bq_client", return_value=MagicMock()):
                res = asyncio.run(google_chat_webhook(sample_event))
                self.assertIn("text", res)
                self.assertIn("Paycheck Surplus Sweep", res["text"])

    def test_job_run_sweep(self):
        from app import job

        mock_alert = {
            "type": "PAYCHECK_SURPLUS_SWEEP",
            "title": "⚡ Paycheck Sweep Opportunity",
            "detail": "Surplus detected",
            "suggested_fix": "Sweep $2,500 to HELOC",
        }

        with patch("app.alerts.check_paycheck_surplus_sweep", return_value=[mock_alert]):
            with patch("app.alerts.resolve_secret", return_value="https://chat.googleapis.com/v1/spaces/XYZ/messages"):
                with patch("app.bq_service.get_bq_client", return_value=MagicMock()):
                    with patch("requests.post") as mock_post:
                        mock_post.return_value.status_code = 200
                        res = asyncio.run(job.run_sweep())
                        self.assertEqual(res["status"], "success")
                        self.assertTrue(res["has_sweep_opportunity"])
                        self.assertTrue(res["webhook_dispatched"])
                        mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
