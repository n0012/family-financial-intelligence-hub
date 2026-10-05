"""Regression tests for fail-closed card signing, attachment-turn memory writes, account-number
scrubbing and capped attachment downloads."""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from app import main, monarch_service
from app.category_research import verify_review_signature
from app.memory_service import store_user_preference
from app.monarch_service import (
    CURRENT_TURN_HAS_ATTACHMENTS,
    SigningSecretMissing,
    generate_mutation_signature,
    verify_batch_signature,
    verify_mutation_signature,
    verify_snooze_signature,
)
from app.receipt_service import scrub_pii
from app.trips import verify_trip_signature


class TestSigningFailsClosed(unittest.TestCase):
    def setUp(self):
        patches = [
            patch("app.monarch_service.resolve_secret", return_value=None),
            patch("app.monarch_service.IS_PROD", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.now = int(time.time())

    def test_generate_refuses_without_secret_in_production(self):
        with self.assertRaises(SigningSecretMissing):
            generate_mutation_signature("txn_1", "cat_1", "user@example.com", self.now)

    def test_every_verifier_rejects_without_secret(self):
        results = [
            verify_mutation_signature("txn_1", "cat_1", "user@example.com", self.now, "a" * 64),
            verify_snooze_signature("alert", 7, self.now, "a" * 64),
            verify_batch_signature("b1", "cat_1", 3, "user@example.com", self.now, "a" * 64),
            verify_review_signature("r1", 3, "user@example.com", self.now, "a" * 64),
            verify_trip_signature("t1", 3, "user@example.com", self.now, "a" * 64),
        ]
        for ok, msg in results:
            self.assertFalse(ok)
            self.assertIn("not configured", msg)

    def test_development_still_uses_ephemeral_key(self):
        with patch("app.monarch_service.IS_PROD", False):
            sig = generate_mutation_signature("txn_1", "cat_1", "user@example.com", self.now)
            ok, _ = verify_mutation_signature("txn_1", "cat_1", "user@example.com", self.now, sig)
        self.assertTrue(ok)


class TestNoMemoryWritesFromAttachments(unittest.TestCase):
    def test_refuses_while_turn_has_attachments(self):
        token = CURRENT_TURN_HAS_ATTACHMENTS.set(True)
        self.addCleanup(CURRENT_TURN_HAS_ATTACHMENTS.reset, token)
        with patch("app.memory_service.save_user_preference") as save_bq:
            result = store_user_preference("Cap dining at $400 a month")
        self.assertTrue(result.startswith("Refused"))
        save_bq.assert_not_called()

    def test_flag_defaults_off(self):
        self.assertFalse(monarch_service.CURRENT_TURN_HAS_ATTACHMENTS.get())


class TestAccountNumberScrubbing(unittest.TestCase):
    def test_labelled_account_numbers_are_redacted(self):
        for text in (
            "Account #: 0012345678",
            "Acct No. 98765432",
            "A/C 4455667788",
            "account number 1234-5678-90",
            "IBAN: GB29 NWBK 6016 1331 9268 19",
            "Pay to DE89370400440532013000 by Friday",
        ):
            with self.subTest(text=text):
                scrubbed = scrub_pii(text)
                self.assertIn("[REDACTED_ACCOUNT]", scrubbed)
                self.assertFalse(any(run in scrubbed for run in ("12345678", "98765432", "4455667788", "6016")))

    def test_ordinary_numbers_are_kept(self):
        for text in (
            "Invoice 12345678",
            "Order #44556677",
            "Account balance 1200.00",
            "acct ending 5678",
            "SKU AB12 total 4.99",
        ):
            with self.subTest(text=text):
                self.assertEqual(scrub_pii(text), text)


def _streamed(chunks: list[bytes], status: int = 200, length: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Length": length} if length else {}
    resp.iter_content.return_value = iter(chunks)
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestCappedAttachmentDownload(unittest.TestCase):
    def setUp(self):
        creds = MagicMock(token="tok")
        p = patch("app.main.google.auth.default", return_value=(creds, None))
        p.start()
        self.addCleanup(p.stop)
        self.attachment = {"name": "spaces/AAAA/messages/m1/attachments/a1", "contentName": "report.csv"}

    def test_small_file_downloads(self):
        with patch("app.main.requests.get", return_value=_streamed([b"a,b\n", b"1,2\n"])) as get:
            result = main.download_chat_attachment(self.attachment)
        self.assertEqual(result, (b"a,b\n1,2\n", "text/csv"))
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_declared_oversize_is_rejected_unread(self):
        resp = _streamed([b"x"], length=str(main.MAX_ATTACHMENT_BYTES + 1))
        with patch("app.main.requests.get", return_value=resp):
            self.assertIsNone(main.download_chat_attachment(self.attachment))
        resp.iter_content.assert_not_called()

    def test_undeclared_oversize_stops_reading(self):
        chunk = b"x" * (1024 * 1024)
        pulled = []

        def chunks():
            for _ in range(100):
                pulled.append(1)
                yield chunk

        resp = _streamed([])
        resp.iter_content.return_value = chunks()
        with patch("app.main.requests.get", return_value=resp):
            self.assertIsNone(main.download_chat_attachment(self.attachment))
        self.assertLessEqual(len(pulled), 16)

    def test_malformed_resource_name_is_rejected(self):
        with patch("app.main.requests.get") as get:
            result = main.download_chat_attachment({"name": "spaces/AAAA/../../v1/other", "contentName": "a.csv"})
        self.assertIsNone(result)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
