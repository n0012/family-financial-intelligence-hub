#!/usr/bin/env python3
"""
Blocks personal or confidential data from entering this public repository.

This is a public repo about a household's finances, so nothing that identifies
the household, its accounts, its cloud project, or its real figures may be
committed. The check runs in three places so it applies to every author,
human or AI agent:

  * .githooks/pre-commit   -> staged changes   (python3 scripts/check_private_data.py --staged)
  * .githooks/commit-msg   -> the commit message (--message-file PATH)
  * .github/workflows/private-data.yml -> every push and pull request, including
    the PR title and body, so a bypassed local hook (--no-verify) is still caught.

Two layers of rules:

  1. Generic patterns (below): real-looking emails, GCP project numbers and resource
     IDs, Chat space IDs, Cloud Run hash URLs, account masks, card numbers, long
     numeric account IDs, API keys and private keys, and precise dollar amounts in docs.
  2. A private denylist of household-specific terms (names, domains, project IDs,
     account digits, real balances, merchants). It is never committed: it is read
     from the gitignored file `.private-denylist` locally and from the
     PRIVATE_DENYLIST secret in CI. Matches are reported without echoing the term.

A deliberate, reviewed exception can be marked on the same line with:
    private-data: allow
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOW_PRAGMA = "private-data: allow"
DENYLIST_FILE = ".private-denylist"
SKIP_FILES = {"uv.lock"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz"}

ALLOWED_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
    "noreply.anthropic.com",
    "anthropic.com",
    "system.gserviceaccount.com",
    "domain.com",
)
# RFC 2606 / 6761 reserved TLDs never route to a real mailbox.
RESERVED_TLDS = (".test", ".example", ".invalid", ".localhost", ".internal", ".local")
PLACEHOLDER_SA_PROJECTS = {"your-project-id", "project-id", "my-project", "family-finance-hub", "evil-project"}
PLACEHOLDER_MASKS = {"0000", "1111", "1234", "4321", "9999", "xxxx"}


def _is_placeholder_number(digits: str) -> bool:
    """Obviously fabricated IDs: repeated digits or ascending/descending runs (123456789..., 987654321...)."""
    if len(set(digits)) == 1:
        return True
    asc, desc = "01234567890123456789", "98765432109876543210"
    return any(digits[i : i + 9] in asc or digits[i : i + 9] in desc for i in range(len(digits) - 8))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _email_is_private(match: re.Match) -> bool:
    domain = match.group(2).lower()
    if "{" in domain or "$" in domain:
        return False
    if domain.endswith(".iam.gserviceaccount.com"):
        # Service accounts reveal the project ID unless templated or a placeholder.
        return domain.split(".")[0] not in PLACEHOLDER_SA_PROJECTS
    if domain.endswith(RESERVED_TLDS):
        return False
    return not any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


# (rule name, compiled regex, optional predicate on the match, file-suffix filter or None)
RULES = [
    ("email address", re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"), _email_is_private, None),
    ("GCP project number", re.compile(r"\bprojects/\d{6,}\b"), None, None),
    ("GCP project number in service account", re.compile(r"\b\d{10,}-compute@"), None, None),
    (
        "Vertex resource ID",
        re.compile(r"\b(?:reasoningEngines|memories|endpoints|datasets)/\d{6,}\b"),
        None,
        None,
    ),
    ("Google Chat space ID", re.compile(r"\bspaces/(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{11}\b"), None, None),
    ("Cloud Run URL with project hash", re.compile(r"-[a-z0-9]{10}-[a-z]{2}\.a\.run\.app"), None, None),
    ("Cloud Run URL with project number", re.compile(r"-\d{9,}\.[a-z0-9-]+\.run\.app"), None, None),
    (
        "account mask",
        re.compile(r"\(\s*(?:\.\.\.|…|x{2,}|\*{2,})\s*(\d{4})\s*\)", re.IGNORECASE),
        lambda m: m.group(1) not in PLACEHOLDER_MASKS,
        None,
    ),
    (
        "card number",
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        lambda m: (
            (d := re.sub(r"\D", "", m.group(0))) and len(d) >= 13 and _luhn_ok(d) and not _is_placeholder_number(d)
        ),
        None,
    ),
    (
        "long numeric account/transaction ID",
        re.compile(r"(?<![\w.])\d{15,19}(?![\w.])"),
        lambda m: not _is_placeholder_number(m.group(0)),
        None,
    ),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), None, None),
    ("Google OAuth client secret", re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{20,}"), None, None),
    ("Anthropic API key", re.compile(r"\bsk-ant-[0-9A-Za-z_-]{20,}"), None, None),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), None, None),
    ("service account key", re.compile(r'"private_key_id"\s*:\s*"[0-9a-f]{20,}"'), None, None),
    (
        "precise dollar amount in docs (use round illustrative figures)",
        re.compile(r"\$\d{1,3}(?:,\d{3})+\.\d{2}\b"),
        None,
        (".md",),
    ),
]


def load_denylist(repo_root: Path) -> list[str]:
    terms: list[str] = []
    env_terms = os.getenv("PRIVATE_DENYLIST", "")
    terms += env_terms.splitlines()
    path = Path(os.getenv("PRIVATE_DENYLIST_FILE", repo_root / DENYLIST_FILE))
    if path.is_file():
        terms += path.read_text(encoding="utf-8").splitlines()
    cleaned = []
    for t in terms:
        t = t.strip()
        if t and not t.startswith("#"):
            cleaned.append(t.lower())
    return sorted(set(cleaned))


_CONCAT = re.compile(r"""["']\s*\+\s*f?["']""")


def scan_line(path: str, lineno: int, line: str, denylist: list[str]) -> list[str]:
    if ALLOW_PRAGMA in line:
        return []
    findings = []
    suffix = Path(path).suffix.lower()
    for name, regex, predicate, suffixes in RULES:
        if suffixes and suffix not in suffixes:
            continue
        for m in regex.finditer(line):
            if predicate is None or predicate(m):
                findings.append(f"{path}:{lineno}: {name}: {m.group(0)!r}")
                break
    # Also match terms split across string concatenation ("pass" + "word") to defeat trivial evasion.
    lowered = line.lower()
    joined = _CONCAT.sub("", lowered)
    for idx, term in enumerate(denylist, start=1):
        if term in lowered or term in joined:
            # Never echo the term itself: CI logs of a public repo are public.
            findings.append(f"{path}:{lineno}: matches private denylist entry #{idx}")
    return findings


def _should_skip(path: str) -> bool:
    p = Path(path)
    return p.name in SKIP_FILES or p.suffix.lower() in BINARY_SUFFIXES or p.name == DENYLIST_FILE


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def added_lines_from_diff(diff: str):
    """Yields (path, new_lineno, text) for every added line in a unified diff."""
    path, lineno = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
            continue
        if path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            yield path, lineno, raw[1:]
            lineno += 1
        elif not raw.startswith("-"):
            lineno += 1


def scan_diff(diff: str, denylist: list[str]) -> list[str]:
    findings = []
    for path, lineno, text in added_lines_from_diff(diff):
        if not _should_skip(path):
            findings += scan_line(path, lineno, text, denylist)
    return findings


def scan_text(label: str, text: str, denylist: list[str]) -> list[str]:
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("#"):
            continue  # git commit-message comments
        findings += scan_line(label, i, line, denylist)
    return findings


def scan_tree(denylist: list[str]) -> list[str]:
    findings = []
    for path in _git("ls-files").splitlines():
        if _should_skip(path) or not Path(path).is_file():
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            findings += scan_line(path, i, line, denylist)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan lines added in the staged changes")
    mode.add_argument("--range", metavar="BASE..HEAD", help="scan lines added in a commit range")
    mode.add_argument("--all", action="store_true", help="scan every tracked file")
    mode.add_argument("--message-file", metavar="PATH", help="scan a commit message file")
    mode.add_argument("--text-env", metavar="VAR", help="scan text held in an environment variable (PR title/body)")
    parser.add_argument("--require-denylist", action="store_true", help="fail if no private denylist is configured")
    args = parser.parse_args()

    repo_root = Path(_git("rev-parse", "--show-toplevel").strip())
    os.chdir(repo_root)
    denylist = load_denylist(repo_root)
    if not denylist:
        msg = (
            "private-data check: no private denylist configured "
            f"({DENYLIST_FILE} or PRIVATE_DENYLIST); only generic patterns are checked."
        )
        if args.require_denylist:
            print(msg.replace("only generic patterns are checked.", "refusing to pass."), file=sys.stderr)
            return 2
        print(msg, file=sys.stderr)

    if args.staged:
        findings = scan_diff(_git("diff", "--cached", "-U0", "--no-color"), denylist)
    elif args.range:
        findings = scan_diff(_git("diff", "-U0", "--no-color", args.range), denylist)
    elif args.message_file:
        findings = scan_text("commit message", Path(args.message_file).read_text(encoding="utf-8"), denylist)
    elif args.text_env:
        findings = scan_text(args.text_env, os.getenv(args.text_env, ""), denylist)
    else:
        findings = scan_tree(denylist)

    if findings:
        print("✋ Private-data check failed. This is a PUBLIC repo about household finances.", file=sys.stderr)
        print("Replace these with neutral placeholders (see AGENTS.md):\n", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(f"\nIf a match is a reviewed false positive, add '{ALLOW_PRAGMA}' on that line.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
