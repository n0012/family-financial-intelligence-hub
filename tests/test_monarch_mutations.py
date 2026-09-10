"""
Unit tests for PR 4 Monarch Money mutation guards, HMAC signing, and confirmation cards.
"""

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app import main, monarch_service
from app.monarch_service import (
    HMAC_EXPIRATION_SECONDS,
    execute_guarded_recategorization,
    extract_card_action_parameters,
    generate_mutation_signature,
    propose_transaction_recategorization_async,
    resolve_category,
    verify_mutation_signature,
)


class TestMonarchMutations(unittest.TestCase):
    def test_hmac_signature_generation_and_verification(self):
        txn_id = "txn_12345"
        cat_id = "cat_999"
        user_email = "user@example.com"
        now_ts = int(datetime.now(UTC).timestamp())

        sig = generate_mutation_signature(txn_id, cat_id, user_email, now_ts)
        self.assertIsInstance(sig, str)
        self.assertEqual(len(sig), 64)

        # Valid verification
        is_valid, msg = verify_mutation_signature(txn_id, cat_id, user_email, now_ts, sig)
        self.assertTrue(is_valid)
        self.assertEqual(msg, "Valid")

    def test_hmac_signature_tamper_detection(self):
        txn_id = "txn_12345"
        cat_id = "cat_999"
        user_email = "user@example.com"
        now_ts = int(datetime.now(UTC).timestamp())

        sig = generate_mutation_signature(txn_id, cat_id, user_email, now_ts)

        # Tampered transaction ID
        is_valid, msg = verify_mutation_signature("txn_99999", cat_id, user_email, now_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("mismatch", msg.lower())

        # Tampered category ID
        is_valid, msg = verify_mutation_signature(txn_id, "cat_other", user_email, now_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("mismatch", msg.lower())

        # Tampered user email
        is_valid, msg = verify_mutation_signature(txn_id, cat_id, "other@domain.com", now_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("mismatch", msg.lower())

    def test_hmac_signature_expiration(self):
        txn_id = "txn_12345"
        cat_id = "cat_999"
        user_email = "user@example.com"
        old_ts = int(datetime.now(UTC).timestamp()) - (HMAC_EXPIRATION_SECONDS + 60)

        sig = generate_mutation_signature(txn_id, cat_id, user_email, old_ts)
        is_valid, msg = verify_mutation_signature(txn_id, cat_id, user_email, old_ts, sig)
        self.assertFalse(is_valid)
        self.assertIn("expired", msg.lower())

    def test_resolve_category_matching(self):
        monarch_service._CATEGORY_CACHE = {
            "by_id": {
                "cat_1": {"id": "cat_1", "name": "Groceries", "group": "Food"},
                "cat_2": {"id": "cat_2", "name": "Restaurants & Dining", "group": "Food"},
            },
            "by_name": {
                "groceries": {"id": "cat_1", "name": "Groceries", "group": "Food"},
                "restaurants & dining": {"id": "cat_2", "name": "Restaurants & Dining", "group": "Food"},
                "restaurantsdining": {"id": "cat_2", "name": "Restaurants & Dining", "group": "Food"},
            },
            "last_fetched": datetime.now(UTC).timestamp(),
        }

        # Exact ID match
        res_id = asyncio.run(resolve_category("cat_1"))
        self.assertIsNotNone(res_id)
        self.assertEqual(res_id["name"], "Groceries")

        # Exact name case-insensitive
        res_name = asyncio.run(resolve_category("groceries"))
        self.assertIsNotNone(res_name)
        self.assertEqual(res_name["id"], "cat_1")

        # Fuzzy/normalized match
        res_fuzzy = asyncio.run(resolve_category("Restaurants & Dining"))
        self.assertIsNotNone(res_fuzzy)
        self.assertEqual(res_fuzzy["id"], "cat_2")

        # Non-existent
        res_none = asyncio.run(resolve_category("Cryptocurrency Trading"))
        self.assertIsNone(res_none)

    def test_propose_recategorization_bulk_guard(self):
        res = asyncio.run(propose_transaction_recategorization_async("123, 456", "Groceries"))
        self.assertEqual(res["status"], "error")
        self.assertIn("bulk", res["message"].lower())

        res_space = asyncio.run(propose_transaction_recategorization_async("123 456", "Groceries"))
        self.assertEqual(res_space["status"], "error")
        self.assertIn("bulk", res_space["message"].lower())

    def test_propose_recategorization_pending_guard(self):
        with patch("app.monarch_service.get_live_transaction_async") as mock_get_txn:
            mock_get_txn.return_value = {
                "found": True,
                "transaction_id": "txn_pending_1",
                "merchant_name": "Trader Joe's",
                "amount": 54.20,
                "category_name": "Uncategorized",
                "pending": True,
            }

            res = asyncio.run(propose_transaction_recategorization_async("txn_pending_1", "Groceries"))
            self.assertEqual(res["status"], "error")
            self.assertIn("pending", res["message"].lower())
            self.assertIn("refused", res["message"].lower())

    def test_propose_recategorization_success_and_card(self):
        monarch_service._CATEGORY_CACHE = {
            "by_id": {"cat_1": {"id": "cat_1", "name": "Groceries", "group": "Food"}},
            "by_name": {"groceries": {"id": "cat_1", "name": "Groceries", "group": "Food"}},
            "last_fetched": datetime.now(UTC).timestamp(),
        }

        with (
            patch("app.monarch_service.get_live_transaction_async") as mock_get_txn,
            patch("app.monarch_service.get_monarch_client") as mock_get_client,
        ):
            mock_get_txn.return_value = {
                "found": True,
                "transaction_id": "txn_posted_1",
                "merchant_name": "Trader Joe's",
                "amount": 54.20,
                "date": "2026-03-01",
                "category_name": "Shopping",
                "pending": False,
            }
            mock_get_client.return_value = MagicMock()

            res = asyncio.run(propose_transaction_recategorization_async("txn_posted_1", "Groceries"))
            self.assertEqual(res["status"], "confirmation_required")
            self.assertEqual(res["transaction_id"], "txn_posted_1")
            self.assertEqual(res["proposed_category"], "Groceries")
            self.assertIn("card", res)

            card = res["card"]
            self.assertIn("cardId", card)
            self.assertIn("sections", card["card"])
            buttons = card["card"]["sections"][0]["widgets"][-1]["buttonList"]["buttons"]
            self.assertEqual(len(buttons), 2)
            self.assertEqual(buttons[0]["text"], "Confirm Update")
            self.assertEqual(buttons[1]["text"], "Cancel")

    def test_execute_guarded_recategorization(self):
        mock_client = AsyncMock()
        mock_client.update_transaction = AsyncMock(return_value={"id": "txn_100", "categoryId": "cat_1"})

        mock_bq = MagicMock()
        mock_bq_query_job = MagicMock()
        mock_bq_query_job.result = MagicMock()
        mock_bq.query.return_value = mock_bq_query_job

        with (
            patch("app.monarch_service.get_monarch_client", AsyncMock(return_value=mock_client)),
            patch("app.monarch_service.get_bq_client", return_value=mock_bq),
        ):
            result = asyncio.run(execute_guarded_recategorization("txn_100", "cat_1", "Groceries"))
            self.assertTrue(result["success"])
            self.assertEqual(result["transaction_id"], "txn_100")
            mock_client.update_transaction.assert_awaited_once_with(
                transaction_id="txn_100",
                category_id="cat_1",
            )

    def test_extract_card_action_parameters_variations(self):
        # 1. Google Workspace Add-on shape
        addon_payload = {
            "commonEventObject": {
                "invokedFunction": "confirm_recategorize",
                "parameters": {
                    "transaction_id": "txn_555",
                    "category_id": "cat_777",
                },
            }
        }
        action, params = extract_card_action_parameters(addon_payload)
        self.assertEqual(action, "confirm_recategorize")
        self.assertEqual(params["transaction_id"], "txn_555")
        self.assertEqual(params["category_id"], "cat_777")

        # 2. Google Chat direct API shape (list of key/values)
        chat_payload = {
            "action": {
                "actionMethodName": "cancel_recategorize",
                "parameters": [
                    {"key": "transaction_id", "value": "txn_555"},
                ],
            }
        }
        action2, params2 = extract_card_action_parameters(chat_payload)
        self.assertEqual(action2, "cancel_recategorize")
        self.assertEqual(params2["transaction_id"], "txn_555")

    def test_webhook_card_clicked_confirm_success(self):
        now_ts = int(datetime.now(UTC).timestamp())
        sig = generate_mutation_signature("txn_777", "cat_888", "user@example.com", now_ts)
        payload = {
            "type": "CARD_CLICKED",
            "chat": {"user": {"email": "user@example.com", "displayName": "User"}},
            "commonEventObject": {
                "invokedFunction": "confirm_recategorize",
                "parameters": {
                    "transaction_id": "txn_777",
                    "category_id": "cat_888",
                    "category_name": "Groceries",
                    "timestamp": str(now_ts),
                    "user_email": "user@example.com",
                    "signature": sig,
                },
            },
        }
        with patch(
            "app.main.execute_guarded_recategorization",
            AsyncMock(return_value={"success": True, "transaction_id": "txn_777"}),
        ):
            resp = asyncio.run(main.google_chat_webhook(payload))
            msg = resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
            self.assertIn("successfully reclassified", msg["text"])
            self.assertIn("cardsV2", msg)
            self.assertEqual(msg["cardsV2"][0]["cardId"], "recat_success_txn_777")

    def test_webhook_card_clicked_tampered_signature_rejected(self):
        now_ts = int(datetime.now(UTC).timestamp())
        payload = {
            "type": "CARD_CLICKED",
            "chat": {"user": {"email": "user@example.com", "displayName": "User"}},
            "commonEventObject": {
                "invokedFunction": "confirm_recategorize",
                "parameters": {
                    "transaction_id": "txn_777",
                    "category_id": "cat_888",
                    "category_name": "Groceries",
                    "timestamp": str(now_ts),
                    "user_email": "user@example.com",
                    "signature": "invalid_forged_signature_hex",
                },
            },
        }
        resp = asyncio.run(main.google_chat_webhook(payload))
        msg = resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        self.assertIn("rejected", msg["text"].lower())

    def test_webhook_card_clicked_cancel(self):
        payload = {
            "type": "CARD_CLICKED",
            "commonEventObject": {
                "invokedFunction": "cancel_recategorize",
                "parameters": {
                    "transaction_id": "txn_999",
                },
            },
        }
        resp = asyncio.run(main.google_chat_webhook(payload))
        msg = resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]
        self.assertIn("cancelled", msg["text"].lower())


if __name__ == "__main__":
    unittest.main()
