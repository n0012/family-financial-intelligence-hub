"""
Unit and integration tests for Receipt & Tax Deductibility Ingestion Service (PR 9).
Tests multimodal parsing, Zero-PII sanitization, transaction reconciliation (-3d to +10d),
BigQuery persistence, Card v2 generation, REST endpoints, and Google Chat webhook interactions.
"""

from __future__ import annotations

import datetime
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import config
from app.main import app, verify_api_key
from app.receipt_service import (
    LineItem,
    ReceiptExtractionResult,
    build_receipt_chat_card,
    format_tax_summary_text,
    get_tax_deductible_summary,
    get_tax_deduction_analysis,
    match_receipt_to_transaction,
    parse_receipt_image,
    process_receipt_bytes,
    save_receipt_record,
    scrub_pii,
)


class TestReceiptPiiScrubbing(unittest.TestCase):
    """Zero-PII compliance tests."""

    def test_scrub_pii_none_or_empty(self):
        self.assertEqual(scrub_pii(None), "")
        self.assertEqual(scrub_pii(""), "")

    def test_scrub_ssn(self):
        text = "Patient SSN: 123-45-6789 on the prescription."
        scrubbed = scrub_pii(text)
        self.assertNotIn("123-45-6789", scrubbed)
        self.assertIn("[REDACTED_SSN]", scrubbed)

    def test_scrub_ein(self):
        text = "Charity Tax ID: 12-3456789 (501c3 exempt organization)."
        scrubbed = scrub_pii(text)
        self.assertNotIn("12-3456789", scrubbed)
        self.assertIn("[REDACTED_EIN]", scrubbed)

    def test_scrub_card_pan(self):
        text = "Payment card: 4111 2222 3333 4444 approved auth 9821."
        scrubbed = scrub_pii(text)
        self.assertNotIn("4111 2222 3333 4444", scrubbed)
        self.assertIn("[REDACTED_CARD]", scrubbed)

    def test_scrub_multiple_pii_entities(self):
        text = "SSN 987-65-4321, EIN 99-8877665, Card 5500-1122-3344-5566"
        scrubbed = scrub_pii(text)
        self.assertNotIn("987-65-4321", scrubbed)
        self.assertNotIn("99-8877665", scrubbed)
        self.assertNotIn("5500-1122-3344-5566", scrubbed)
        self.assertIn("[REDACTED_SSN]", scrubbed)
        self.assertIn("[REDACTED_EIN]", scrubbed)
        self.assertIn("[REDACTED_CARD]", scrubbed)


