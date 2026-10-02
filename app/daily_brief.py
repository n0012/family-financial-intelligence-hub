"""
Summarized daily brief.

Replaces the fixed alert checklist with a short card that changes day to day:
  1. The past few days at a glance, compared with your normal for that merchant.
  2. Goal pacing for the caps and payoff targets stored in long-term memory.
  3. The one or two most notable findings that have not been shown recently.
  4. 13-week trend sparklines for the biggest discretionary categories.

All figures are computed in BigQuery. Findings are ranked by dollar impact, and
each one is recorded in `brief_history` so the same finding is not repeated
every morning.
"""

import asyncio
import calendar
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import BQ_DATASET_ID, BQ_PROJECT_ID, resolve_secret

try:
    from google.cloud import bigquery
except ImportError:  # pragma: no cover
    bigquery = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("monarch-gemini.daily_brief")

TZ = "America/New_York"
AVATAR_URL = "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png"

# Money movement that is not spending.
NON_SPEND_CATEGORIES = (
    "transfer",
    "credit card payment",
    "balance transfer",
    "loan payment",
    "investment",
    "savings",
    "mortgage",
)
# Fixed bills: real spending, but not interesting as day-to-day trends.
FIXED_CATEGORIES = (
    "auto payment",
    "insurance",
    "taxes",
    "gas & electric",
    "water",
    "utilities",
    "phone",
    "internet & cable",
    "rent",
)

GOAL_FILTERS = {
    "dining": (
        "(LOWER(category_name) IN ('restaurants', 'dining out', 'fast food', 'coffee shops', 'bars', 'restaurants & bars')"
        " OR LOWER(merchant_name) LIKE '%doordash%' OR LOWER(merchant_name) LIKE '%ubereats%'"
        " OR LOWER(merchant_name) LIKE '%grubhub%')"
    ),
    "groceries": (
        "(LOWER(category_name) IN ('groceries', 'supermarkets', 'grocery')"
        " OR LOWER(merchant_name) LIKE '%whole foods%' OR LOWER(merchant_name) LIKE '%trader joe%'"
        " OR LOWER(merchant_name) LIKE '%costco%' OR LOWER(merchant_name) LIKE '%grocery mart%')"
    ),
}

# Existing alert checks that are worth surfacing once, and their base ranking score.
ALERT_SCORES = {
    "DUPLICATE_CHARGE": 200.0,
    "NEW_SUBSCRIPTION_DETECTED": 120.0,
    "PRICE_CREEP": 100.0,
    "UTILITY_SEASONAL_SPIKE": 80.0,
    "SUBSCRIPTION_OVERLAP": 60.0,
}

TREND_REPEAT_DAYS = 14
ALERT_REPEAT_DAYS = 365
MAX_FINDINGS = 2
MIN_HABIT_TRANSACTIONS = 6
STALE_ACCOUNT_DAYS = 14
STALE_ACCOUNT_SCORE = 1_000_000.0  # data-quality warnings outrank every spending finding
RECENT_DAYS = 3
SPARKS = "▁▂▃▄▅▆▇█"


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


SPEND_WHERE = (
    "amount < 0 AND NOT COALESCE(pending, FALSE) "
    f"AND LOWER(COALESCE(category_name, '')) NOT IN ({_sql_list(NON_SPEND_CATEGORIES)})"
)
# Recent activity, month-to-date and goal pacing include pending charges, since Monarch posts a day or two late.
SPEND_INCL_PENDING_WHERE = SPEND_WHERE.replace("AND NOT COALESCE(pending, FALSE) ", "")
DISCRETIONARY_WHERE = f"{SPEND_WHERE} AND LOWER(COALESCE(category_name, '')) NOT IN ({_sql_list(FIXED_CATEGORIES)})"
TODAY = f"CURRENT_DATE('{TZ}')"
YESTERDAY = f"DATE_SUB({TODAY}, INTERVAL 1 DAY)"
MERCHANT = "COALESCE(NULLIF(clean_merchant_name, ''), NULLIF(merchant_name, ''), 'Unknown')"


