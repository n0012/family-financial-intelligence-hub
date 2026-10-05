"""
Business tags: Monarch tags are synced into BigQuery, and transactions carrying the business tag are left
out of household spend totals, trends, pacing and alerts.
"""

import ast
import asyncio
import pathlib
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import monarch_service

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUSINESS_FILTER = re.compile(r"is_business|\{HOUSEHOLD_SPEND_SQL\}|\{SPEND_WHERE\}|\{DISCRETIONARY_WHERE\}")


def _txn(txn_id, tags):
    return {
        "id": txn_id,
        "date": "2026-03-11",
        "amount": -120.0,
        "account": {"id": "acc_1", "institution": {"name": "Test Bank"}},
        "merchant": {"name": "Example Air"},
        "category": {"id": "cat_air", "name": "Airfare"},
        "tags": [{"id": f"tag_{n}", "name": n} for n in tags],
    }


class TestTagSync(unittest.TestCase):
    def setUp(self):
        monarch_service._TAG_COLUMNS_READY = False

    @patch("app.monarch_service.get_excluded_institutions", return_value=set())
    @patch("app.monarch_service.get_decommissioned_account_ids", return_value=set())
    def test_tags_and_business_flag_reach_bigquery(self, *_):
        client = MagicMock()
        client.get_transactions = AsyncMock(
            return_value={
                "allTransactions": {"results": [_txn("t1", ["business", "Trip: Springfield Mar 2026"]), _txn("t2", [])]}
            }
        )
        bq = MagicMock()
        count = asyncio.run(monarch_service.sync_transactions(client, bq, 30, "2026-03-20T00:00:00Z"))
        self.assertEqual(count, 2)

        rows = bq.load_table_from_json.call_args[0][0]
        self.assertEqual(rows[0]["tags"], ["business", "Trip: Springfield Mar 2026"])
        self.assertTrue(rows[0]["is_business"])  # tag match ignores case
        self.assertEqual(rows[1]["tags"], [])
        self.assertFalse(rows[1]["is_business"])

        schema = {f.name: f for f in bq.load_table_from_json.call_args.kwargs["job_config"].schema}
        self.assertEqual(schema["tags"].mode, "REPEATED")
        sqls = [c[0][0] for c in bq.query.call_args_list]
        self.assertIn("ADD COLUMN IF NOT EXISTS tags", sqls[0])  # columns exist before the MERGE writes them
        self.assertIn("T.tags = S.tags", sqls[-1])
        self.assertIn("T.is_business = S.is_business", sqls[-1])

    def test_business_tag_is_configurable(self):
        with patch.object(monarch_service, "BUSINESS_TAG", "Work"):
            self.assertTrue(monarch_service.is_business_tagged(["work "]))
            self.assertFalse(monarch_service.is_business_tagged(["Business"]))


def _sql_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every string literal in a module, with f-string placeholders kept as {name}."""
    tree = ast.parse(path.read_text())
    inside_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value
                if isinstance(v, ast.Constant)
                else "{" + (v.value.id if isinstance(v.value, ast.Name) else "expr") + "}"
                for v in node.values
            )
            out.append((node.lineno, text))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside_fstring:
            out.append((node.lineno, node.value))
    return out


class TestHouseholdSpendExcludesBusiness(unittest.TestCase):
    def test_every_spend_query_filters_business(self):
        # Any query summing expenses (amount < 0) in the alert and brief modules must drop business spend.
        missing = []
        for module in ("app/alerts.py", "app/daily_brief.py"):
            for line, text in _sql_strings(ROOT / module):
                if "amount < 0" in text and not BUSINESS_FILTER.search(text):
                    missing.append(f"{module}:{line}")
        self.assertEqual(missing, [], "spend queries without the business filter")

    def test_spend_views_filter_business(self):
        schema = (ROOT / "schema.sql").read_text()
        for view in ("v_spend_classification", "v_food_efficiency", "v_micro_transaction_leakage"):
            body = schema.split(f"VIEW `family_finance.{view}` AS", 1)[1].split("CREATE OR REPLACE VIEW", 1)[0]
            self.assertIn("NOT COALESCE(is_business, FALSE)", body, view)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_business BOOL", schema)

    def test_daily_brief_spend_filters_cover_business(self):
        from app import daily_brief

        for clause in (daily_brief.SPEND_WHERE, daily_brief.SPEND_INCL_PENDING_WHERE, daily_brief.DISCRETIONARY_WHERE):
            self.assertIn("NOT COALESCE(is_business, FALSE)", clause)


if __name__ == "__main__":
    unittest.main()