class TestReceiptParsingAndExtraction(unittest.TestCase):
    """Multimodal extraction & confidence gating tests."""

    def test_parse_receipt_fallback_when_no_client(self):
        res = parse_receipt_image(b"fake_image_bytes", client=None)
        self.assertFalse(res.is_tax_deductible)
        self.assertEqual(res.audit_status, "NEEDS_REVIEW")
        self.assertEqual(res.tax_category, "STANDARD_NON_DEDUCTIBLE")

    def test_parse_receipt_verified_deductible(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "merchant_name": "Dr. Smith Orthodontics",
                "receipt_date": "2026-04-10",
                "total_amount": 250.0,
                "deductible_amount": 250.0,
                "tax_category": "HSA_FSA_ELIGIBLE",
                "is_tax_deductible": True,
                "deductibility_confidence": 0.95,
                "tax_justification": "IRC Sec. 213(d) qualified medical dental treatment",
                "audit_status": "VERIFIED",
                "line_items": [{"description": "Dental Exam & Cleaning", "amount": 250.0, "is_deductible": True}],
            }
        )
        mock_client.models.generate_content.return_value = mock_response

        res = parse_receipt_image(b"mock_bytes", client=mock_client)
        self.assertTrue(res.is_tax_deductible)
        self.assertEqual(res.audit_status, "VERIFIED")
        self.assertEqual(res.deductible_amount, 250.0)
        self.assertEqual(res.tax_category, "HSA_FSA_ELIGIBLE")

    def test_parse_receipt_needs_review_on_low_confidence(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "merchant_name": "Target Store #1024",
                "receipt_date": "2026-05-15",
                "total_amount": 85.0,
                "deductible_amount": 35.0,
                "tax_category": "SCHEDULE_C_EXPENSE",
                "is_tax_deductible": True,
                "deductibility_confidence": 0.70,  # Below 0.85 threshold
                "tax_justification": "Possible office equipment mixed basket",
                "audit_status": "VERIFIED",  # Model tried to claim verified, gate should override
                "line_items": [
                    {"description": "Printer Paper & Ink", "amount": 35.0, "is_deductible": True},
                    {"description": "Snacks & Soda", "amount": 50.0, "is_deductible": False},
                ],
            }
        )
        mock_client.models.generate_content.return_value = mock_response

        res = parse_receipt_image(b"mock_bytes", client=mock_client)
        self.assertTrue(res.is_tax_deductible)
        self.assertEqual(res.audit_status, "NEEDS_REVIEW")
        self.assertEqual(res.deductible_amount, 35.0)

    def test_parse_receipt_standard_non_deductible(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps(
            {
                "merchant_name": "Whole Foods Market",
                "receipt_date": "2026-06-01",
                "total_amount": 112.50,
                "deductible_amount": 0.0,
                "tax_category": "STANDARD_NON_DEDUCTIBLE",
                "is_tax_deductible": False,
                "deductibility_confidence": 0.99,
                "tax_justification": "Personal food groceries non-deductible",
                "audit_status": "VERIFIED",
                "line_items": [{"description": "Groceries", "amount": 112.50, "is_deductible": False}],
            }
        )
        mock_client.models.generate_content.return_value = mock_response

        res = parse_receipt_image(b"mock_bytes", client=mock_client)
        self.assertFalse(res.is_tax_deductible)
        self.assertEqual(res.audit_status, "VERIFIED")
        self.assertEqual(res.deductible_amount, 0.0)


class TestReceiptTransactionMatching(unittest.TestCase):
    """Reconciliation tests with asymmetric -3d to +10d window and tip support."""

    def test_match_exact_total(self):
        mock_bq = MagicMock()
        mock_row = SimpleNamespace(
            transaction_id="tx_12345",
            account_id="acc_chk1",
            account_name="Primary Checking",
            transaction_date=datetime.date(2026, 7, 12),
            amount=-64.32,
            merchant_name="Home Depot #0452",
            clean_merchant_name="Home Depot",
            category_name="Home Improvement",
            pending=False,
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        matched = match_receipt_to_transaction(
            bq=mock_bq,
            project_id="test-proj",
            dataset_id="test-ds",
            merchant_name="The Home Depot",
            receipt_date="2026-07-10",
            total_amount=64.32,
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["transaction_id"], "tx_12345")
        self.assertEqual(matched["amount"], -64.32)
        self.assertIn("Home Depot", matched["merchant_name"])

    def test_match_pre_tip_authorization(self):
        mock_bq = MagicMock()
        mock_row = SimpleNamespace(
            transaction_id="tx_dining_99",
            account_id="acc_cc_blue",
            account_name="Sapphire Card",
            transaction_date=datetime.date(2026, 8, 1),
            amount=-50.00,  # Pre-tip subtotal
            merchant_name="Bistro Central",
            clean_merchant_name="Bistro Central",
            category_name="Restaurants",
            pending=True,
        )
        mock_bq.query.return_value.result.return_value = [mock_row]

        matched = match_receipt_to_transaction(
            bq=mock_bq,
            project_id="test-proj",
            dataset_id="test-ds",
            merchant_name="Bistro Central",
            receipt_date="2026-08-01",
            total_amount=62.50,  # Total with tip
            tip_amount=12.50,  # 50.00 subtotal
        )

        self.assertIsNotNone(matched)
        self.assertEqual(matched["transaction_id"], "tx_dining_99")
        self.assertEqual(matched["amount"], -50.00)

    def test_match_no_transaction_found(self):
        mock_bq = MagicMock()
        mock_bq.query.return_value.result.return_value = []

        matched = match_receipt_to_transaction(
            bq=mock_bq,
            project_id="test-proj",
            dataset_id="test-ds",
            merchant_name="Obscure Vendor",
            receipt_date="2026-01-01",
            total_amount=99.99,
        )
        self.assertIsNone(matched)


class TestReceiptPersistenceAndCard(unittest.TestCase):
    """BigQuery insertion and Card v2 formatting tests."""

    def test_save_receipt_record(self):
        mock_bq = MagicMock()
        record = ReceiptExtractionResult(
            merchant_name="CVS Pharmacy #4092",
            receipt_date="2026-09-02",
            total_amount=45.20,
            deductible_amount=35.00,
            tax_category="HSA_FSA_ELIGIBLE",
            is_tax_deductible=True,
            deductibility_confidence=0.92,
            tax_justification="IRC Sec. 213(d) prescription copay",
            audit_status="VERIFIED",
            line_items=[
                LineItem(description="Rx Refill Copay", amount=35.00, is_deductible=True),
                LineItem(description="Bottled Water", amount=10.20, is_deductible=False),
            ],
            notes="Card SSN: 123-45-6789 paid in store.",
        )

        rcpt_id = save_receipt_record(
            bq=mock_bq,
            project_id="test-proj",
            dataset_id="test-ds",
            receipt_data=record,
            matched_transaction_id="tx_cvs_01",
            user_email="test@family.internal",
        )

        self.assertIsNotNone(rcpt_id)
        self.assertTrue(rcpt_id.startswith("rcpt_"))
        self.assertTrue(mock_bq.query.called)
        # Check job config parameter sanitization
        call_kwargs = mock_bq.query.call_args[1]
        job_config = call_kwargs.get("job_config")
        params = {p.name: p.value for p in job_config.query_parameters}
        self.assertNotIn("123-45-6789", params["notes"])
        self.assertIn("[REDACTED_SSN]", params["notes"])
        self.assertEqual(params["deductible_amount"], 35.00)
        self.assertEqual(params["matched_transaction_id"], "tx_cvs_01")

    def test_build_receipt_chat_card_verified(self):
        record = {
            "merchant_name": "Quest Diagnostics",
            "receipt_date": "2026-03-14",
            "total_amount": 140.0,
            "deductible_amount": 140.0,
            "tax_category": "HSA_FSA_ELIGIBLE",
            "is_tax_deductible": True,
            "deductibility_confidence": 0.98,
            "tax_justification": "IRC Sec. 213(d) diagnostic blood panel",
            "audit_status": "VERIFIED",
            "line_items": [{"description": "Comprehensive Metabolic Panel", "amount": 140.0, "is_deductible": True}],
        }
        matched_tx = {
            "transaction_id": "tx_quest_1",
            "amount": -140.0,
            "transaction_date": "2026-03-15",
            "merchant_name": "Quest Diagnostics",
            "account_name": "HSA Spending Account",
        }

        card_dict = build_receipt_chat_card(record, receipt_id="rcpt_test1", matched_tx=matched_tx)
        self.assertIn("card", card_dict)
        header = card_dict["card"]["header"]
        self.assertIn("Quest Diagnostics", header["title"])
        sections = card_dict["card"]["sections"]
        widgets = sections[0]["widgets"]
        card_str = json.dumps(widgets)
        self.assertIn("HSA / FSA ELIGIBLE", card_str)
        self.assertIn("Verified Deductible", card_str)
        self.assertIn("Matched: Quest Diagnostics", card_str)

    def test_build_receipt_chat_card_needs_review(self):
        record = {
            "merchant_name": "Amazon Business",
            "receipt_date": "2026-03-20",
            "total_amount": 180.0,
            "deductible_amount": 180.0,
            "tax_category": "SCHEDULE_C_EXPENSE",
            "is_tax_deductible": True,
            "deductibility_confidence": 0.72,
            "tax_justification": "IRC Sec. 162 home office equipment",
            "audit_status": "NEEDS_REVIEW",
        }

        card_dict = build_receipt_chat_card(record, receipt_id="rcpt_rev1", matched_tx=None)
        card_str = json.dumps(card_dict)
        self.assertIn("Needs Review", card_str)
        self.assertIn("No matching bank debit found", card_str)


class TestTaxSummaryQueriesAndFormatting(unittest.TestCase):
    """View aggregation and markdown reporting tests."""

    def test_get_tax_deductible_summary(self):
        mock_bq = MagicMock()
        mock_row1 = SimpleNamespace(
            tax_year=2026,
            tax_category="HSA_FSA_ELIGIBLE",
            receipt_count=12,
            total_deductible_amount=1450.50,
            total_gross_receipt_amount=1600.00,
            matched_transaction_count=11,
            pending_review_count=1,
            sample_merchants=["CVS", "Walgreens", "Quest Diagnostics"],
        )
        mock_row2 = SimpleNamespace(
            tax_year=2026,
            tax_category="SCHEDULE_C_EXPENSE",
            receipt_count=5,
            total_deductible_amount=820.00,
            total_gross_receipt_amount=820.00,
            matched_transaction_count=5,
            pending_review_count=0,
            sample_merchants=["AWS", "Google Cloud", "GitHub"],
        )
        mock_bq.query.return_value.result.return_value = [mock_row1, mock_row2]

        summary = get_tax_deductible_summary(mock_bq, "test-p", "test-d", 2026)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["tax_category"], "HSA_FSA_ELIGIBLE")
        self.assertEqual(summary[0]["total_deductible_amount"], 1450.50)

    def test_format_tax_summary_text_empty(self):
        text = format_tax_summary_text([], 2026)
        self.assertIn("No tax-deductible receipts recorded yet for 2026", text)

    def test_format_tax_summary_text_populated(self):
        rows = [
            {
                "tax_year": 2026,
                "tax_category": "HSA_FSA_ELIGIBLE",
                "receipt_count": 8,
                "total_deductible_amount": 1200.00,
                "total_gross_receipt_amount": 1250.00,
                "matched_transaction_count": 7,
                "pending_review_count": 1,
                "sample_merchants": ["CVS", "Doctor Copay"],
            },
            {
                "tax_year": 2026,
                "tax_category": "CHARITABLE_DONATION",
                "receipt_count": 2,
                "total_deductible_amount": 500.00,
                "total_gross_receipt_amount": 500.00,
                "matched_transaction_count": 2,
                "pending_review_count": 0,
                "sample_merchants": ["St Jude", "Red Cross"],
            },
        ]
        formatted = format_tax_summary_text(rows, 2026)
        self.assertIn("Tax Year 2026 Deduction Summary", formatted)
        self.assertIn("$1,700.00", formatted)
        self.assertIn("HSA / FSA ELIGIBLE", formatted)
        self.assertIn("CHARITABLE DONATION", formatted)
        self.assertIn("1 item(s) flagged for manual review", formatted)

    def test_get_tax_deduction_analysis_wrapper(self):
        with (
            patch("app.bq_service.get_bq_client") as mock_get_bq,
            patch(
                "app.receipt_service.get_tax_deductible_summary",
                return_value=[
                    {
                        "tax_year": 2026,
                        "tax_category": "SCHEDULE_C_EXPENSE",
                        "receipt_count": 3,
                        "total_deductible_amount": 450.0,
                        "total_gross_receipt_amount": 450.0,
                        "matched_transaction_count": 3,
                        "pending_review_count": 0,
                        "sample_merchants": ["Vercel", "DigitalOcean"],
                    }
                ],
            ),
        ):
            mock_bq = MagicMock()
            mock_get_bq.return_value = mock_bq

            result = get_tax_deduction_analysis(2026)
            self.assertIn("Tax Year 2026 Deduction Summary", result)
            self.assertIn("$450.00", result)


class TestReceiptPipelineAndEndpoints(unittest.TestCase):
    """FastAPI REST endpoints and process_receipt_bytes pipeline tests."""

    def setUp(self):
        self.client = TestClient(app)
        app.dependency_overrides[verify_api_key] = lambda: "test-valid-key"

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_process_receipt_bytes_pipeline(self):
        mock_extraction = ReceiptExtractionResult(
            merchant_name="Staples",
            receipt_date="2026-06-20",
            total_amount=95.0,
            deductible_amount=95.0,
            tax_category="SCHEDULE_C_EXPENSE",
            is_tax_deductible=True,
            deductibility_confidence=0.94,
            tax_justification="IRC Sec. 162 business office supplies",
            audit_status="VERIFIED",
        )
        mock_matched = {
            "transaction_id": "tx_staples_99",
            "amount": -95.0,
            "transaction_date": "2026-06-21",
            "merchant_name": "Staples #100",
            "account_name": "Business Checking",
        }

        with (
            patch("app.receipt_service.parse_receipt_image", return_value=mock_extraction),
            patch("app.receipt_service.match_receipt_to_transaction", return_value=mock_matched),
            patch("app.receipt_service.save_receipt_record", return_value="rcpt_pipe_123"),
        ):
            mock_bq = MagicMock()
            out = process_receipt_bytes(b"sample_bytes", mime_type="image/png", bq=mock_bq)

            self.assertEqual(out["receipt_id"], "rcpt_pipe_123")
            self.assertEqual(out["extraction"]["merchant_name"], "Staples")
            self.assertEqual(out["matched_transaction"]["transaction_id"], "tx_staples_99")
            self.assertIn("card", out["chat_card"])

    def test_endpoint_upload_receipt(self):
        mock_result = {
            "receipt_id": "rcpt_rest_123",
            "extraction": {
                "merchant_name": "Walgreens",
                "total_amount": 50.0,
                "deductible_amount": 50.0,
                "tax_category": "HSA_FSA_ELIGIBLE",
                "is_tax_deductible": True,
            },
            "matched_transaction": None,
            "chat_card": {"cardId": "receipt_rcpt_rest_123"},
        }

        with (
            patch("app.main.get_bq_client"),
            patch("app.main.process_receipt_bytes", return_value=mock_result),
        ):
            files = {"file": ("receipt.png", b"sample_png_bytes", "image/png")}
            resp = self.client.post("/advisor/receipts/upload", files=files, headers={"X-API-Key": "test-valid-key"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["receipt_id"], "rcpt_rest_123")
            self.assertEqual(data["extraction"]["merchant_name"], "Walgreens")

    def test_endpoint_tax_summary(self):
        mock_rows = [
            {
                "tax_year": 2026,
                "tax_category": "HSA_FSA_ELIGIBLE",
                "receipt_count": 4,
                "total_deductible_amount": 600.0,
                "total_gross_receipt_amount": 600.0,
                "matched_transaction_count": 4,
                "pending_review_count": 0,
                "sample_merchants": ["CVS"],
            }
        ]

        with (
            patch("app.main.get_bq_client"),
            patch("app.main.get_tax_deductible_summary", return_value=mock_rows),
        ):
            resp = self.client.get("/advisor/tax-summary?tax_year=2026", headers={"X-API-Key": "test-valid-key"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["tax_year"], 2026)
            self.assertEqual(len(data["summary"]), 1)
            self.assertIn("HSA / FSA ELIGIBLE", data["formatted_text"])


class TestGoogleChatReceiptWebhookAndJob(unittest.TestCase):
    """Google Chat Webhook attachment routing and CLI batch execution tests."""

    def setUp(self):
        config.clear_secret_cache()
        from app.main import verify_chat_origin

        app.dependency_overrides[verify_chat_origin] = lambda: True

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_chat_webhook_tax_command(self):
        client = TestClient(app)
        event = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/123",
                "text": "@FinSage /tax 2026",
                "sender": {"displayName": "FinSage User", "email": "test@family.internal"},
                "space": {"name": "spaces/AAA"},
            },
        }

        with patch("app.main.get_tax_deduction_analysis", return_value="📋 Tax Year 2026 Deduction Summary"):
            resp = client.post("/chat/event", json=event, headers={"X-API-Key": "test-key"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("Tax Year 2026 Deduction Summary", data["text"])

    def test_chat_webhook_receipt_attachment(self):
        client = TestClient(app)
        event = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/456",
                "text": "@FinSage /receipt",
                "sender": {"displayName": "FinSage User", "email": "test@family.internal"},
                "space": {"name": "spaces/AAA"},
                "attachment": [{"contentName": "receipt.jpg", "contentType": "image/jpeg", "name": "media/12345"}],
            },
        }

        mock_process = {
            "receipt_id": "rcpt_chat_77",
            "extraction": {
                "merchant_name": "Target",
                "total_amount": 45.0,
                "deductible_amount": 25.0,
                "is_tax_deductible": True,
            },
            "matched_transaction": None,
            "chat_card": {
                "card": {
                    "header": {"title": "🧾 Receipt: Target"},
                    "sections": [],
                }
            },
        }

        with (
            patch("app.main.download_chat_attachment", return_value=(b"fake_jpg", "image/jpeg")),
            patch("app.main.get_bq_client"),
            patch("app.main.process_receipt_bytes", return_value=mock_process),
        ):
            resp = client.post("/chat/event", json=event, headers={"X-API-Key": "test-key"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("Target ($45.00)", data["text"])
            self.assertIn("cardsV2", data)

    def test_job_tax_summary(self):
        from app.job import run_tax_summary

        mock_rows = [
            {
                "tax_year": 2026,
                "tax_category": "HSA_FSA_ELIGIBLE",
                "receipt_count": 3,
                "total_deductible_amount": 350.0,
                "total_gross_receipt_amount": 350.0,
                "matched_transaction_count": 3,
                "pending_review_count": 0,
                "sample_merchants": ["Walgreens"],
            }
        ]

        with (
            patch("app.bq_service.get_bq_client"),
            patch("app.receipt_service.get_tax_deductible_summary", return_value=mock_rows),
        ):
            import asyncio

            result = asyncio.run(run_tax_summary(2026))
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["tax_year"], 2026)
            self.assertEqual(len(result["summary"]), 1)


if __name__ == "__main__":
    unittest.main()