def _num(v: Any) -> float:
    """BigQuery NUMERIC arrives as Decimal; NULL as None."""
    return float(v) if v is not None else 0.0


def _rows(bq: Any, sql: str, job_config: Any = None) -> list[dict[str, Any]]:
    result = bq.query(sql, job_config=job_config).result() if job_config else bq.query(sql).result()
    return [r if isinstance(r, dict) else dict(r.items()) for r in result]


def _day(d: Any) -> str:
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    return d.strftime("%b %-d") if hasattr(d, "strftime") else str(d)


def _money(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def fetch_recent(bq: Any, table: str) -> dict[str, Any]:
    """Spending over the past few days, pending included, since Monarch posts a day or two late."""
    sql = f"""
    WITH spend AS (
        SELECT transaction_date AS d, {MERCHANT} AS merchant, category_name, -amount AS amt
        FROM `{table}`
        WHERE {SPEND_INCL_PENDING_WHERE} AND transaction_date >= DATE_SUB({TODAY}, INTERVAL 366 DAY)
    ),
    y AS (SELECT * FROM spend WHERE d >= DATE_SUB({TODAY}, INTERVAL {RECENT_DAYS} DAY)),
    top AS (SELECT merchant, category_name, amt FROM y ORDER BY amt DESC LIMIT 1),
    hist AS (
        SELECT APPROX_QUANTILES(s.amt, 2)[OFFSET(1)] AS usual, COUNT(*) AS n
        FROM spend s JOIN top ON s.merchant = top.merchant
        WHERE s.d < DATE_SUB({TODAY}, INTERVAL {RECENT_DAYS} DAY)
    )
    SELECT
        (SELECT COUNT(*) FROM y) AS n,
        (SELECT SUM(amt) FROM y) AS total,
        top.merchant, top.category_name, top.amt AS top_amt,
        hist.usual, hist.n AS hist_n
    FROM (SELECT 1) LEFT JOIN top ON TRUE LEFT JOIN hist ON TRUE
    """
    r = _rows(bq, sql)[0]
    return {
        "count": int(_num(r.get("n"))),
        "total": _num(r.get("total")),
        "top_merchant": r.get("merchant"),
        "top_category": r.get("category_name"),
        "top_amount": _num(r.get("top_amt")),
        "top_usual": _num(r.get("usual")),
        "top_history_count": int(_num(r.get("hist_n"))),
    }


def fetch_month_spend(bq: Any, table: str) -> dict[str, float]:
    sql = f"""
    SELECT
        SUM(IF(transaction_date >= DATE_TRUNC({TODAY}, MONTH), -amount, 0)) AS mtd,
        SUM(IF(transaction_date >= DATE_TRUNC(DATE_SUB({TODAY}, INTERVAL 1 MONTH), MONTH)
               AND transaction_date <= DATE_SUB({TODAY}, INTERVAL 1 MONTH), -amount, 0)) AS last_month_same_point
    FROM `{table}`
    WHERE {SPEND_INCL_PENDING_WHERE} AND transaction_date >= DATE_TRUNC(DATE_SUB({TODAY}, INTERVAL 1 MONTH), MONTH)
    """
    r = _rows(bq, sql)[0]
    return {"mtd": _num(r.get("mtd")), "last_month_same_point": _num(r.get("last_month_same_point"))}


def fetch_cap_spend(bq: Any, table: str, goal: str) -> dict[str, float]:
    sql = f"""
    SELECT
        SUM(IF(transaction_date >= DATE_TRUNC({TODAY}, MONTH), -amount, 0)) AS mtd,
        SUM(IF(transaction_date < DATE_TRUNC({TODAY}, MONTH), -amount, 0)) AS last_month
    FROM `{table}`
    WHERE amount < 0 AND {GOAL_FILTERS[goal]}
      AND transaction_date >= DATE_TRUNC(DATE_SUB({TODAY}, INTERVAL 1 MONTH), MONTH)
    """
    r = _rows(bq, sql)[0]
    return {"mtd": _num(r.get("mtd")), "last_month": _num(r.get("last_month"))}


def fetch_heloc(bq: Any, project_id: str, dataset_id: str) -> dict[str, float] | None:
    sql = f"""
    WITH h AS (
        SELECT account_id, ABS(current_balance) AS bal
        FROM `{project_id}.{dataset_id}.raw_accounts`
        WHERE LOWER(COALESCE(subtype_name, '')) = 'home_equity' AND ABS(COALESCE(current_balance, 0)) > 0
    )
    SELECT
        (SELECT SUM(bal) FROM h) AS balance,
        (SELECT SUM(t.amount) FROM `{project_id}.{dataset_id}.raw_transactions` t JOIN h USING (account_id)
         WHERE t.amount > 0 AND NOT COALESCE(t.pending, FALSE)
           AND t.transaction_date >= DATE_TRUNC({TODAY}, MONTH)) AS paid_mtd
    """
    r = _rows(bq, sql)[0]
    balance = _num(r.get("balance"))
    if balance <= 0:
        return None
    return {"balance": balance, "paid_mtd": _num(r.get("paid_mtd"))}


def fetch_window_stats(bq: Any, table: str, key_expr: str) -> list[dict[str, Any]]:
    """Discretionary spend per key in four 28-day windows ending yesterday (window 0 = most recent)."""
    sql = f"""
    SELECT {key_expr} AS k,
           DIV(DATE_DIFF({YESTERDAY}, transaction_date, DAY), 28) AS win,
           SUM(-amount) AS amt, COUNT(*) AS n
    FROM `{table}`
    WHERE {DISCRETIONARY_WHERE}
      AND transaction_date BETWEEN DATE_SUB({YESTERDAY}, INTERVAL 111 DAY) AND {YESTERDAY}
    GROUP BY 1, 2
    """
    return _rows(bq, sql)


def fetch_new_merchants(bq: Any, table: str) -> list[dict[str, Any]]:
    sql = f"""
    WITH s AS (
        SELECT {MERCHANT} AS merchant, category_name, transaction_date AS d, -amount AS amt
        FROM `{table}` WHERE {SPEND_WHERE}
    ),
    first_seen AS (SELECT merchant, MIN(d) AS first_d FROM s GROUP BY merchant)
    SELECT s.merchant, s.category_name, s.d, s.amt
    FROM s JOIN first_seen f ON s.merchant = f.merchant AND s.d = f.first_d
    WHERE s.d >= DATE_SUB({TODAY}, INTERVAL 3 DAY) AND s.amt >= 75 AND s.merchant != 'Unknown'
    ORDER BY s.amt DESC
    LIMIT 3
    """
    return _rows(bq, sql)


def fetch_stale_accounts(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Accounts that averaged 10+ transactions a month over the past year but have gone quiet."""
    sql = f"""
    SELECT a.display_name AS account, MAX(t.transaction_date) AS last_txn, COUNT(*) AS n_year
    FROM `{project_id}.{dataset_id}.raw_transactions` t
    JOIN `{project_id}.{dataset_id}.raw_accounts` a USING (account_id)
    WHERE t.transaction_date >= DATE_SUB({TODAY}, INTERVAL 365 DAY)
    GROUP BY 1
    HAVING COUNT(*) >= 120 AND MAX(t.transaction_date) < DATE_SUB({TODAY}, INTERVAL {STALE_ACCOUNT_DAYS} DAY)
    """
    return _rows(bq, sql)


def fetch_weekly_trends(bq: Any, table: str, top_n: int = 3) -> list[dict[str, Any]]:
    sql = f"""
    WITH s AS (
        SELECT category_name AS cat, DIV(DATE_DIFF({YESTERDAY}, transaction_date, DAY), 7) AS wk, -amount AS amt
        FROM `{table}`
        WHERE {DISCRETIONARY_WHERE}
          AND transaction_date BETWEEN DATE_SUB({YESTERDAY}, INTERVAL 90 DAY) AND {YESTERDAY}
    ),
    top AS (SELECT cat FROM s GROUP BY cat ORDER BY SUM(amt) DESC LIMIT {int(top_n)})
    SELECT s.cat, s.wk, SUM(s.amt) AS amt FROM s JOIN top USING (cat) GROUP BY 1, 2
    """
    weeks: dict[str, list[float]] = {}
    for r in _rows(bq, sql):
        series = weeks.setdefault(r["cat"], [0.0] * 13)
        wk = int(_num(r["wk"]))
        if 0 <= wk < 13:
            series[12 - wk] += _num(r["amt"])
    ordered = sorted(weeks.items(), key=lambda kv: -sum(kv[1]))
    return [{"category": cat, "weekly": series} for cat, series in ordered]


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(
    r"\bby\s+((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})", re.IGNORECASE
)


def parse_heloc_target(memories: list[str]) -> date | None:
    """Finds a payoff date like 'eliminate the HELOC by December 31, 2026'."""
    for mem in memories:
        if "heloc" not in mem.lower():
            continue
        m = _DATE_RE.search(mem)
        if not m:
            continue
        raw = re.sub(r"[.,]", "", m.group(1))
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def describe_cap(goal: str, cap: float, spend: dict[str, float], today: date) -> dict[str, Any]:
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    mtd = spend["mtd"]
    expected = cap * today.day / days_in_month
    if mtd > cap:
        status = f"over by {_money(mtd - cap)}"
    elif mtd > expected * 1.1:
        left = max(cap - mtd, 0.0)
        days_left = days_in_month - today.day + 1
        status = f"ahead of pace, {_money(left / days_left)}/day left"
    else:
        status = "on pace"
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%B")
    last = spend["last_month"]
    verdict = "under ✅" if last <= cap else f"over by {_money(last - cap)}"
    return {
        "label": goal.capitalize(),
        "progress": min(mtd / cap, 1.0) if cap else 0.0,
        "line": f"{_money(mtd)} of {_money(cap)} · {status}",
        "detail": f"{prev} finished at {_money(last)}, {verdict}",
    }


def describe_heloc(heloc: dict[str, float], target: date | None, today: date) -> dict[str, Any]:
    line = f"{_money(heloc['balance'])} · paid {_money(heloc['paid_mtd'])} this month"
    detail = ""
    if target and target > today:
        months_left = (target.year - today.year) * 12 + target.month - today.month + 1  # includes this month
        needed = heloc["balance"] / months_left
        detail = f"Payoff by {target.strftime('%b %-d, %Y')} needs ~{_money(needed)}/month"
    return {"label": "HELOC", "progress": None, "line": line, "detail": detail}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _windows(rows: list[dict[str, Any]]) -> dict[str, dict[int, tuple[float, int]]]:
    out: dict[str, dict[int, tuple[float, int]]] = {}
    for r in rows:
        out.setdefault(r["k"], {})[int(_num(r["win"]))] = (_num(r["amt"]), int(_num(r["n"])))
    return out


def category_shift_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Habitual categories whose last 4 weeks differ sharply from their usual 4 weeks.

    "Usual" is the median of the three prior 4-week windows, so one large purchase
    (a laptop, a flight) does not become the baseline.
    """
    findings = []
    for cat, wins in _windows(rows).items():
        current = wins.get(0, (0.0, 0))[0]
        prior = sorted(wins.get(i, (0.0, 0))[0] for i in (1, 2, 3))
        baseline = prior[1]
        if sum(wins.get(i, (0.0, 0))[1] for i in (1, 2, 3)) < MIN_HABIT_TRANSACTIONS:
            continue
        delta = current - baseline
        if abs(delta) < 75 or max(current, baseline) < 150:
            continue
        if baseline > 0 and 0.6 < current / baseline < 1.4:
            continue
        if delta > 0:
            ratio = f"{current / baseline:.1f}x" if baseline > 0 else "new"
            text = f"<b>{cat}</b> is up {_money(delta)} ({ratio}): {_money(current)} in the last 4 weeks vs about {_money(baseline)} usually."
            key = f"category_up:{cat.lower()}"
        else:
            text = f"<b>{cat}</b> is down {abs(delta) / baseline:.0%}: {_money(current)} in the last 4 weeks vs about {_money(baseline)} usually. Nice."
            key = f"category_down:{cat.lower()}"
        findings.append({"key": key, "score": abs(delta), "text": text, "repeat_days": TREND_REPEAT_DAYS})
    return findings


def merchant_frequency_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merchants you are visiting at least twice as often as usual."""
    findings = []
    for merchant, wins in _windows(rows).items():
        if merchant == "Unknown":
            continue
        amt, visits = wins.get(0, (0.0, 0))
        usual_visits = sum(wins.get(i, (0.0, 0))[1] for i in (1, 2, 3)) / 3
        usual_amt = sum(wins.get(i, (0.0, 0))[0] for i in (1, 2, 3)) / 3
        if visits < 4 or visits < usual_visits * 2 or amt - usual_amt < 40:
            continue
        text = (
            f"<b>{merchant}</b>: {visits} visits in the last 4 weeks ({_money(amt)}) vs about "
            f"{usual_visits:.0f} usually ({_money(usual_amt)})."
        )
        findings.append(
            {
                "key": f"merchant_freq:{merchant.lower()}",
                "score": amt - usual_amt,
                "text": text,
                "repeat_days": TREND_REPEAT_DAYS,
            }
        )
    return findings


def new_merchant_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for r in rows:
        amt = _num(r["amt"])
        when = _day(r["d"])
        text = f"First purchase ever at <b>{r['merchant']}</b>: {_money(amt)} on {when} ({r.get('category_name') or 'Uncategorized'})."
        findings.append(
            {
                "key": f"new_merchant:{str(r['merchant']).lower()}",
                "score": amt * 0.5,
                "text": text,
                "repeat_days": ALERT_REPEAT_DAYS,
            }
        )
    return findings


def stale_account_findings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings = []
    for r in rows:
        when = _day(r["last_txn"])
        text = (
            f"<b>{r['account']}</b> has had no transactions since {when}, after "
            f"{int(_num(r['n_year']))} in the past year. If it is still in use, reconnect it in Monarch; "
            "until then, trends that include its spending will read low."
        )
        findings.append(
            {
                "key": f"stale_account:{str(r['account']).lower()}",
                "score": STALE_ACCOUNT_SCORE,
                "text": text,
                "repeat_days": 7,
            }
        )
    return findings


def alert_findings(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converts existing alert checks into one-time findings, dropping duplicates and one-off 'subscriptions'."""
    findings: dict[str, dict[str, Any]] = {}
    for a in alerts:
        kind = a.get("type")
        if kind not in ALERT_SCORES or not a.get("alert_key"):
            continue
        if kind == "NEW_SUBSCRIPTION_DETECTED" and "across 1 transaction" in str(a.get("detail", "")):
            continue
        key = f"alert:{a['alert_key']}"
        findings.setdefault(
            key,
            {
                "key": key,
                "score": ALERT_SCORES[kind],
                "text": f"<b>{a.get('title', '')}</b><br>{a.get('detail', '')}",
                "repeat_days": ALERT_REPEAT_DAYS,
            },
        )
    return list(findings.values())


def select_findings(
    candidates: list[dict[str, Any]], recently_shown: dict[str, int], limit: int = MAX_FINDINGS
) -> list[dict[str, Any]]:
    """Highest-impact findings not shown within their repeat window. recently_shown maps key -> days since shown."""
    fresh = [c for c in candidates if recently_shown.get(c["key"], 10_000) >= c["repeat_days"]]
    seen: set[str] = set()
    picked = []
    for c in sorted(fresh, key=lambda c: -c["score"]):
        if c["key"] in seen:
            continue
        seen.add(c["key"])
        picked.append(c)
        if len(picked) == limit:
            break
    return picked


def ensure_history_table(bq: Any, project_id: str, dataset_id: str) -> None:
    bq.query(
        f"CREATE TABLE IF NOT EXISTS `{project_id}.{dataset_id}.brief_history` "
        "(finding_key STRING NOT NULL, shown_at TIMESTAMP NOT NULL)"
    ).result()


def fetch_recently_shown(bq: Any, project_id: str, dataset_id: str) -> dict[str, int]:
    sql = f"""
    SELECT finding_key, DATE_DIFF(CURRENT_DATE('{TZ}'), DATE(MAX(shown_at), '{TZ}'), DAY) AS days_ago
    FROM `{project_id}.{dataset_id}.brief_history`
    WHERE shown_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {ALERT_REPEAT_DAYS} DAY)
    GROUP BY finding_key
    """
    return {r["finding_key"]: int(_num(r["days_ago"])) for r in _rows(bq, sql)}


def record_shown(bq: Any, project_id: str, dataset_id: str, keys: list[str]) -> None:
    if not keys or bigquery is None:
        return
    job_config = bigquery.QueryJobConfig(query_parameters=[bigquery.ArrayQueryParameter("keys", "STRING", keys)])
    bq.query(
        f"INSERT INTO `{project_id}.{dataset_id}.brief_history` (finding_key, shown_at) "
        "SELECT k, CURRENT_TIMESTAMP() FROM UNNEST(@keys) AS k",
        job_config=job_config,
    ).result()


# ---------------------------------------------------------------------------
# Assembly and rendering
# ---------------------------------------------------------------------------


def sparkline(values: list[float]) -> str:
    hi = max(values) if values else 0
    if hi <= 0:
        return SPARKS[0] * len(values)
    return "".join(SPARKS[min(7, round(v / hi * 7))] for v in values)


def trend_arrow(weekly: list[float]) -> str:
    recent = sum(weekly[-4:]) / 4
    prior = sum(weekly[:-4]) / max(len(weekly) - 4, 1)
    if prior <= 0:
        return ""
    ratio = recent / prior
    return " ↑" if ratio >= 1.25 else (" ↓" if ratio <= 0.8 else "")


def progress_bar(fraction: float, length: int = 10) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * length)
    return "█" * filled + "░" * (length - filled)


def _safe(label: str, fn, default):
    try:
        return fn()
    except Exception as e:
        logger.warning(f"Daily brief: {label} failed: {e}")
        return default


def generate_daily_brief(
    bq: Any,
    project_id: str,
    dataset_id: str,
    user_email: str | None = None,
    record_history: bool = True,
) -> dict[str, Any]:
    from app.alerts import (
        check_duplicate_charges,
        check_new_subscriptions,
        check_subscription_overlap,
        check_subscription_price_creep,
        check_utility_seasonal_spike,
        extract_budget_caps,
        filter_suppressed_alerts,
        get_active_suppressions,
    )

    table = f"{project_id}.{dataset_id}.raw_transactions"
    today = datetime.now(ZoneInfo(TZ)).date()

    recent = _safe("recent activity", lambda: fetch_recent(bq, table), None)
    month = _safe("month spend", lambda: fetch_month_spend(bq, table), None)

    def _memories() -> list[str]:
        from app.memory_service import retrieve_user_memories

        return retrieve_user_memories(user_email)

    memories = _safe("memories", _memories, [])
    goals = []
    for goal, cap in extract_budget_caps(memories).items():
        spend = _safe(f"{goal} cap", lambda g=goal: fetch_cap_spend(bq, table, g), None)
        if spend and cap > 0:
            goals.append(describe_cap(goal, cap, spend, today))
    heloc = _safe("heloc", lambda: fetch_heloc(bq, project_id, dataset_id), None)
    if heloc:
        goals.append(describe_heloc(heloc, parse_heloc_target(memories), today))

    candidates: list[dict[str, Any]] = []
    candidates += _safe(
        "category shifts", lambda: category_shift_findings(fetch_window_stats(bq, table, "category_name")), []
    )
    candidates += _safe(
        "merchant frequency", lambda: merchant_frequency_findings(fetch_window_stats(bq, table, MERCHANT)), []
    )
    candidates += _safe("new merchants", lambda: new_merchant_findings(fetch_new_merchants(bq, table)), [])
    stale = _safe(
        "stale accounts", lambda: stale_account_findings(fetch_stale_accounts(bq, project_id, dataset_id)), []
    )
    if stale:
        # A silent account makes spending look lower than it is, so "down" findings are likely artifacts.
        candidates = [c for c in candidates if not c["key"].startswith("category_down:")]
    candidates += stale

    def _alerts() -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for check in (
            check_duplicate_charges,
            check_new_subscriptions,
            check_subscription_price_creep,
            check_utility_seasonal_spike,
            check_subscription_overlap,
        ):
            raw += check(bq, project_id, dataset_id)
        return alert_findings(filter_suppressed_alerts(raw, get_active_suppressions(bq, project_id, dataset_id)))

    candidates += _safe("alert checks", _alerts, [])

    def _recent() -> dict[str, int]:
        ensure_history_table(bq, project_id, dataset_id)
        return fetch_recently_shown(bq, project_id, dataset_id)

    recently_shown = _safe("history", _recent, {})
    findings = select_findings(candidates, recently_shown)
    if record_history:
        _safe("record history", lambda: record_shown(bq, project_id, dataset_id, [f["key"] for f in findings]), None)

    trends = _safe("trends", lambda: fetch_weekly_trends(bq, table), [])

    return {
        "date": today,
        "recent": recent,
        "month": month,
        "goals": goals,
        "findings": findings,
        "trends": trends,
    }


def _recent_text(y: dict[str, Any] | None) -> str:
    if not y:
        return "Recent transactions are unavailable."
    if y["count"] == 0:
        return f"No spending in the past {RECENT_DAYS} days."
    noun = "transaction" if y["count"] == 1 else "transactions"
    text = f"Past {RECENT_DAYS} days: {y['count']} {noun}, {_money(y['total'])}."
    if y["top_merchant"]:
        text += f" Biggest: <b>{y['top_merchant']}</b> {_money(y['top_amount'])}"
        usual = y["top_usual"]
        if y["top_history_count"] >= 3 and usual > 0:
            ratio = y["top_amount"] / usual
            if ratio >= 1.3 or ratio <= 0.7:
                text += f" (usually about {_money(usual)})"
        text += "."
    return text


def _month_text(m: dict[str, float] | None) -> str:
    if not m:
        return ""
    text = f"Month to date: {_money(m['mtd'])}"
    last = m["last_month_same_point"]
    if last > 0:
        diff = m["mtd"] - last
        direction = "more" if diff > 0 else "less"
        text += f", {_money(abs(diff))} {direction} than this point last month"
    return text + "."


def build_brief_card(brief: dict[str, Any]) -> dict[str, Any]:
    day = brief["date"]
    sections: list[dict[str, Any]] = []

    glance = _recent_text(brief["recent"])
    month_text = _month_text(brief["month"])
    if month_text:
        glance += f"<br>{month_text}"
    sections.append({"header": "Recent activity", "widgets": [{"textParagraph": {"text": glance}}]})

    if brief["goals"]:
        widgets = []
        for g in brief["goals"]:
            bar = f"{progress_bar(g['progress'])}  " if g["progress"] is not None else ""
            text = f"{bar}{g['line']}"
            if g["detail"]:
                text += f'<br><font color="#5f6368">{g["detail"]}</font>'
            widgets.append({"decoratedText": {"topLabel": g["label"].upper(), "text": text, "wrapText": True}})
        sections.append({"header": "🎯 Goals", "widgets": widgets})

    if brief["findings"]:
        widgets = [{"textParagraph": {"text": f["text"]}} for f in brief["findings"]]
    else:
        widgets = [{"textParagraph": {"text": "Nothing new stands out today."}}]
    sections.append({"header": "🔍 Worth a look", "widgets": widgets})

    if brief["trends"]:
        lines = [f"{sparkline(t['weekly'])}  {t['category']}{trend_arrow(t['weekly'])}" for t in brief["trends"]]
        sections.append(
            {
                "header": "📈 13-week trends",
                "collapsible": True,
                "uncollapsibleWidgetsCount": 1,
                "widgets": [{"textParagraph": {"text": "<br>".join(lines)}}],
            }
        )

    return {
        "text": f"☀️ *FinSage* daily brief for {day.strftime('%a, %b %-d')}",
        "cardsV2": [
            {
                "cardId": "dailyBrief",
                "card": {
                    "header": {
                        "title": "FinSage",
                        "subtitle": f"Daily brief · {day.strftime('%A, %b %-d')}",
                        "imageUrl": AVATAR_URL,
                        "imageType": "CIRCLE",
                    },
                    "sections": sections,
                },
            }
        ],
    }


def build_brief_markdown(brief: dict[str, Any]) -> str:
    def plain(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s.replace("<br>", "\n").replace("<b>", "**").replace("</b>", "**"))

    lines = [f"☀️ **FinSage daily brief · {brief['date'].strftime('%A, %b %-d')}**", ""]
    lines.append(plain(_recent_text(brief["recent"])))
    month_text = _month_text(brief["month"])
    if month_text:
        lines.append(month_text)
    if brief["goals"]:
        lines += ["", "**🎯 Goals**"]
        for g in brief["goals"]:
            lines.append(f"• {g['label']}: {g['line']}" + (f" ({g['detail']})" if g["detail"] else ""))
    lines += ["", "**🔍 Worth a look**"]
    lines += [f"• {plain(f['text'])}" for f in brief["findings"]] or ["• Nothing new stands out today."]
    if brief["trends"]:
        lines += ["", "**📈 13-week trends**"]
        lines += [f"`{sparkline(t['weekly'])}` {t['category']}{trend_arrow(t['weekly'])}" for t in brief["trends"]]
    return "\n".join(lines)


async def execute_daily_brief(
    bq_client: Any | None = None,
    project_id: str | None = None,
    dataset_id: str | None = None,
    webhook_url: str | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Builds the daily brief and posts it to ALERT_WEBHOOK_URL."""
    target_project = project_id or BQ_PROJECT_ID
    target_dataset = dataset_id or BQ_DATASET_ID
    if not target_project:
        raise ValueError("PROJECT_ID not set.")
    bq = bq_client or bigquery.Client(project=target_project)

    brief = await asyncio.to_thread(generate_daily_brief, bq, target_project, target_dataset, user_email)

    target_webhook = webhook_url or resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if target_webhook and requests:
        try:
            if "chat.googleapis.com" in target_webhook:
                payload = build_brief_card(brief)
            else:
                md = build_brief_markdown(brief)
                payload = {"content": md, "text": md}
            resp = await asyncio.to_thread(requests.post, target_webhook, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Daily brief webhook post failed: {e}")

    return {
        "status": "success",
        "finding_keys": [f["key"] for f in brief["findings"]],
        "goal_count": len(brief["goals"]),
        "webhook_dispatched": webhook_sent,
    }
