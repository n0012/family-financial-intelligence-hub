"""
Tests for the summarized daily brief: finding selection, baselines, goal pacing and rendering.
"""

import unittest
from datetime import date
from decimal import Decimal

from app.daily_brief import (
    ALERT_REPEAT_DAYS,
    STALE_ACCOUNT_SCORE,
    TREND_REPEAT_DAYS,
    alert_findings,
    build_brief_card,
    build_brief_markdown,
    category_shift_findings,
    describe_cap,
    describe_heloc,
    merchant_frequency_findings,
    parse_heloc_target,
    select_findings,
    sparkline,
    stale_account_findings,
)


def _win_rows(key: str, amounts: list[float], counts: list[int]) -> list[dict]:
    return [{"k": key, "win": i, "amt": Decimal(str(a)), "n": n} for i, (a, n) in enumerate(zip(amounts, counts))]


class TestCategoryShifts(unittest.TestCase):
    def test_one_large_purchase_does_not_become_the_baseline(self):
        # Prior windows: $9,000 laptop month, then two normal $300 months. Median baseline is $300.
        rows = _win_rows("Electronics", [280, 9000, 300, 310], [3, 2, 3, 3])
        self.assertEqual(category_shift_findings(rows), [])

    def test_habitual_category_spike_is_reported(self):
        rows = _win_rows("Restaurants & Bars", [900, 300, 320, 280], [12, 8, 9, 7])
        findings = category_shift_findings(rows)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["key"], "category_up:restaurants & bars")
        self.assertAlmostEqual(findings[0]["score"], 600.0)

    def test_infrequent_category_is_ignored(self):
        rows = _win_rows("Furniture", [1500, 0, 200, 0], [1, 0, 1, 0])
        self.assertEqual(category_shift_findings(rows), [])


class TestMerchantFrequency(unittest.TestCase):
    def test_merchant_visited_twice_as_often(self):
        rows = _win_rows("Starbucks", [180, 60, 80, 70], [9, 3, 4, 4])
        findings = merchant_frequency_findings(rows)
        self.assertEqual([f["key"] for f in findings], ["merchant_freq:starbucks"])


class TestSelection(unittest.TestCase):
    def _c(self, key, score, repeat):
        return {"key": key, "score": score, "text": key, "repeat_days": repeat}

    def test_recently_shown_findings_are_skipped_until_their_window_passes(self):
        candidates = [
            self._c("category_up:travel", 900, TREND_REPEAT_DAYS),
            self._c("alert:duplicate:southwest", 200, ALERT_REPEAT_DAYS),
            self._c("merchant_freq:uber", 100, TREND_REPEAT_DAYS),
        ]
        shown = {"category_up:travel": 3, "alert:duplicate:southwest": 40}
        picked = select_findings(candidates, shown)
        self.assertEqual([p["key"] for p in picked], ["merchant_freq:uber"])

        shown_later = {"category_up:travel": TREND_REPEAT_DAYS, "alert:duplicate:southwest": 40}
        picked_later = select_findings(candidates, shown_later)
        self.assertEqual([p["key"] for p in picked_later], ["category_up:travel", "merchant_freq:uber"])

    def test_stale_account_outranks_spending_findings(self):
        stale = stale_account_findings([{"account": "Card (...1234)", "last_txn": "2026-08-14", "n_year": 1500}])
        self.assertEqual(stale[0]["score"], STALE_ACCOUNT_SCORE)
        self.assertIn("Aug 14", stale[0]["text"])
        picked = select_findings([self._c("category_up:travel", 50_000, TREND_REPEAT_DAYS)] + stale, {})
        self.assertTrue(picked[0]["key"].startswith("stale_account:"))


class TestAlertFindings(unittest.TestCase):
    def test_duplicate_alert_rows_collapse_and_one_off_subscriptions_are_dropped(self):
        dup = {"type": "DUPLICATE_CHARGE", "alert_key": "dup:united:318.40", "title": "Dup", "detail": "x"}
        one_off = {
            "type": "NEW_SUBSCRIPTION_DETECTED",
            "alert_key": "new_sub:cap_store",
            "title": "New Subscription",
            "detail": "Total charged so far: $147.05 across 1 transaction(s).",
        }
        standing = {"type": "HELOC_OPPORTUNITY", "alert_key": "heloc", "title": "HELOC", "detail": "x"}
        findings = alert_findings([dup, dict(dup), one_off, standing])
        self.assertEqual([f["key"] for f in findings], ["alert:dup:united:318.40"])


class TestGoals(unittest.TestCase):
    def test_parse_heloc_target(self):
        mem = ["Goal: pay off the HELOC by June 30, 2027."]
        self.assertEqual(parse_heloc_target(mem), date(2027, 6, 30))
        self.assertIsNone(parse_heloc_target(["Dining cap is $400 by month end"]))

    def test_heloc_months_left_includes_current_month(self):
        g = describe_heloc({"balance": 300_000.0, "paid_mtd": 0.0}, date(2027, 3, 31), date(2026, 10, 2))
        self.assertIn("$50,000/month", g["detail"])

    def test_cap_pacing_states(self):
        today = date(2026, 10, 10)  # 10/31 of the month elapsed; on-pace spend is ~$129 of $400
        self.assertIn("on pace", describe_cap("dining", 400, {"mtd": 120, "last_month": 390}, today)["line"])
        ahead = describe_cap("dining", 400, {"mtd": 300, "last_month": 390}, today)
        self.assertIn("ahead of pace", ahead["line"])
        over = describe_cap("dining", 400, {"mtd": 450, "last_month": 470}, today)
        self.assertIn("over by $50.00", over["line"])
        self.assertIn("September finished at $470, over by $70.00", over["detail"])


class TestRendering(unittest.TestCase):
    def _brief(self, findings):
        return {
            "date": date(2026, 10, 2),
            "recent": {
                "count": 2,
                "total": 140.0,
                "top_merchant": "Costco",
                "top_category": "Groceries",
                "top_amount": 138.0,
                "top_usual": 95.0,
                "top_history_count": 12,
            },
            "month": {"mtd": 61.0, "last_month_same_point": 120.0},
            "goals": [describe_cap("dining", 400, {"mtd": 61, "last_month": 390}, date(2026, 10, 2))],
            "findings": findings,
            "trends": [{"category": "Groceries", "weekly": [100.0] * 9 + [200.0] * 4}],
        }

    def test_card_sections_and_quiet_day(self):
        card = build_brief_card(self._brief([]))
        sections = card["cardsV2"][0]["card"]["sections"]
        self.assertEqual(
            [s["header"] for s in sections], ["Recent activity", "🎯 Goals", "🔍 Worth a look", "📈 13-week trends"]
        )
        self.assertIn("Nothing new stands out today.", str(sections[2]))
        self.assertIn("usually about $95.00", str(sections[0]))
        self.assertIn("Groceries ↑", str(sections[3]))

    def test_markdown_fallback_strips_html(self):
        md = build_brief_markdown(self._brief([{"key": "k", "text": "<b>Uber</b>: 9 visits", "score": 1}]))
        self.assertIn("**Uber**: 9 visits", md)
        self.assertNotIn("<b>", md)

    def test_sparkline(self):
        self.assertEqual(sparkline([0, 50, 100]), "▁▅█")
        self.assertEqual(sparkline([0, 0]), "▁▁")


if __name__ == "__main__":
    unittest.main()
