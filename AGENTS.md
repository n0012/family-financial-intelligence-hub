# Rules for every contributor (human or AI agent)

## This is a PUBLIC repository about a household's finances

Nothing personal or confidential may be committed — not in code, tests, fixtures, docs,
commit messages, PR titles or PR bodies. Treat everything you learned while running the
app against real data (BigQuery rows, Monarch accounts, Chat messages, logs) as private.

Never commit:

- Names of household members, payees, employers, schools, doctors, or contractors
- Email addresses (use `user@example.com`) and real domains
- GCP project IDs and numbers, Cloud Run URLs, service-account emails, Vertex resource
  IDs (reasoning engines, memory banks), Google Chat space IDs, webhook URLs
  (use `your-project-id`, `family-finance-hub`, `spaces/AAAA`)
- Account names, masks (`...1234`), card numbers, Monarch account or transaction IDs
- Real balances, transaction amounts, dates, merchants, goals, or spending figures —
  even in examples. Use round illustrative numbers (`$400` cap, `$250,000` balance) and
  common fictional or generic merchants.
- Secrets of any kind (API keys, tokens, service-account keys, `.env` files)

When copying a real bug report into a test, **rebuild the fixture from scratch** with
fake values; do not paste and edit a real payload.

## Enforcement

`scripts/check_private_data.py` scans every change against generic patterns plus a
private denylist of household-specific terms (gitignored `.private-denylist` locally,
`PRIVATE_DENYLIST` secret in CI). It runs:

| Where | What it scans |
| --- | --- |
| `.githooks/pre-commit` | staged lines |
| `.githooks/commit-msg` | the commit message |
| `.githooks/pre-push` | every outgoing commit and message |
| `.github/workflows/private-data.yml` | full tree, every pushed commit and message, PR title and body |

Enable the local hooks once per clone:

```bash
git config core.hooksPath .githooks
```

**Never bypass it**: no `git commit --no-verify`, no `git push --no-verify`, no editing
`core.hooksPath`, no weakening the scanner or the workflow to get a change through. If
the check fails, remove the data. If it is a genuine false positive, add
`private-data: allow` on that line and say why in the PR description.

Run it by hand any time:

```bash
python3 scripts/check_private_data.py --all
```

If a leak reaches `main`, removing it in a new commit is not enough — the old commit
stays public. Stop and tell the maintainer so the history can be rewritten.

## Other conventions

- Python environments use `uv` (`uv venv`, `uv pip install`, `uv run`).
- Run tests with `uv run python -m pytest`.
