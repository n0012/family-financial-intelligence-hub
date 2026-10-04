import asyncio
import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app import category_research as cr
from app import main
from app.monarch_service import extract_card_form_values

CATEGORIES = [
    {"category_id": "c_coffee", "category_name": "Coffee Shops", "group_name": "Food & Dining", "is_income": False},
    {"category_id": "c_rest", "category_name": "Restaurants & Bars", "group_name": "Food & Dining", "is_income": False},
    {"category_id": "c_subs", "category_name": "Subscriptions", "group_name": "Bills", "is_income": False},
    {"category_id": "c_pay", "category_name": "Paychecks", "group_name": "Income", "is_income": True},
]


def _cand(merchant, cats, n=4, typical=12.0):
    return {
        "merchant": merchant,
        "txn_count": n,
        "total_amount": typical * n,
        "typical_amount": typical,
        "uncategorized_count": cats.get("Uncategorized", 0),
        "is_recurring": False,
        "categories": [{"category_name": k, "n": v} for k, v in cats.items()],
    }


class TestMemory(unittest.TestCase):
    def test_split_by_memory(self):
        now = datetime(2026, 6, 1, tzinfo=UTC)
        cands = [
            _cand("Corner Cafe", {"Uncategorized": 3}),
            _cand("Burger Barn", {"Uncategorized": 2}),
            _cand("Stream Co", {"Uncategorized": 1}),
            _cand("Old No", {"Uncategorized": 1}),
            _cand("Unsure Shop", {"Uncategorized": 1}),
            _cand("Brand New", {"Uncategorized": 1}),
        ]
        decisions = [
            {
                "merchant": "corner cafe",
                "category_id": "c_coffee",
                "category_name": "Coffee Shops",
                "decision": "ACCEPTED",
                "decided_at": now - timedelta(days=400),
            },
            {"merchant": "Burger Barn", "decision": "REJECTED", "decided_at": now - timedelta(days=10)},
            {"merchant": "Old No", "decision": "REJECTED", "decided_at": now - timedelta(days=365)},
            {"merchant": "Unsure Shop", "decision": "AI_SKIPPED", "decided_at": now - timedelta(days=5)},
        ]
        rule_based, to_research = cr.split_by_memory(cands, decisions, now)
        self.assertEqual([r["merchant"] for r in rule_based], ["Corner Cafe"])
        self.assertEqual(rule_based[0]["category_id"], "c_coffee")
        self.assertEqual(rule_based[0]["source"], "rule")
        self.assertEqual([c["merchant"] for c in to_research], ["Stream Co", "Old No", "Brand New"])


class TestCandidates(unittest.TestCase):
    def test_bigquery_decimals_become_json_safe(self):
        from decimal import Decimal

        row = MagicMock()
        row.items.return_value = {
            "merchant": "Corner Cafe",
            "txn_count": 3,
            "total_amount": Decimal("36.00"),
            "typical_amount": Decimal("12.00"),
            "uncategorized_count": 3,
            "is_recurring": False,
            "categories": [{"category_name": "Uncategorized", "n": 3}],
        }.items()
        bq = MagicMock()
        bq.query.return_value.result.return_value = [row]
        cands = cr.find_review_candidates(bq)
        self.assertEqual(cands[0]["typical_amount"], 12.0)
        json.dumps(cands)
        cr.build_research_prompt(cands, CATEGORIES, [])


