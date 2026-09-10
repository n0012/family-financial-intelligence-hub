"""
Unit tests for bq_service.py
Tests read-only SQL safety guards, Conversational Analytics, session history caching/hydration,
and schema application.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from app import bq_service


class TestRunReadonlySql(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()

    def test_forbidden_keywords_rejected(self):
        forbidden_queries = [
            "INSERT INTO `family_finance.raw_accounts` VALUES ('1')",
            "update `family_finance.raw_accounts` set apr = 0.05",
            "DELETE FROM `family_finance.raw_transactions` WHERE amount > 100",
            "DROP TABLE `family_finance.raw_categories`",
            "TRUNCATE TABLE `family_finance.staging_transactions`",
            "ALTER TABLE `family_finance.raw_accounts` ADD COLUMN test STRING",
            "create table `family_finance.temp` (id STRING)",
            "MERGE INTO `family_finance.raw_transactions` USING staging ON 1=1 WHEN MATCHED THEN UPDATE SET pending=false",
            "GRANT `roles/bigquery.admin` ON DATASET `family_finance` TO 'user@example.com'",
            "REVOKE `roles/bigquery.dataViewer` ON DATASET `family_finance` FROM 'user@example.com'",
        ]
        for query in forbidden_queries:
            result = bq_service.run_readonly_sql(query, client=self.mock_client)
            self.assertTrue(result.startswith("Error: Only SELECT queries are permitted"), f"Failed to reject: {query}")
            self.mock_client.query.assert_not_called()

    def test_legitimate_select_with_rows(self):
        mock_job = MagicMock()
        mock_job.result.return_value = [
            {"account_id": "acc-1", "current_balance": 1500.50, "display_name": "Checking"},
            {"account_id": "acc-2", "current_balance": -25000.00, "display_name": "HELOC"},
        ]
        self.mock_client.query.return_value = mock_job

        query = "SELECT account_id, current_balance, display_name FROM `family_finance.raw_accounts`"
        result = bq_service.run_readonly_sql(query, client=self.mock_client)

        self.mock_client.query.assert_called_once()
        parsed = json.loads(result)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["display_name"], "Checking")
        self.assertEqual(parsed[1]["current_balance"], -25000.00)

    def test_legitimate_select_empty_rows(self):
        mock_job = MagicMock()
        mock_job.result.return_value = []
        self.mock_client.query.return_value = mock_job

        query = "SELECT * FROM `family_finance.v_heloc_daily_cost` WHERE current_balance > 1000000"
        result = bq_service.run_readonly_sql(query, client=self.mock_client)

        self.assertEqual(result, "No rows returned from query.")

    def test_bigquery_exception_handling(self):
        self.mock_client.query.side_effect = RuntimeError("Access Denied")

        query = "SELECT * FROM `family_finance.v_active_subscriptions`"
        result = bq_service.run_readonly_sql(query, client=self.mock_client)

        self.assertTrue(result.startswith("BigQuery execution error: Access Denied"))


class TestConversationalAnalytics(unittest.TestCase):
    @patch("app.bq_service.google.auth.default")
    @patch("app.bq_service.requests.post")
    def test_ask_conversational_analytics_success(self, mock_post, mock_auth):
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "fake-token"
        mock_auth.return_value = (mock_creds, "fake-project")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "messages": [
                {
                    "systemMessage": {
                        "text": {"parts": ["Based on your subscriptions, you can save $45/mo."]},
                        "data": {"generatedSql": "SELECT * FROM v_active_subscriptions"},
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        res = bq_service.ask_conversational_analytics("What can I cut?", project_id="test-project")
        self.assertEqual(res["answer"], "Based on your subscriptions, you can save $45/mo.")
        self.assertEqual(res["sql"], "SELECT * FROM v_active_subscriptions")

    @patch("app.bq_service.google.auth.default")
    @patch("app.bq_service.requests.post")
    def test_ask_conversational_analytics_http_error(self, mock_post, mock_auth):
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.token = "fake-token"
        mock_auth.return_value = (mock_creds, "fake-project")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        res = bq_service.ask_conversational_analytics("Help", project_id="test-project")
        self.assertTrue(res["answer"].startswith("I ran into an issue analyzing that:"))
        self.assertIsNone(res["sql"])


class TestSessionHistory(unittest.TestCase):
    def setUp(self):
        bq_service.clear_session_history()
        self.mock_client = MagicMock()

    def test_in_memory_cache_hit(self):
        bq_service.THREAD_HISTORY["spaces/SPACE_1/threads/THREAD_1"] = [
            {"userMessage": {"text": "hello"}},
            {"systemMessage": {"text": {"parts": ["hi there"]}}},
        ]

        history = bq_service.get_session_history(
            thread_name="spaces/SPACE_1/threads/THREAD_1",
            space_name="spaces/SPACE_1",
            client=self.mock_client,
        )

        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["userMessage"]["text"], "hello")
        self.mock_client.query.assert_not_called()

    def test_in_memory_session_history(self):
        # Empty when not cached
        history = bq_service.get_session_history(
            thread_name="spaces/SPACE_1/threads/THREAD_EMPTY",
            space_name="spaces/SPACE_1",
            client=self.mock_client,
        )
        self.assertEqual(len(history), 0)
        self.mock_client.query.assert_not_called()

    def test_save_session_history_in_memory(self):
        success = bq_service.save_session_history(
            thread_name="spaces/SPACE_1/threads/THREAD_3",
            space_name="spaces/SPACE_1",
            user_email="user@example.com",
            user_text="Can we afford dinner out tonight?",
            model_text="Yes, you have $180 remaining in dining for this week.",
            client=self.mock_client,
        )

        self.assertTrue(success)
        # BigQuery chat_history insert is retired; no insert_rows_json call should be made
        self.mock_client.insert_rows_json.assert_not_called()

        # Check that it's present in in-memory caches
        self.assertIn("spaces/SPACE_1/threads/THREAD_3", bq_service.THREAD_HISTORY)
        self.assertIn("spaces/SPACE_1", bq_service.SPACE_HISTORY)

        # Retrieve it back
        cached = bq_service.get_session_history(
            thread_name="spaces/SPACE_1/threads/THREAD_3",
            space_name="spaces/SPACE_1",
        )
        self.assertEqual(len(cached), 2)
        self.assertEqual(cached[0]["userMessage"]["text"], "Can we afford dinner out tonight?")


class TestApplyBigQuerySchema(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()

    def test_schema_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            bq_service.apply_bigquery_schema(schema_file_path="/nonexistent/schema.sql", client=self.mock_client)

    def test_schema_file_success(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".sql") as tmp:
            tmp.write("CREATE TABLE IF NOT EXISTS `family_finance.test` (id STRING);")
            tmp_path = tmp.name

        mock_job = MagicMock()
        self.mock_client.query.return_value = mock_job

        res = bq_service.apply_bigquery_schema(schema_file_path=tmp_path, client=self.mock_client)
        self.assertEqual(res["status"], "success")
        self.mock_client.query.assert_called_once_with("CREATE TABLE IF NOT EXISTS `family_finance.test` (id STRING);")
        mock_job.result.assert_called_once()


if __name__ == "__main__":
    unittest.main()
