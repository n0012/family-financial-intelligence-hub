"""
Tests for the public-repo private-data scanner. Sample leaks are assembled from fragments
so this file itself passes the scan.
"""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_private_data", Path(__file__).resolve().parent.parent / "scripts" / "check_private_data.py"
)
cpd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpd)


def hits(line: str, path: str = "app/x.py", denylist=None) -> list[str]:
    return cpd.scan_line(path, 1, line, denylist or [])


class TestGenericRules(unittest.TestCase):
    def test_flags_identifying_values(self):
        leaks = [
            "owner = 'jane.doe" + "@" + "gmail.com'",
            "SA = 'runner" + "@" + "acme-prod-42.iam.gserviceaccount.com'",
            "name = 'projects/" + "8642" + "09753113/locations/us-central1'",
            "engine = 'reasoningEngines/" + "71530864" + "29107735142'",
            "space = 'spaces/" + "AAQBz7" + "kR3mT'",
            "url = 'https://svc-" + "q7w2k9xpzt" + "-uc.a.run.app'",
            "url = 'https://svc-" + "864209753113" + ".us-central1.run.app'",
            "acct = 'Visa (..." + "4821)'",
            "card = '4111 1111 " + "1111 1111'",
            "txn_id = '6180274539" + "20481736'",
            "key = 'AIza" + "Sy" + "A" * 33 + "'",
            "-----BEGIN " + "PRIVATE KEY-----",
        ]
        for line in leaks:
            with self.subTest(line=line):
                self.assertTrue(hits(line), line)

    def test_precise_amounts_only_flagged_in_docs(self):
        line = "Balance: $" + "212,407.63"
        self.assertTrue(hits(line, "docs/guide.md"))
        self.assertFalse(hits(line, "tests/test_x.py"))
        self.assertFalse(hits("Balance: $250,000", "docs/guide.md"))

    def test_placeholders_pass(self):
        ok = [
            "email = 'user" + "@" + "example.com'",
            "email = 'test" + "@" + "family.internal'",
            "sa = 'bot" + "@" + "your-project-id.iam.gserviceaccount.com'",
            "sa = f'{name}" + "@" + "{project}.iam.gserviceaccount.com'",
            "ids: ['123456789012345678', '987654321098765432']",
            "acct = 'Card (..." + "1234)'",
            "name = 'spaces/AAAA'",
            "SERVICE_URL = 'https://svc-<hash>.a.run.app'",
        ]
        for line in ok:
            with self.subTest(line=line):
                self.assertEqual(hits(line), [], line)

    def test_allow_pragma(self):
        self.assertEqual(hits("x = 'a" + "@" + "gmail.com'  # private-data: allow"), [])


class TestDenylist(unittest.TestCase):
    def test_case_insensitive_and_term_not_echoed(self):
        found = hits("merchant = 'Zebra Bistro'", denylist=["zebra bistro"])
        self.assertEqual(len(found), 1)
        self.assertNotIn("zebra", found[0].lower())

    def test_term_split_across_concatenation_is_caught(self):
        self.assertTrue(hits("m = 'Zebra' + ' Bistro'", denylist=["zebra bistro"]))

    def test_diff_scans_only_added_lines(self):
        diff = "\n".join(
            [
                "+++ b/app/a.py",
                "@@ -1,2 +10,2 @@",
                "-old = 'zebra bistro'",
                "+new = 'fine'",
                "+bad = 'Zebra Bistro'",
            ]
        )
        found = cpd.scan_diff(diff, ["zebra bistro"])
        self.assertEqual(found, ["app/a.py:11: matches private denylist entry #1"])

    def test_commit_message_comments_ignored(self):
        msg = "fix: tidy\n# On branch zebra-bistro\n"
        self.assertEqual(cpd.scan_text("commit message", msg, ["zebra-bistro"]), [])


if __name__ == "__main__":
    unittest.main()