class TestResearch(unittest.TestCase):
    def test_prompt_lists_spend_categories_and_never_totals(self):
        prompt = cr.build_research_prompt([_cand("SQ *CORNER CAFE", {"Uncategorized": 3})], CATEGORIES, [])
        self.assertIn("Coffee Shops", prompt)
        self.assertNotIn("Paychecks", prompt)
        self.assertIn("SQ *CORNER CAFE", prompt)
        self.assertNotIn("total_amount", prompt)
        self.assertIn("merchant name only", prompt)

    def test_research_chunk_uses_search_grounding_and_schema(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = json.dumps(
            {
                "suggestions": [
                    {
                        "merchant": "A",
                        "action": "recategorize",
                        "category": "Coffee Shops",
                        "confidence": 0.9,
                        "reason": "Cafe.",
                    }
                ]
            }
        )
        out = cr.research_chunk([_cand("A", {"Uncategorized": 1})], CATEGORIES, [], client)
        self.assertEqual(out[0]["category"], "Coffee Shops")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNotNone(config.tools[0].google_search)
        self.assertEqual(config.response_mime_type, "application/json")

    def test_research_chunk_survives_bad_json(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = "not json"
        self.assertEqual(cr.research_chunk([_cand("A", {"Uncategorized": 1})], CATEGORIES, [], client), [])

    def test_validate_suggestions(self):
        cands = [
            _cand("Corner Cafe", {"Uncategorized": 3}),
            _cand("Made Up", {"Uncategorized": 1}),
            _cand("Maybe Diner", {"Uncategorized": 1}),
            _cand("Big Box", {"Groceries": 5, "Shopping": 4}),
            _cand("Already Right", {"Coffee Shops": 4}),
            _cand("Paycheck Co", {"Uncategorized": 1}),
            _cand("No Answer", {"Uncategorized": 1}),
        ]
        sugg = [
            {
                "merchant": "corner cafe",
                "action": "recategorize",
                "category": "coffee shops",
                "confidence": 0.95,
                "reason": "A cafe.",
            },
            {
                "merchant": "Made Up",
                "action": "recategorize",
                "category": "Teleportation",
                "confidence": 0.99,
                "reason": "?",
            },
            {
                "merchant": "Maybe Diner",
                "action": "recategorize",
                "category": "Restaurants & Bars",
                "confidence": 0.5,
                "reason": "Maybe.",
            },
            {
                "merchant": "Big Box",
                "action": "leave",
                "category": "Groceries",
                "confidence": 0.9,
                "reason": "Sells both.",
            },
            {
                "merchant": "Already Right",
                "action": "recategorize",
                "category": "Coffee Shops",
                "confidence": 0.99,
                "reason": "Cafe.",
            },
            {
                "merchant": "Paycheck Co",
                "action": "recategorize",
                "category": "Paychecks",
                "confidence": 0.99,
                "reason": "Income categories are never offered.",
            },
        ]
        accepted, skipped = cr.validate_suggestions(cands, sugg, CATEGORIES)
        self.assertEqual([a["merchant"] for a in accepted], ["Corner Cafe"])
        self.assertEqual(accepted[0]["category_id"], "c_coffee")
        self.assertEqual(accepted[0]["category_name"], "Coffee Shops")
        self.assertEqual(
            sorted(s["merchant"] for s in skipped),
            ["Already Right", "Big Box", "Made Up", "Maybe Diner", "Paycheck Co"],
        )


class TestSigningAndCard(unittest.TestCase):
    def test_signature_round_trip_and_tampering(self):
        ts = int(datetime.now(UTC).timestamp())
        sig = cr.generate_review_signature("r1", 3, "User@Example.com", ts)
        self.assertTrue(cr.verify_review_signature("r1", 3, "user@example.com", ts, sig)[0])
        self.assertFalse(cr.verify_review_signature("r1", 4, "user@example.com", ts, sig)[0])
        self.assertFalse(cr.verify_review_signature("r1", 3, "partner@example.com", ts, sig)[0])
        self.assertFalse(cr.verify_review_signature("r1", 3, "user@example.com", ts, "")[0])
        old = ts - cr.REVIEW_EXPIRATION_SECONDS - 10
        old_sig = cr.generate_review_signature("r1", 3, "user@example.com", old)
        self.assertIn("expired", cr.verify_review_signature("r1", 3, "user@example.com", old, old_sig)[1])

    def test_card_has_one_ticked_checkbox_per_merchant(self):
        items = [
            {
                **_cand("Corner Cafe", {"Uncategorized": 3}),
                "category_id": "c_coffee",
                "category_name": "Coffee Shops",
                "confidence": 0.95,
                "reason": "A cafe.",
                "source": "research",
                "transaction_ids": ["t1", "t2", "t3"],
            },
            {
                **_cand("Stream Co", {"Entertainment": 2}),
                "category_id": "c_subs",
                "category_name": "Subscriptions",
                "confidence": 1.0,
                "reason": "Confirmed before.",
                "source": "rule",
                "transaction_ids": ["t4"],
            },
        ]
        card = cr.build_review_card("r1", items, "user@example.com", 1700000000, "sig")
        widgets = card["card"]["sections"][0]["widgets"]
        checkbox = next(w["selectionInput"] for w in widgets if "selectionInput" in w)
        self.assertEqual(checkbox["name"], "selected")
        self.assertEqual([i["value"] for i in checkbox["items"]], ["0", "1"])
        self.assertTrue(all(i["selected"] for i in checkbox["items"]))
        buttons = next(w["buttonList"]["buttons"] for w in widgets if "buttonList" in w)
        params = {p["key"]: p["value"] for p in buttons[0]["onClick"]["action"]["parameters"]}
        self.assertEqual(params["action"], "apply_category_review")
        self.assertEqual(params["item_count"], "2")
        self.assertEqual(params["user_email"], "user@example.com")
        self.assertIn("4 transactions", card["card"]["header"]["subtitle"])
        self.assertIn("confirmed before", json.dumps(card))


class TestFormValues(unittest.TestCase):
    def test_reads_checkbox_values(self):
        payload = {"commonEventObject": {"formInputs": {"selected": {"stringInputs": {"value": ["0", "2"]}}}}}
        self.assertEqual(extract_card_form_values(payload, "selected"), ["0", "2"])

    def test_distinguishes_nothing_ticked_from_unreadable(self):
        self.assertEqual(extract_card_form_values({"commonEventObject": {"formInputs": {}}}, "selected"), [])
        self.assertIsNone(extract_card_form_values({"commonEventObject": {"parameters": {}}}, "selected"))


class TestPropose(unittest.TestCase):
    def test_unattributed_user_gets_no_review(self):
        res = cr.propose_category_review("unknown", bq=MagicMock())
        self.assertEqual(res["status"], "error")

    def test_rules_skip_research_and_research_fills_the_rest(self):
        decisions = [
            {
                "merchant": "Stream Co",
                "category_id": "c_subs",
                "category_name": "Subscriptions",
                "decision": "ACCEPTED",
                "decided_at": datetime.now(UTC),
            }
        ]
        cands = [
            _cand("Stream Co", {"Entertainment": 2, "Subscriptions": 5}),
            _cand("Corner Cafe", {"Uncategorized": 3}),
        ]
        client = MagicMock()
        client.models.generate_content.return_value.text = json.dumps(
            {
                "suggestions": [
                    {
                        "merchant": "Corner Cafe",
                        "action": "recategorize",
                        "category": "Coffee Shops",
                        "confidence": 0.92,
                        "reason": "A cafe.",
                    }
                ]
            }
        )
        with (
            patch.object(cr, "ensure_review_tables"),
            patch.object(cr, "load_from_monarch", AsyncMock(return_value=([], []))),
            patch.object(cr, "load_categories", return_value=CATEGORIES),
            patch.object(cr, "load_decisions", return_value=decisions),
            patch.object(cr, "find_review_candidates", return_value=cands),
            patch.object(cr, "_transaction_ids_to_change", side_effect=lambda bq, m, c: [f"{m}-1", f"{m}-2"]),
            patch.object(cr, "save_review") as save,
            patch.object(cr, "record_decisions"),
        ):
            res = cr.propose_category_review("user@example.com", 10, bq=MagicMock(), research_client=client)
        self.assertEqual(res["status"], "confirmation_required")
        self.assertEqual([s["merchant"] for s in res["suggestions"]], ["Stream Co", "Corner Cafe"])
        # Only the unknown merchant was researched.
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn('"merchant": "Corner Cafe"', prompt)
        self.assertNotIn('"merchant": "Stream Co"', prompt)
        review = save.call_args[0][1]
        params = {
            p["key"]: p["value"]
            for p in res["card"]["card"]["sections"][0]["widgets"][-2]["buttonList"]["buttons"][0]["onClick"]["action"][
                "parameters"
            ]
        }
        self.assertTrue(
            cr.verify_review_signature(
                review["review_id"], 2, "user@example.com", int(params["timestamp"]), params["signature"]
            )[0]
        )


class TestApply(unittest.TestCase):
    def setUp(self):
        cr._PENDING_REVIEWS.clear()
        cr._PENDING_REVIEWS["r1"] = {
            "review_id": "r1",
            "user_email": "user@example.com",
            "status": "PENDING",
            "items": [
                {
                    "merchant": "Corner Cafe",
                    "category_id": "c_coffee",
                    "category_name": "Coffee Shops",
                    "transaction_ids": ["t1", "t2"],
                },
                {
                    "merchant": "Big Box",
                    "category_id": "c_rest",
                    "category_name": "Restaurants & Bars",
                    "transaction_ids": ["t3"],
                },
                {
                    "merchant": "Stream Co",
                    "category_id": "c_subs",
                    "category_name": "Subscriptions",
                    "transaction_ids": ["t4", "t5"],
                },
            ],
        }

    def _apply(self, indexes, client):
        with (
            patch.object(cr, "record_decisions") as rec,
            patch.object(cr, "_mirror_to_bq") as mirror,
            patch.object(cr, "log_mutation_audit") as audit,
        ):
            res = asyncio.run(
                cr.apply_category_review("r1", indexes, "user@example.com", bq=MagicMock(), client=client)
            )
        return res, rec, mirror, audit

    def test_selected_are_written_and_the_rest_rejected(self):
        client = MagicMock()
        client.update_transaction = AsyncMock()
        res, rec, mirror, audit = self._apply([0, 2, 99], client)
        updated = sorted(c.kwargs["transaction_id"] for c in client.update_transaction.call_args_list)
        self.assertEqual(updated, ["t1", "t2", "t4", "t5"])
        self.assertEqual(res["status"], "APPLIED")
        self.assertEqual([i["merchant"] for i in res["applied"]], ["Corner Cafe", "Stream Co"])
        decisions = {c.args[2]: [i["merchant"] for i in c.args[1]] for c in rec.call_args_list}
        self.assertEqual(decisions["ACCEPTED"], ["Corner Cafe", "Stream Co"])
        self.assertEqual(decisions["REJECTED"], ["Big Box"])
        self.assertEqual(mirror.call_count, 2)
        self.assertEqual(audit.call_args.kwargs["action_type"], "CATEGORY_REVIEW")
        # A second click cannot apply it again.
        again, *_ = self._apply([0], client)
        self.assertFalse(again["success"])

    def test_reject_all_writes_nothing(self):
        client = MagicMock()
        client.update_transaction = AsyncMock()
        res, rec, mirror, _ = self._apply(None, client)
        client.update_transaction.assert_not_called()
        mirror.assert_not_called()
        self.assertEqual(res["status"], "REJECTED")
        self.assertEqual(len(rec.call_args_list[-1].args[1]), 3)

    def test_failed_updates_are_counted_and_not_mirrored(self):
        client = MagicMock()

        async def flaky(transaction_id, category_id):
            if transaction_id == "t2":
                raise RuntimeError("upstream")

        client.update_transaction = AsyncMock(side_effect=flaky)
        with patch("app.category_research.asyncio.sleep", AsyncMock()):
            res, _, mirror, _ = self._apply([0], client)
        self.assertEqual(res["status"], "PARTIAL_SUCCESS")
        self.assertEqual(res["failed_count"], 1)
        self.assertEqual(mirror.call_args.args[1], ["t1"])


def _reply_text(resp: dict) -> str:
    return resp["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]["text"]


def _review_click(clicker, card_user, action="apply_category_review", ticked=("0", "2"), sig=None, count=3):
    ts = int(datetime.now(UTC).timestamp())
    sig = sig if sig is not None else cr.generate_review_signature("r1", count, card_user, ts)
    common = {
        "invokedFunction": action,
        "parameters": {
            "action": action,
            "review_id": "r1",
            "item_count": str(count),
            "user_email": card_user,
            "timestamp": str(ts),
            "signature": sig,
        },
    }
    if ticked is not None:
        common["formInputs"] = {"selected": {"stringInputs": {"value": list(ticked)}}} if ticked else {}
    return {
        "type": "CARD_CLICKED",
        "chat": {"user": {"email": clicker, "displayName": "User"}},
        "commonEventObject": common,
    }


class TestChatHandlers(unittest.TestCase):
    def _click(self, payload):
        result = {
            "success": True,
            "applied": [{"merchant": "Corner Cafe", "applied_count": 2}],
            "rejected": [],
            "failed_count": 0,
            "card": {"cardId": "done", "card": {}},
        }
        with (
            patch("app.main.apply_category_review", AsyncMock(return_value=result)) as apply,
            patch("app.main.log_mutation_audit") as audit,
        ):
            text = _reply_text(asyncio.run(main.google_chat_webhook(payload)))
        return text, apply, audit

    def test_valid_apply_passes_ticked_indexes(self):
        text, apply, _ = self._click(_review_click("user@example.com", "user@example.com"))
        self.assertEqual(apply.call_args.args[:3], ("r1", [0, 2], "user@example.com"))
        self.assertIn("Recategorized 2 transactions", text)

    def test_reject_all_passes_none(self):
        _, apply, _ = self._click(
            _review_click("user@example.com", "user@example.com", action="reject_category_review", ticked=None)
        )
        self.assertIsNone(apply.call_args.args[1])

    def test_other_user_cannot_apply(self):
        text, apply, audit = self._click(_review_click("partner@example.com", "user@example.com"))
        self.assertIn("Only user@example.com", text)
        apply.assert_not_called()
        self.assertEqual(audit.call_args.kwargs["status"], "REJECTED")

    def test_tampered_signature_is_rejected(self):
        text, apply, _ = self._click(_review_click("user@example.com", "user@example.com", sig="forged"))
        self.assertIn("rejected", text)
        apply.assert_not_called()

    def test_unreadable_selection_changes_nothing(self):
        text, apply, _ = self._click(_review_click("user@example.com", "user@example.com", ticked=None))
        self.assertIn("couldn't read", text)
        apply.assert_not_called()

    def test_nothing_ticked_changes_nothing(self):
        text, apply, _ = self._click(_review_click("user@example.com", "user@example.com", ticked=()))
        self.assertIn("Nothing was ticked", text)
        apply.assert_not_called()

    def test_categorize_command_posts_interim_then_card(self):
        event = {
            "chat": {
                "user": {"email": "user@example.com", "displayName": "User"},
                "messagePayload": {
                    "message": {
                        "name": "spaces/AAAA/messages/m1",
                        "text": "/categorize 5",
                        "space": {"name": "spaces/AAAA"},
                        "thread": {"name": "spaces/AAAA/threads/t1"},
                    }
                },
            }
        }
        push = {"message": {"data": base64.b64encode(json.dumps(event).encode()).decode()}}
        proposal = {
            "status": "confirmation_required",
            "merchant_count": 2,
            "transaction_count": 4,
            "card": {"cardId": "category_review_x", "card": {}},
        }
        with (
            patch("app.main.propose_category_review", return_value=proposal) as propose,
            patch("app.main.post_to_chat_thread") as post,
        ):
            asyncio.run(main.google_chat_webhook(push))
        propose.assert_called_once_with("user@example.com", 5)
        self.assertEqual(post.call_count, 2)
        self.assertIn("Researching merchants", post.call_args_list[0].args[0])
        self.assertEqual(post.call_args_list[1].kwargs["cards_v2"], [proposal["card"]])


MONARCH_RULES_RAW = [
    {  # merchant-only rule: counts
        "id": "rule_1",
        "merchantNameCriteria": [{"operator": "eq", "value": "Stream Co"}],
        "setCategoryAction": {"id": "c_subs", "name": "Subscriptions"},
    },
    {  # legacy merchant field with contains
        "id": "rule_2",
        "merchantCriteria": [{"operator": "contains", "value": "burger"}],
        "setCategoryAction": {"id": "c_rest", "name": "Restaurants & Bars"},
    },
    {  # conditional on amount: only covers some transactions, ignored
        "id": "rule_3",
        "merchantNameCriteria": [{"operator": "eq", "value": "Corner Cafe"}],
        "amountCriteria": {"operator": "gt"},
        "setCategoryAction": {"id": "c_rest", "name": "Restaurants & Bars"},
    },
    {  # renames only, no category: ignored
        "id": "rule_4",
        "merchantNameCriteria": [{"operator": "eq", "value": "Big Box"}],
        "setCategoryAction": None,
    },
]


class TestMonarchRules(unittest.TestCase):
    def test_parse_keeps_unconditional_category_rules(self):
        rules = cr.parse_monarch_rules(MONARCH_RULES_RAW)
        self.assertEqual([r["id"] for r in rules], ["rule_1", "rule_2"])

    def test_matching(self):
        rules = cr.parse_monarch_rules(MONARCH_RULES_RAW)
        self.assertEqual(cr.matching_monarch_rule("stream co", rules)["id"], "rule_1")
        self.assertEqual(cr.matching_monarch_rule("Burger Barn #12", rules)["id"], "rule_2")
        self.assertIsNone(cr.matching_monarch_rule("Stream Company", rules))
        self.assertIsNone(cr.matching_monarch_rule("Corner Cafe", rules))

    def test_precedence(self):
        now = datetime(2026, 6, 1, tzinfo=UTC)
        rules = cr.parse_monarch_rules(MONARCH_RULES_RAW)
        cands = [
            _cand("Stream Co", {"Entertainment": 2}),
            _cand("Burger Barn", {"Uncategorized": 2}),
            _cand("Corner Cafe", {"Uncategorized": 2}),
        ]
        decisions = [
            # an older confirmation loses to the live Monarch rule
            {
                "merchant": "Stream Co",
                "category_id": "c_coffee",
                "category_name": "Coffee Shops",
                "decision": "ACCEPTED",
                "decided_at": now - timedelta(days=30),
            },
            # a recent rejection beats the Monarch rule
            {"merchant": "Burger Barn", "decision": "REJECTED", "decided_at": now - timedelta(days=3)},
        ]
        rule_based, to_research = cr.split_by_memory(cands, decisions, now, monarch_rules=rules)
        self.assertEqual(
            [(r["merchant"], r["category_id"], r["source"]) for r in rule_based],
            [("Stream Co", "c_subs", "monarch_rule")],
        )
        self.assertEqual(rule_based[0]["monarch_rule_id"], "rule_1")
        self.assertEqual([c["merchant"] for c in to_research], ["Corner Cafe"])

    def test_create_rule_sends_merchant_equals_and_category(self):
        client = MagicMock()
        client.gql_call = AsyncMock(
            return_value={"createTransactionRuleV2": {"transactionRule": {"id": "new_rule"}, "errors": None}}
        )
        rule_id = asyncio.run(cr.create_monarch_rule(client, "Corner Cafe", "c_coffee"))
        self.assertEqual(rule_id, "new_rule")
        sent = client.gql_call.call_args.kwargs["variables"]["input"]
        self.assertEqual(sent["merchantNameCriteria"], [{"operator": "eq", "value": "Corner Cafe"}])
        self.assertEqual(sent["setCategoryAction"], "c_coffee")
        self.assertFalse(sent["applyToExistingTransactions"])

    def test_create_rule_raises_on_monarch_errors(self):
        client = MagicMock()
        client.gql_call = AsyncMock(
            return_value={
                "createTransactionRuleV2": {
                    "transactionRule": None,
                    "errors": {"message": "Invalid", "fieldErrors": []},
                }
            }
        )
        with self.assertRaises(RuntimeError):
            asyncio.run(cr.create_monarch_rule(client, "Corner Cafe", "c_coffee"))

    def test_live_categories_mark_income_groups(self):
        client = MagicMock()
        client.get_transaction_categories = AsyncMock(
            return_value={
                "categories": [
                    {"id": "1", "name": "Coffee Shops", "group": {"name": "Food & Dining", "type": "expense"}},
                    {"id": "2", "name": "Paychecks", "group": {"name": "Income", "type": "income"}},
                    {"id": "3", "name": "Credit Card Payment", "group": {"name": "Transfers", "type": "transfer"}},
                ]
            }
        )
        cats = asyncio.run(cr.fetch_live_categories(client))
        self.assertEqual(
            [(c["category_name"], c["is_income"]) for c in cats],
            [("Coffee Shops", False), ("Paychecks", True), ("Credit Card Payment", True)],
        )

    def test_propose_uses_monarch_rules_and_live_categories(self):
        live_cats = [
            {"category_id": "live_coffee", "category_name": "Coffee & Tea", "group_name": "Food", "is_income": False}
        ]
        client = MagicMock()
        client.models.generate_content.return_value.text = json.dumps(
            {
                "suggestions": [
                    {
                        "merchant": "Corner Cafe",
                        "action": "recategorize",
                        "category": "Coffee & Tea",
                        "confidence": 0.9,
                        "reason": "A cafe.",
                    }
                ]
            }
        )
        cands = [_cand("Stream Co", {"Entertainment": 2}), _cand("Corner Cafe", {"Uncategorized": 3})]
        with (
            patch.object(cr, "ensure_review_tables"),
            patch.object(
                cr, "load_from_monarch", AsyncMock(return_value=(cr.parse_monarch_rules(MONARCH_RULES_RAW), live_cats))
            ),
            patch.object(cr, "load_categories") as synced_cats,
            patch.object(cr, "load_decisions", return_value=[]),
            patch.object(cr, "find_review_candidates", return_value=cands),
            patch.object(cr, "_transaction_ids_to_change", return_value=["t1"]),
            patch.object(cr, "save_review"),
            patch.object(cr, "record_decisions"),
        ):
            res = cr.propose_category_review("user@example.com", 10, bq=MagicMock(), research_client=client)
        synced_cats.assert_not_called()
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("Coffee & Tea", prompt)
        self.assertIn("Stream Co -> Subscriptions", prompt)  # Monarch rules guide the research
        card = json.dumps(res["card"])
        self.assertIn("your Monarch rule", card)
        self.assertIn("adds Monarch rule", card)  # only the researched merchant gets a new rule
        self.assertEqual(card.count("adds Monarch rule"), 1)

    def test_monarch_unreachable_falls_back_to_synced_categories(self):
        with (
            patch.object(cr, "ensure_review_tables"),
            patch.object(cr, "load_from_monarch", AsyncMock(side_effect=RuntimeError("down"))),
            patch.object(cr, "load_categories", return_value=CATEGORIES) as synced_cats,
            patch.object(cr, "load_decisions", return_value=[]),
            patch.object(cr, "find_review_candidates", return_value=[]),
        ):
            res = cr.propose_category_review("user@example.com", 10, bq=MagicMock())
        synced_cats.assert_called_once()
        self.assertEqual(res["status"], "none_found")


class TestApplyCreatesRules(unittest.TestCase):
    def setUp(self):
        cr._PENDING_REVIEWS.clear()
        cr._PENDING_REVIEWS["r2"] = {
            "review_id": "r2",
            "user_email": "user@example.com",
            "status": "PENDING",
            "items": [
                {
                    "merchant": "Corner Cafe",
                    "category_id": "c_coffee",
                    "category_name": "Coffee Shops",
                    "transaction_ids": ["t1"],
                    "create_rule": True,
                },
                {
                    "merchant": "Stream Co",
                    "category_id": "c_subs",
                    "category_name": "Subscriptions",
                    "transaction_ids": ["t2"],
                    "create_rule": False,
                    "source": "monarch_rule",
                    "monarch_rule_id": "rule_1",
                },
                {
                    "merchant": "Burger Barn",
                    "category_id": "c_rest",
                    "category_name": "Restaurants & Bars",
                    "transaction_ids": ["t3"],
                    "create_rule": True,
                },
            ],
        }

    def _apply(self, client):
        with (
            patch.object(cr, "record_decisions") as rec,
            patch.object(cr, "_mirror_to_bq"),
            patch.object(cr, "log_mutation_audit"),
        ):
            res = asyncio.run(
                cr.apply_category_review("r2", [0, 1, 2], "user@example.com", bq=MagicMock(), client=client)
            )
        return res, rec

    def _client(self, create_result):
        client = MagicMock()
        client.update_transaction = AsyncMock()

        async def gql_call(operation, graphql_query, variables=None):
            if operation == "GetTransactionRules":
                return {"transactionRules": MONARCH_RULES_RAW}
            if isinstance(create_result, Exception):
                raise create_result
            return create_result

        client.gql_call = AsyncMock(side_effect=gql_call)
        return client

    def test_rules_created_only_where_missing(self):
        client = self._client({"createTransactionRuleV2": {"transactionRule": {"id": "new_rule"}, "errors": None}})
        res, rec = self._apply(client)
        by_merchant = {i["merchant"]: i for i in res["applied"]}
        self.assertEqual(by_merchant["Corner Cafe"]["rule_status"], "created")
        self.assertEqual(by_merchant["Corner Cafe"]["monarch_rule_id"], "new_rule")
        # Burger Barn is already covered by a Monarch "contains burger" rule.
        self.assertEqual(by_merchant["Burger Barn"]["rule_status"], "exists")
        self.assertNotIn("rule_status", by_merchant["Stream Co"])
        creates = [c for c in client.gql_call.call_args_list if c.kwargs["operation"] != "GetTransactionRules"]
        self.assertEqual(len(creates), 1)
        accepted = next(c.args[1] for c in rec.call_args_list if c.args[2] == "ACCEPTED")
        self.assertEqual(
            {i["merchant"]: i.get("monarch_rule_id") for i in accepted},
            {"Corner Cafe": "new_rule", "Stream Co": "rule_1", "Burger Barn": "rule_2"},
        )
        self.assertIn("Monarch rule added", json.dumps(res["card"]))

    def test_rule_failure_keeps_the_transaction_fixes(self):
        client = self._client(RuntimeError("schema changed"))
        res, _ = self._apply(client)
        self.assertEqual(res["status"], "APPLIED")
        self.assertEqual(client.update_transaction.call_count, 3)
        by_merchant = {i["merchant"]: i for i in res["applied"]}
        self.assertEqual(by_merchant["Corner Cafe"]["rule_status"], "failed")
        self.assertIn("Monarch rule not added", json.dumps(res["card"]))


if __name__ == "__main__":
    unittest.main()
