"""
Unit tests for monarch_service.py.
"""
import asyncio
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import monarch_service


class TestMonarchServiceAuth(unittest.TestCase):
    def setUp(self):
        monarch_service._monarch_client = None

    @patch("monarch_service.resolve_secret")
    @patch("monarch_service.MonarchMoney")
    def test_get_monarch_client_cached(self, mock_mm_class, mock_resolve):
        existing_client = MagicMock()
        monarch_service._monarch_client = existing_client

        result = asyncio.run(monarch_service.get_monarch_client())
        self.assertIs(result, existing_client)
        mock_mm_class.assert_not_called()

    @patch("monarch_service.resolve_secret")
    @patch("monarch_service.MonarchMoney")
    def test_get_monarch_client_login_mfa_secret(self, mock_mm_class, mock_resolve):
        def fake_resolve(secret_id, env_var):
            if "email" in secret_id:
                return "test@example.com"
            if "password" in secret_id:
                return "password123"
            if "mfa" in secret_id:
                return "JBSWY3DPEHPK3PXP"
            return None

        mock_resolve.side_effect = fake_resolve
        mock_instance = MagicMock()
        mock_instance.login = AsyncMock()
        mock_mm_class.return_value = mock_instance

        client = asyncio.run(monarch_service.get_monarch_client())
        self.assertIs(client, mock_instance)
        mock_instance.login.assert_awaited_once_with(
            email="test@example.com",
            password="password123",
            mfa_secret_key="JBSWY3DPEHPK3PXP",
        )


class TestMonarchReadTools(unittest.TestCase):
    @patch("monarch_service.get_monarch_client")
    def test_get_live_account_balance_found(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value={
            "accounts": [
                {
                    "id": "acc_100",
                    "displayName": "First Tech HELOC",
                    "institution": {"name": "First Tech"},
                    "currentBalance": 45000.0,
                    "availableBalance": 55000.0,
                    "creditLimit": 100000.0,
                    "isAsset": False,
                    "updatedAt": "2026-09-10T12:00:00Z",
                },
                {
                    "id": "acc_200",
                    "displayName": "Chase Sapphire Reserve",
                    "institution": {"name": "Chase"},
                    "currentBalance": 1250.50,
                    "availableBalance": 18749.50,
                    "creditLimit": 20000.0,
                    "isAsset": False,
                    "updatedAt": "2026-09-10T11:45:00Z",
                }
            ]
        })
        mock_get_client.return_value = mock_client

        res = asyncio.run(monarch_service.get_live_account_balance_async("heloc"))
        self.assertTrue(res["found"])
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["accounts"][0]["account_id"], "acc_100")
        self.assertEqual(res["accounts"][0]["current_balance"], 45000.0)

    @patch("monarch_service.get_monarch_client")
    def test_get_live_account_balance_not_found(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value={"accounts": []})
        mock_get_client.return_value = mock_client

        res = asyncio.run(monarch_service.get_live_account_balance_async("nonexistent"))
        self.assertFalse(res["found"])
        self.assertIn("No active account", res["message"])

    @patch("monarch_service.get_monarch_client")
    def test_get_live_transaction(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_transaction_details = AsyncMock(return_value={
            "id": "txn_999",
            "date": "2026-09-09",
            "amount": -45.50,
            "merchant": {"name": "Trader Joe's"},
            "category": {"name": "Groceries"},
            "account": {"displayName": "Chase Sapphire"},
            "pending": False,
            "isRecurring": False,
            "notes": "Weekly produce",
        })
        mock_get_client.return_value = mock_client

        raw_json = monarch_service.get_live_transaction("txn_999")
        data = json.loads(raw_json)
        self.assertTrue(data["found"])
        self.assertEqual(data["transaction_id"], "txn_999")
        self.assertEqual(data["merchant_name"], "Trader Joe's")
        self.assertEqual(data["amount"], -45.50)

    @patch("monarch_service.get_monarch_client")
    def test_request_plaid_refresh_flow_and_cooldown(self, mock_get_client):
        monarch_service._PLAID_REFRESH_COOLDOWNS.clear()
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value={
            "accounts": [
                {
                    "id": "acc_chase_1",
                    "displayName": "Chase Checking",
                    "institution": {"name": "Chase"},
                },
                {
                    "id": "acc_chase_2",
                    "displayName": "Chase Freedom",
                    "institution": {"name": "Chase"},
                }
            ]
        })
        mock_client.request_accounts_refresh = AsyncMock(return_value=True)
        mock_get_client.return_value = mock_client

        # First request should trigger
        res1 = asyncio.run(monarch_service.request_plaid_refresh_async("Chase"))
        self.assertEqual(res1["status"], "triggered")
        self.assertEqual(res1["accounts_count"], 2)
        mock_client.request_accounts_refresh.assert_awaited_once_with(["acc_chase_1", "acc_chase_2"])

        # Second request immediately should be rate-limited by 60m cooldown
        res2 = asyncio.run(monarch_service.request_plaid_refresh_async("Chase"))
        self.assertEqual(res2["status"], "rate_limited")
        self.assertIn("Please wait", res2["message"])


class TestMonarchSyncIngestion(unittest.TestCase):
    @patch("monarch_service.bigquery.Client")
    def test_sync_all_accounts_and_categories(self, mock_bq_class):
        mock_bq = MagicMock()
        mock_bq.load_table_from_json.return_value.result.return_value = None

        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value={
            "accounts": [
                {
                    "id": "123",
                    "displayName": "Checking",
                    "type": {"name": "depository"},
                    "subtype": {"name": "checking"},
                    "currentBalance": 5000.0,
                    "availableBalance": 5000.0,
                    "institution": {"name": "Test Bank"},
                    "isAsset": True,
                }
            ]
        })
        mock_client.get_transaction_categories = AsyncMock(return_value={
            "categories": [
                {
                    "id": "cat_1",
                    "name": "Groceries",
                    "group": {"name": "Food & Dining"},
                    "isIncome": False,
                    "budgetAmount": 800.0,
                }
            ]
        })

        acc_count = asyncio.run(monarch_service.sync_all_accounts(mock_client, mock_bq, "2026-09-10T12:00:00Z"))
        cat_count = asyncio.run(monarch_service.sync_all_categories(mock_client, mock_bq, "2026-09-10T12:00:00Z"))

        self.assertEqual(acc_count, 1)
        self.assertEqual(cat_count, 1)
        self.assertEqual(mock_bq.load_table_from_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
