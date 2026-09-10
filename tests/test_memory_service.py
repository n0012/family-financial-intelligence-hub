import unittest
from unittest.mock import MagicMock, patch

from app import memory_service
from app.monarch_service import CURRENT_USER_EMAIL


class TestMemoryService(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        CURRENT_USER_EMAIL.set("user@example.com")

    def test_resolve_user_email_explicit(self):
        email = memory_service._resolve_user_email("alice@example.com")
        self.assertEqual(email, "alice@example.com")

    def test_resolve_user_email_from_context(self):
        CURRENT_USER_EMAIL.set("bob@example.com")
        email = memory_service._resolve_user_email(None)
        self.assertEqual(email, "bob@example.com")

    def test_resolve_user_email_fallback_on_unknown(self):
        CURRENT_USER_EMAIL.set("unknown")
        email = memory_service._resolve_user_email(None)
        self.assertEqual(email, memory_service.DEFAULT_USER_EMAIL)

    def test_retrieve_user_memories_success(self):
        mock_mem1 = MagicMock()
        mock_mem1.memory.fact = "User's financial preferences: Capped dining spend at $400/month."
        mock_mem2 = MagicMock()
        mock_mem2.memory.fact = "Target HELOC payoff date: December 2026."

        mock_page = MagicMock()
        mock_page.page = [mock_mem1, mock_mem2]
        self.mock_client.memory_banks.memories.retrieve.return_value = mock_page

        memories = memory_service.retrieve_user_memories(
            user_email="user@example.com",
            client=self.mock_client,
        )

        self.assertEqual(len(memories), 2)
        self.assertIn("Capped dining spend at $400/month", memories[0])
        self.assertIn("Target HELOC payoff date", memories[1])

        self.mock_client.memory_banks.memories.retrieve.assert_called_once_with(
            name=memory_service.DEFAULT_MEMORY_BANK_NAME,
            scope={"user_id": "user@example.com"},
        )

    def test_retrieve_user_memories_error_fallback(self):
        self.mock_client.memory_banks.memories.retrieve.side_effect = RuntimeError("API unavailable")

        memories = memory_service.retrieve_user_memories(
            user_email="user@example.com",
            client=self.mock_client,
        )
        self.assertEqual(memories, [])

    def test_format_memories_for_prompt(self):
        # Empty memories
        self.assertEqual(memory_service.format_memories_for_prompt([]), "")

        # Non-empty memories
        facts = [
            "Capped dining spend at $400/month.",
            "Target HELOC payoff date: December 2026.",
        ]
        formatted = memory_service.format_memories_for_prompt(facts)
        self.assertIn("<USER_PREFERENCES_AND_MEMORY>", formatted)
        self.assertIn("- Capped dining spend at $400/month.", formatted)
        self.assertIn("- Target HELOC payoff date: December 2026.", formatted)
        self.assertIn("</USER_PREFERENCES_AND_MEMORY>", formatted)

    def test_save_user_preference_success(self):
        mock_resp = MagicMock()
        self.mock_client.memory_banks.memories.generate.return_value = mock_resp

        success = memory_service.save_user_preference(
            preference_or_rule="Capped monthly dining spend at $450.",
            user_email="user@example.com",
            client=self.mock_client,
        )

        self.assertTrue(success)
        self.mock_client.memory_banks.memories.generate.assert_called_once_with(
            name=memory_service.DEFAULT_MEMORY_BANK_NAME,
            direct_memories_source={"direct_memories": [{"fact": "Capped monthly dining spend at $450."}]},
            scope={"user_id": "user@example.com"},
        )

    def test_save_user_preference_empty_string(self):
        success = memory_service.save_user_preference("", client=self.mock_client)
        self.assertFalse(success)
        self.mock_client.memory_banks.memories.generate.assert_not_called()

        success_spaces = memory_service.save_user_preference("   ", client=self.mock_client)
        self.assertFalse(success_spaces)

    def test_save_user_preference_failure(self):
        self.mock_client.memory_banks.memories.generate.side_effect = Exception("Vertex error")
        success = memory_service.save_user_preference(
            "Some new rule",
            user_email="user@example.com",
            client=self.mock_client,
        )
        self.assertFalse(success)

    @patch("app.memory_service.save_user_preference")
    def test_store_user_preference_tool_success(self, mock_save):
        mock_save.return_value = True
        CURRENT_USER_EMAIL.set("user@example.com")

        reply = memory_service.store_user_preference("Prioritize HELOC debt payoff with all bonus income.")
        self.assertIn("Successfully saved to your long-term Memory Bank", reply)
        self.assertIn("user@example.com", reply)
        self.assertIn("Prioritize HELOC debt payoff", reply)

    @patch("app.memory_service.save_user_preference")
    def test_store_user_preference_tool_failure(self, mock_save):
        mock_save.return_value = False
        CURRENT_USER_EMAIL.set("user@example.com")

        reply = memory_service.store_user_preference("Some rule")
        self.assertIn("Could not record preference into Memory Bank", reply)
