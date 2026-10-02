#!/usr/bin/env python3
"""PreToolUse hook: stop agents from bypassing the private-data git hooks in this public repo."""

import json
import re
import sys

command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
patterns = [
    r"\bgit\b[^|;&]*\s--no-verify\b",
    r"\bgit\b[^|;&]*\scommit\b[^|;&]*\s-n\b",
    r"core\.hooksPath(?!\s+\.githooks\s*$)",
    r"\bSKIP_PRIVATE_DATA\b",
]
if any(re.search(p, command) for p in patterns):
    print(
        "Blocked: this public financial repo forbids bypassing the private-data hooks "
        "(see AGENTS.md). Remove the private data instead.",
        file=sys.stderr,
    )
    sys.exit(2)
