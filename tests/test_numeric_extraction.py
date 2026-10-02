"""
BigQuery returns NUMERIC columns (balances, amounts) as decimal.Decimal.
These must not be silently coerced to the 0.0 default.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from app.alerts import _safe_float, _safe_str


class TestNumericExtraction(unittest.TestCase):
    def test_safe_float_reads_decimal_from_row_attribute(self):
        row = SimpleNamespace(liquid_balance=Decimal("1234.56"))
        self.assertEqual(_safe_float(row, "liquid_balance"), 1234.56)

    def test_safe_float_reads_decimal_from_dict(self):
        self.assertEqual(_safe_float({"mtd_spend": Decimal("89.10")}, "mtd_spend"), 89.10)

    def test_safe_float_still_defaults_when_missing(self):
        self.assertEqual(_safe_float(SimpleNamespace(), "liquid_balance", default=-1.0), -1.0)

    def test_safe_str_formats_decimal(self):
        self.assertEqual(_safe_str({"amount": Decimal("12.50")}, "amount"), "12.50")


if __name__ == "__main__":
    unittest.main()
