"""
Researched category reviews.

Finds merchants whose transactions are uncategorized or split across categories, looks up the
household's live Monarch categories and transaction rules, researches the remaining businesses with
Gemini grounded on Google Search, and proposes one category per merchant in a single signed review
card. The user ticks the suggestions to apply. Applying recategorizes the merchant's transactions in
Monarch Money and creates a Monarch rule so future transactions are categorized at the source. Every
decision (applied or rejected) is remembered so the same merchant is not proposed again.

Only merchant names, category labels, counts and typical amounts are sent to the model. Searches are
limited to the merchant name.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from google.cloud import bigquery
from gql import gql

from app.bq_service import get_bq_client
from app.config import BQ_DATASET_ID, BQ_PROJECT_ID, get_chat_action_target, resolve_secret
from app.monarch_service import (
    CURRENT_PROPOSED_CARD,
    CURRENT_USER_EMAIL,
    _run_async,
    get_monarch_client,
    get_mutation_hmac_secret,
    log_mutation_audit,
)

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover
    genai = None
    types = None

logger = logging.getLogger("monarch-gemini.category_research")

RESEARCH_MODEL = os.getenv("CATEGORY_RESEARCH_MODEL", "gemini-3.8-flash")
REVIEW_EXPIRATION_SECONDS = 3600  # a review lists up to 10 merchants, so allow time to read it
MIN_CONFIDENCE = 0.75
DEFAULT_REVIEW_SIZE = 10
MAX_REVIEW_SIZE = 15
RESEARCH_CHUNK_SIZE = 5
MAX_TXNS_PER_MERCHANT = 200
REJECTION_MEMORY_DAYS = 180  # a rejected merchant is not proposed again for this long
AI_SKIP_MEMORY_DAYS = 90  # merchants the model left alone are not re-researched for this long
LOOKBACK_DAYS = 730
# Impact ranking: spend in the last RECENT_DAYS counts in full, older spend at OLDER_SPEND_WEIGHT, and
# uncategorized spend is multiplied by UNCATEGORIZED_WEIGHT.
RECENT_DAYS = 365
OLDER_SPEND_WEIGHT = 0.5
UNCATEGORIZED_WEIGHT = 1.5

_PENDING_REVIEWS: dict[str, dict] = {}
_TABLES_READY = False


def _table(name: str) -> str:
    return f"`{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{name}`"


def ensure_review_tables(bq: bigquery.Client) -> None:
    """Creates the review and decision tables once per process."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    bq.query(
        f"""
        CREATE TABLE IF NOT EXISTS {_table("pending_category_reviews")} (
            review_id STRING NOT NULL,
            user_email STRING NOT NULL,
            items_json STRING NOT NULL,
            created_at TIMESTAMP NOT NULL,
            status STRING NOT NULL,
            signature STRING NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {_table("merchant_category_decisions")} (
            merchant STRING NOT NULL,
            category_id STRING,
            category_name STRING,
            decision STRING NOT NULL,
            source STRING,
            review_id STRING,
            user_email STRING,
            decided_at TIMESTAMP NOT NULL,
            monarch_rule_id STRING
        );
        ALTER TABLE {_table("merchant_category_decisions")} ADD COLUMN IF NOT EXISTS monarch_rule_id STRING;
        """
    ).result()
    _TABLES_READY = True


# -----------------------------------------------------------------------------
# Signing
# -----------------------------------------------------------------------------


def generate_review_signature(review_id: str, item_count: int, user_email: str, timestamp: int) -> str:
    """Signs a review card. The merchant to category mapping stays server-side, keyed by review_id."""
    key = get_mutation_hmac_secret().encode("utf-8")
    payload = f"category_review:v1:{review_id}:{item_count}:{user_email.strip().lower()}:{timestamp}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_review_signature(
    review_id: str,
    item_count: int,
    user_email: str,
    timestamp: int,
    signature: str,
    max_age_seconds: int = REVIEW_EXPIRATION_SECONDS,
) -> tuple[bool, str]:
    if not signature:
        return False, "Missing cryptographic signature for category review."
    age = abs(int(datetime.now(UTC).timestamp()) - timestamp)
    if age > max_age_seconds:
        return False, f"Category review expired (card age: {age}s > limit: {max_age_seconds}s). Start a new review."
    expected = generate_review_signature(review_id, item_count, user_email, timestamp)
    if not secrets.compare_digest(expected, signature):
        return False, "Cryptographic signature mismatch. Review parameters may have been altered."
    return True, "Valid"


# -----------------------------------------------------------------------------
# Monarch lookup: live categories and transaction rules
# -----------------------------------------------------------------------------

# Monarch's web app uses these operations; the Python client has no wrapper for rules.
_RULES_QUERY = gql(
    """
    query GetTransactionRules {
      transactionRules {
        id
        merchantCriteria { operator value }
        merchantNameCriteria { operator value }
        originalStatementCriteria { operator value }
        amountCriteria { operator }
        categoryIds
        accountIds
        setCategoryAction { id name }
      }
    }
    """
)
_CREATE_RULE_MUTATION = gql(
    """
    mutation Common_CreateTransactionRuleMutationV2($input: CreateTransactionRuleInput!) {
      createTransactionRuleV2(input: $input) {
        transactionRule { id }
        errors { message code fieldErrors { field messages } }
      }
    }
    """
)


def parse_monarch_rules(raw_rules: list[dict]) -> list[dict]:
    """
    Keeps the rules that categorize by merchant name alone. Rules that also filter on amount,
    account, current category or statement text only cover some of a merchant's transactions, so
    they are not treated as the merchant's category.
    """
    rules = []
    for r in raw_rules or []:
        action = r.get("setCategoryAction") or {}
        criteria = (r.get("merchantNameCriteria") or []) + (r.get("merchantCriteria") or [])
        conditional = any(
            r.get(k) for k in ("originalStatementCriteria", "amountCriteria", "categoryIds", "accountIds")
        )
        if not action.get("id") or not criteria or conditional:
            continue
        rules.append(
            {
                "id": str(r.get("id")),
                "category_id": str(action["id"]),
                "category_name": action.get("name") or "",
                "criteria": [(str(c.get("operator", "")).lower(), str(c.get("value", ""))) for c in criteria],
            }
        )
    return rules


def matching_monarch_rule(merchant: str, rules: list[dict]) -> dict | None:
    """The first Monarch rule whose merchant criteria match this merchant name."""
    name = merchant.strip().lower()
    for rule in rules:
        for op, value in rule["criteria"]:
            v = value.strip().lower()
            if v and ((op == "eq" and name == v) or (op == "contains" and v in name)):
                return rule
    return None


async def fetch_monarch_rules(client) -> list[dict]:
    resp = await client.gql_call(operation="GetTransactionRules", graphql_query=_RULES_QUERY)
    return parse_monarch_rules(resp.get("transactionRules") or [])


async def fetch_live_categories(client) -> list[dict]:
    """Categories as they exist in Monarch right now, in the same shape as load_categories."""
    raw = await client.get_transaction_categories()
    cats = []
    for c in raw.get("categories", []):
        group = c.get("group") or {}
        group_name = group.get("name") if isinstance(group, dict) else str(group or "")
        group_type = str(group.get("type", "")).lower() if isinstance(group, dict) else ""
        cats.append(
            {
                "category_id": str(c.get("id")),
                "category_name": str(c.get("name")),
                "group_name": group_name,
                # Income and transfer categories are never offered for a purchase.
                "is_income": group_type in ("income", "transfer") or str(group_name).lower() in ("income", "transfers"),
            }
        )
    return [c for c in cats if c["category_id"] and c["category_name"]]


async def load_from_monarch() -> tuple[list[dict], list[dict]]:
    """(rules, categories) from Monarch. Either may be empty if Monarch is unreachable."""
    client = await get_monarch_client()
    rules, categories = [], []
    try:
        rules = await fetch_monarch_rules(client)
    except Exception as e:
        logger.warning(f"Could not read Monarch transaction rules: {e}")
    try:
        categories = await fetch_live_categories(client)
    except Exception as e:
        logger.warning(f"Could not read Monarch categories: {e}")
    return rules, categories


async def create_monarch_rule(client, merchant: str, category_id: str) -> str:
    """Creates a Monarch rule: merchant name equals `merchant` -> category. Returns the rule id."""
    rule_input = {
        "merchantNameCriteria": [{"operator": "eq", "value": merchant}],
        "setCategoryAction": category_id,
        # Past transactions are updated one by one by the review itself, so they can be counted.
        "applyToExistingTransactions": False,
    }
    resp = await client.gql_call(
        operation="Common_CreateTransactionRuleMutationV2",
        graphql_query=_CREATE_RULE_MUTATION,
        variables={"input": rule_input},
    )
    payload = resp.get("createTransactionRuleV2") or {}
    errors = payload.get("errors") or {}
    if errors.get("message") or errors.get("fieldErrors"):
        raise RuntimeError(errors.get("message") or str(errors.get("fieldErrors")))
    rule_id = (payload.get("transactionRule") or {}).get("id")
    if not rule_id:
        raise RuntimeError("Monarch returned no rule id")
    return str(rule_id)


# -----------------------------------------------------------------------------
# Candidates and memory
# -----------------------------------------------------------------------------


def load_categories(bq: bigquery.Client) -> list[dict]:
    """Monarch categories synced into BigQuery: id, name and group."""
    sql = f"""
    SELECT category_id, category_name, group_name,
        -- Monarch files paychecks under an "Income" group without always setting is_income.
        -- Transfer categories are excluded the same way: they are never right for a purchase.
        COALESCE(is_income, FALSE) OR LOWER(COALESCE(group_name, '')) IN ('income', 'transfers') AS is_income
    FROM {_table("raw_categories")}
    WHERE category_id IS NOT NULL AND category_name IS NOT NULL
    ORDER BY group_name, category_name
    """
    return [dict(r.items()) for r in bq.query(sql).result()]


def load_decisions(bq: bigquery.Client) -> list[dict]:
    """Latest decision per merchant."""
    sql = f"""
    SELECT merchant, category_id, category_name, decision, decided_at
    FROM {_table("merchant_category_decisions")}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(merchant) ORDER BY decided_at DESC) = 1
    """
    return [dict(r.items()) for r in bq.query(sql).result()]


def find_review_candidates(bq: bigquery.Client, pool_size: int = 60) -> list[dict]:
    """
    Merchants with uncategorized transactions or transactions split across categories, ranked by
    impact: dollars that are uncategorized or filed away from the merchant's main category. The last
    12 months count in full and older spend at half; uncategorized spend counts 1.5x, because it is
    missing from every category report rather than sitting in a slightly wrong one.
    Merchants that touch transfer or income categories are left out: those are payments between
    accounts and paychecks, not purchases.
    """
    sql = f"""
    WITH tx AS (
        SELECT
            COALESCE(t.clean_merchant_name, t.merchant_name) AS merchant,
            t.category_id,
            COALESCE(t.category_name, 'Uncategorized') AS category_name,
            ABS(t.amount) AS amt,
            ABS(t.amount) * IF(t.transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {RECENT_DAYS} DAY),
                               1.0, {OLDER_SPEND_WEIGHT}) AS weighted_amt,
            t.category_id IS NULL OR LOWER(COALESCE(t.category_name, 'Uncategorized')) = 'uncategorized'
                AS is_uncategorized,
            t.is_recurring,
            c.group_name,
            c.is_income
        FROM {_table("raw_transactions")} t
        LEFT JOIN {_table("raw_categories")} c USING (category_id)
        WHERE t.pending IS NOT TRUE
          AND COALESCE(t.clean_merchant_name, t.merchant_name) IS NOT NULL
          AND t.transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {LOOKBACK_DAYS} DAY)
    ),
    by_cat AS (
        SELECT
            merchant, category_name, LOGICAL_OR(is_uncategorized) AS is_uncategorized, COUNT(*) AS n,
            SUM(amt) AS amt, SUM(weighted_amt) AS weighted_amt
        FROM tx
        GROUP BY 1, 2
    ),
    splits AS (
        SELECT
            merchant,
            ARRAY_AGG(STRUCT(category_name, n) ORDER BY n DESC) AS categories,
            COUNT(*) AS category_count,
            -- Dollars in categories other than the main (largest) one, plus uncategorized dollars.
            SUM(amt) - MAX(IF(is_uncategorized, 0, amt)) AS misfiled_amount,
            {UNCATEGORIZED_WEIGHT} * SUM(IF(is_uncategorized, weighted_amt, 0))
                + SUM(IF(is_uncategorized, 0, weighted_amt)) - MAX(IF(is_uncategorized, 0, weighted_amt))
                AS impact_score
        FROM by_cat
        GROUP BY 1
    ),
    m AS (
        SELECT
            merchant,
            COUNT(*) AS txn_count,
            ROUND(SUM(amt), 2) AS total_amount,
            ROUND(APPROX_QUANTILES(amt, 2)[OFFSET(1)], 2) AS typical_amount,
            COUNTIF(is_uncategorized) AS uncategorized_count,
            LOGICAL_OR(COALESCE(is_recurring, FALSE)) AS is_recurring,
            LOGICAL_OR(COALESCE(is_income, FALSE) OR LOWER(COALESCE(group_name, '')) = 'income') AS touches_income,
            LOGICAL_OR(LOWER(COALESCE(group_name, '')) LIKE '%transfer%') AS touches_transfer
        FROM tx
        GROUP BY 1
    )
    SELECT
        m.merchant, m.txn_count, m.total_amount, m.typical_amount, m.uncategorized_count, m.is_recurring,
        ROUND(s.misfiled_amount, 2) AS misfiled_amount, ROUND(s.impact_score, 2) AS impact_score, s.categories
    FROM m
    JOIN splits s USING (merchant)
    WHERE NOT m.touches_income
      AND NOT m.touches_transfer
      AND (m.uncategorized_count > 0 OR s.category_count > 1)
    ORDER BY s.impact_score DESC, m.uncategorized_count DESC, m.txn_count DESC
    LIMIT {int(pool_size)}
    """
    rows = []
    for r in bq.query(sql).result():
        row = dict(r.items())
        # BigQuery returns NUMERIC as Decimal, which json.dumps (prompt, stored review) cannot encode.
        for key in ("total_amount", "typical_amount", "misfiled_amount", "impact_score"):
            row[key] = float(row[key]) if row.get(key) is not None else None
        row["txn_count"] = int(row["txn_count"])
        row["uncategorized_count"] = int(row["uncategorized_count"])
        row["categories"] = [{"category_name": c["category_name"], "n": int(c["n"])} for c in row["categories"]]
        rows.append(row)
    return rows


def split_by_memory(
    candidates: list[dict],
    decisions: list[dict],
    now: datetime | None = None,
    monarch_rules: list[dict] | None = None,
):
    """
    Returns (rule_based, to_research). Order of precedence for each merchant:
    a recent rejection drops it; an existing Monarch rule supplies the category; a category the
    household confirmed in an earlier review supplies it; a recent "leave it" from the model drops it;
    anything else is researched.
    """
    now = now or datetime.now(UTC)
    latest = {str(d["merchant"]).strip().lower(): d for d in decisions}
    rule_based, to_research = [], []
    for cand in candidates:
        d = latest.get(str(cand["merchant"]).strip().lower()) or {}
        decided_at = d.get("decided_at")
        age_days = (now - decided_at).days if isinstance(decided_at, datetime) else 0
        decision = d.get("decision")
        monarch_rule = matching_monarch_rule(cand["merchant"], monarch_rules or [])

        if decision == "REJECTED" and age_days < REJECTION_MEMORY_DAYS:
            continue
        if monarch_rule:
            rule_based.append(
                {
                    **cand,
                    "category_id": monarch_rule["category_id"],
                    "category_name": monarch_rule["category_name"],
                    "confidence": 1.0,
                    "reason": "Your Monarch rule files this merchant here; older transactions predate it.",
                    "source": "monarch_rule",
                    "monarch_rule_id": monarch_rule["id"],
                }
            )
        elif decision == "ACCEPTED" and d.get("category_id"):
            rule_based.append(
                {
                    **cand,
                    "category_id": str(d["category_id"]),
                    "category_name": d.get("category_name") or "",
                    "confidence": 1.0,
                    "reason": "You confirmed this category for this merchant before.",
                    "source": "rule",
                }
            )
        elif decision == "AI_SKIPPED" and age_days < AI_SKIP_MEMORY_DAYS:
            continue
        else:
            to_research.append(cand)
    return rule_based, to_research


# -----------------------------------------------------------------------------
# Research
# -----------------------------------------------------------------------------

_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string"},
                    "action": {"type": "string", "enum": ["recategorize", "leave"]},
                    "category": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["merchant", "action", "category", "confidence", "reason"],
            },
        }
    },
    "required": ["suggestions"],
}


def _genai_client():
    if not genai:
        raise RuntimeError("google-genai is not installed")
    key = resolve_secret("gemini-api-key", "GEMINI_API_KEY")
    if key:
        return genai.Client(api_key=key)
    return genai.Client(vertexai=True, project=BQ_PROJECT_ID, location=os.getenv("REGION", "us-central1"))


def build_research_prompt(candidates: list[dict], categories: list[dict], rules: list[dict]) -> str:
    cat_lines = "\n".join(
        f"- {c['category_name']} (group: {c.get('group_name') or 'Other'})"
        for c in categories
        if not c.get("is_income")
    )
    rule_lines = "\n".join(f"- {r['merchant']} -> {r['category_name']}" for r in rules[:40]) or "- (none yet)"
    merchant_lines = "\n".join(
        json.dumps(
            {
                "merchant": c["merchant"],
                "transactions": c["txn_count"],
                "typical_amount": c.get("typical_amount"),
                "recurring": bool(c.get("is_recurring")),
                "current_categories": {x["category_name"]: x["n"] for x in c["categories"]},
            }
        )
        for c in candidates
    )
    return (
        "You categorize a household's card transactions into their budgeting app's categories.\n\n"
        f"ALLOWED CATEGORIES (use the exact name):\n{cat_lines}\n\n"
        f"CATEGORIES THIS HOUSEHOLD HAS CONFIRMED FOR OTHER MERCHANTS (follow their conventions):\n{rule_lines}\n\n"
        f"MERCHANTS TO REVIEW (one JSON object per line):\n{merchant_lines}\n\n"
        "For each merchant:\n"
        "1. Work out what the business is. Strip payment-processor prefixes and store numbers "
        "(SQ *, TST*, PAYPAL *, SP *, #1234). If it is not a widely known brand, search the web for the "
        "merchant name only, at most two searches per merchant. Never put amounts or anything other than "
        "the merchant name in a search.\n"
        "2. Pick the single best category from the allowed list, using the typical amount and recurrence "
        "as hints (a small fixed monthly charge is usually a subscription).\n"
        "3. Use action 'leave' (with the current main category) when the merchant is a transfer, bill "
        "payment or refund, when it legitimately sells across categories so the split is correct, or when "
        "you cannot identify it.\n"
        "4. confidence is your probability that the category is right: 0.9+ only when you identified the "
        "business with certainty.\n"
        "5. reason: one short sentence saying what the business is.\n"
        "Return one suggestion per merchant, with the merchant string exactly as given."
    )


def research_chunk(candidates: list[dict], categories: list[dict], rules: list[dict], client=None) -> list[dict]:
    """One grounded Gemini call for a handful of merchants. Returns the raw suggestions."""
    client = client or _genai_client()
    resp = client.models.generate_content(
        model=RESEARCH_MODEL,
        contents=build_research_prompt(candidates, categories, rules),
        config=types.GenerateContentConfig(
            temperature=0.0,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
            response_schema=_RESEARCH_SCHEMA,
        ),
    )
    try:
        return json.loads(resp.text or "{}").get("suggestions", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.warning(f"Category research returned unparseable output: {e}")
        return []


def validate_suggestions(
    candidates: list[dict], suggestions: list[dict], categories: list[dict]
) -> tuple[list[dict], list[dict]]:
    """
    Matches model output back to candidates. Returns (accepted, skipped): accepted items carry a real
    category id; skipped are merchants the model left alone, was unsure about, or answered with a
    category that does not exist.
    """
    by_name = {c["category_name"].strip().lower(): c for c in categories if not c.get("is_income")}
    by_merchant = {str(s.get("merchant", "")).strip().lower(): s for s in suggestions}
    accepted, skipped = [], []
    for cand in candidates:
        s = by_merchant.get(cand["merchant"].strip().lower())
        if not s:
            continue  # no answer: try again next review
        cat = by_name.get(str(s.get("category", "")).strip().lower())
        try:
            confidence = float(s.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        main_current = cand["categories"][0]["category_name"] if cand["categories"] else None
        if (
            s.get("action") != "recategorize"
            or not cat
            or confidence < MIN_CONFIDENCE
            or (len(cand["categories"]) == 1 and cat["category_name"] == main_current)
        ):
            skipped.append({**cand, "reason": str(s.get("reason", ""))[:200]})
            continue
        accepted.append(
            {
                **cand,
                "category_id": str(cat["category_id"]),
                "category_name": cat["category_name"],
                "confidence": round(confidence, 2),
                "reason": str(s.get("reason", ""))[:200],
                "source": "research",
            }
        )
    return accepted, skipped


def research_candidates(
    candidates: list[dict], categories: list[dict], rules: list[dict], client=None
) -> tuple[list[dict], list[dict]]:
    """Researches candidates in parallel chunks."""
    if not candidates:
        return [], []
    client = client or _genai_client()
    chunks = [candidates[i : i + RESEARCH_CHUNK_SIZE] for i in range(0, len(candidates), RESEARCH_CHUNK_SIZE)]

    def _one(chunk):
        try:
            return validate_suggestions(chunk, research_chunk(chunk, categories, rules, client), categories)
        except Exception as e:
            logger.warning(f"Category research chunk failed: {e}")
            return [], []

    accepted, skipped = [], []
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
        for a, s in pool.map(_one, chunks):
            accepted.extend(a)
            skipped.extend(s)
    return accepted, skipped


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------


def record_decisions(bq: bigquery.Client, items: list[dict], decision: str, source: str, review_id, user_email):
    if not items:
        return
    now = datetime.now(UTC).isoformat()
    rows = [
        {
            "merchant": i["merchant"],
            "category_id": i.get("category_id"),
            "category_name": i.get("category_name"),
            "decision": decision,
            "source": source,
            "review_id": review_id,
            "user_email": user_email,
            "decided_at": now,
            "monarch_rule_id": i.get("monarch_rule_id"),
        }
        for i in items
    ]
    try:
        errors = bq.insert_rows_json(f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.merchant_category_decisions", rows)
        if errors:
            logger.warning(f"Category decision insert errors: {errors}")
    except Exception as e:
        logger.warning(f"Could not record category decisions: {e}")


def _transactions_to_change(bq: bigquery.Client, merchant: str, category_id: str) -> tuple[list[str], float]:
    """IDs of the merchant's transactions not yet in category_id, and their total (absolute) amount."""
    sql = f"""
    SELECT transaction_id, ABS(amount) AS amt
    FROM {_table("raw_transactions")}
    WHERE LOWER(COALESCE(clean_merchant_name, merchant_name)) = LOWER(@merchant)
      AND pending IS NOT TRUE
      AND (category_id IS NULL OR category_id != @category_id)
    ORDER BY transaction_date DESC
    LIMIT {MAX_TXNS_PER_MERCHANT}
    """
    cfg = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("merchant", "STRING", merchant),
            bigquery.ScalarQueryParameter("category_id", "STRING", category_id),
        ]
    )
    rows = list(bq.query(sql, job_config=cfg).result())
    return [str(r["transaction_id"]) for r in rows], round(sum(float(r["amt"] or 0) for r in rows), 2)


def save_review(bq: bigquery.Client, review: dict) -> None:
    _PENDING_REVIEWS[review["review_id"]] = review
    try:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("review_id", "STRING", review["review_id"]),
                bigquery.ScalarQueryParameter("user_email", "STRING", review["user_email"]),
                bigquery.ScalarQueryParameter("items_json", "STRING", json.dumps(review["items"], default=str)),
                bigquery.ScalarQueryParameter("sig", "STRING", review["signature"]),
            ]
        )
        bq.query(
            f"INSERT INTO {_table('pending_category_reviews')} "
            "(review_id, user_email, items_json, created_at, status, signature) "
            "VALUES (@review_id, @user_email, @items_json, CURRENT_TIMESTAMP(), 'PENDING', @sig)",
            job_config=cfg,
        ).result()
    except Exception as e:
        logger.warning(f"Could not persist category review {review['review_id']}: {e}")


def load_review(bq: bigquery.Client, review_id: str) -> dict | None:
    if review_id in _PENDING_REVIEWS:
        return _PENDING_REVIEWS[review_id]
    try:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("review_id", "STRING", review_id)]
        )
        rows = list(
            bq.query(
                f"SELECT review_id, user_email, items_json, status, signature "
                f"FROM {_table('pending_category_reviews')} WHERE review_id = @review_id LIMIT 1",
                job_config=cfg,
            ).result()
        )
    except Exception as e:
        logger.warning(f"Could not read category review {review_id}: {e}")
        return None
    if not rows:
        return None
    r = dict(rows[0].items())
    review = {**r, "items": json.loads(r.pop("items_json"))}
    _PENDING_REVIEWS[review_id] = review
    return review


def set_review_status(bq: bigquery.Client, review_id: str, status: str) -> None:
    if review_id in _PENDING_REVIEWS:
        _PENDING_REVIEWS[review_id]["status"] = status
    try:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("status", "STRING", status),
                bigquery.ScalarQueryParameter("review_id", "STRING", review_id),
            ]
        )
        bq.query(
            f"UPDATE {_table('pending_category_reviews')} SET status = @status WHERE review_id = @review_id",
            job_config=cfg,
        ).result()
    except Exception as e:
        logger.warning(f"Could not update category review {review_id}: {e}")


# -----------------------------------------------------------------------------
# Proposal
# -----------------------------------------------------------------------------


def propose_category_review(
    user_email: str,
    max_merchants: int = DEFAULT_REVIEW_SIZE,
    bq: bigquery.Client | None = None,
    research_client=None,
) -> dict:
    """Builds a signed review of up to max_merchants category fixes. Blocking; run it off the event loop."""
    if not user_email or user_email == "unknown":
        return {"status": "error", "message": "Cannot attribute this review to a Chat user, so it was not created."}
    max_merchants = max(1, min(int(max_merchants or DEFAULT_REVIEW_SIZE), MAX_REVIEW_SIZE))
    bq = bq or get_bq_client(BQ_PROJECT_ID)
    ensure_review_tables(bq)

    # Start from what is in Monarch now: its categories and the household's own rules.
    monarch_rules, live_categories = [], []
    try:
        monarch_rules, live_categories = _run_async(load_from_monarch())
    except Exception as e:
        logger.warning(f"Monarch lookup failed; using the last synced categories and no rules: {e}")
    categories = live_categories or load_categories(bq)
    decisions = load_decisions(bq)
    conventions = [d for d in decisions if d.get("decision") == "ACCEPTED" and d.get("category_name")]
    conventions += [
        {"merchant": value, "category_name": r["category_name"]} for r in monarch_rules for _, value in r["criteria"]
    ]
    candidates = find_review_candidates(bq)
    rule_based, to_research = split_by_memory(candidates, decisions, monarch_rules=monarch_rules)

    # Rule-based and researched merchants compete on the same impact ranking. Take the top of it,
    # with a few extra to research because some come back as "leave" or low confidence.
    by_merchant = {i["merchant"]: i for i in rule_based}
    researchable = {id(c) for c in to_research}
    pool = [c for c in candidates if c["merchant"] in by_merchant or id(c) in researchable]
    pool = pool[: max_merchants + RESEARCH_CHUNK_SIZE]
    items = [by_merchant[c["merchant"]] for c in pool if c["merchant"] in by_merchant]
    batch = [c for c in pool if id(c) in researchable]
    if batch:
        researched, skipped = research_candidates(batch, categories, conventions, research_client)
        items += researched
        record_decisions(bq, skipped, "AI_SKIPPED", "research", None, user_email)

    for item in items:
        item["transaction_ids"], item["change_amount"] = _transactions_to_change(
            bq, item["merchant"], item["category_id"]
        )
        # A merchant already covered by a Monarch rule needs no new one.
        item["create_rule"] = item.get("source") != "monarch_rule"
    # Biggest fixes first: the dollars that actually move, then how many transactions.
    items = [i for i in items if i["transaction_ids"]]
    items.sort(key=lambda i: (i["change_amount"], len(i["transaction_ids"])), reverse=True)
    items = items[:max_merchants]
    if not items:
        return {"status": "none_found", "message": "No confident category fixes found right now."}

    review_id = uuid.uuid4().hex[:12]
    timestamp = int(datetime.now(UTC).timestamp())
    signature = generate_review_signature(review_id, len(items), user_email, timestamp)
    review = {
        "review_id": review_id,
        "user_email": user_email,
        "items": items,
        "status": "PENDING",
        "signature": signature,
    }
    save_review(bq, review)
    card = build_review_card(review_id, items, user_email, timestamp, signature)
    return {
        "status": "confirmation_required",
        "review_id": review_id,
        "merchant_count": len(items),
        "transaction_count": sum(len(i["transaction_ids"]) for i in items),
        "amount": round(sum(i["change_amount"] for i in items), 2),
        "suggestions": [
            {
                "merchant": i["merchant"],
                "category": i["category_name"],
                "amount": i["change_amount"],
                "reason": i["reason"],
            }
            for i in items
        ],
        "card": card,
    }


def start_category_review(max_merchants: int = 10) -> str:
    """
    Tool: Looks up the household's Monarch categories and rules, researches merchants whose transactions
    are uncategorized or split across categories using web search, and posts one review card listing
    up to max_merchants suggested category fixes. The user ticks which to apply; applying updates the
    transactions in Monarch and adds a Monarch rule for future ones. Nothing changes until they do.
    """
    try:
        res = propose_category_review(CURRENT_USER_EMAIL.get(), max_merchants)
        if res.get("card"):
            CURRENT_PROPOSED_CARD.set(res["card"])
        return json.dumps({k: v for k, v in res.items() if k != "card"}, default=str)
    except Exception as e:
        logger.error(f"Category review failed: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Category review failed: {e}"})


# -----------------------------------------------------------------------------
# Cards
# -----------------------------------------------------------------------------


def build_review_card(review_id: str, items: list[dict], user_email: str, timestamp: int, signature: str) -> dict:
    apply_action = get_chat_action_target("apply_category_review")
    reject_action = get_chat_action_target("reject_category_review")
    txn_total = sum(len(i["transaction_ids"]) for i in items)
    amount_total = sum(i.get("change_amount") or 0 for i in items)

    widgets = []
    for item in items:
        current = ", ".join(f"{c['category_name']} ({c['n']})" for c in item["categories"][:3])
        badge = {"monarch_rule": "✔ your Monarch rule", "rule": "✔ confirmed before"}.get(
            item.get("source"), f"{int(item['confidence'] * 100)}% sure"
        )
        if item.get("create_rule"):
            badge += " · adds Monarch rule"
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": f"${item.get('change_amount') or 0:,.0f} · {len(item['transaction_ids'])} txns · now: {current}",
                    "text": f"<b>{html.escape(item['merchant'])}</b> → <b>{html.escape(item['category_name'])}</b>",
                    "bottomLabel": f"{badge} · {item['reason']}"[:200],
                    "wrapText": True,
                }
            }
        )
    widgets.append(
        {
            "selectionInput": {
                "name": "selected",
                "label": "Apply these",
                "type": "CHECK_BOX",
                "items": [
                    {"text": f"{i['merchant']} → {i['category_name']}", "value": str(n), "selected": True}
                    for n, i in enumerate(items)
                ],
            }
        }
    )
    common = [
        {"key": "review_id", "value": review_id},
        {"key": "item_count", "value": str(len(items))},
        {"key": "user_email", "value": user_email},
        {"key": "timestamp", "value": str(timestamp)},
        {"key": "signature", "value": signature},
    ]
    widgets.append(
        {
            "buttonList": {
                "buttons": [
                    {
                        "text": "Apply selected",
                        "color": {"red": 0.12, "green": 0.53, "blue": 0.90},
                        "onClick": {
                            "action": {
                                "function": apply_action,
                                "parameters": [{"key": "action", "value": "apply_category_review"}, *common],
                            }
                        },
                    },
                    {
                        "text": "Reject all",
                        "onClick": {
                            "action": {
                                "function": reject_action,
                                "parameters": [{"key": "action", "value": "reject_category_review"}, *common],
                            }
                        },
                    },
                ]
            }
        }
    )
    widgets.append(
        {
            "textParagraph": {
                "text": "<i>Applying updates these transactions in Monarch and adds a Monarch rule so future ones "
                "are categorized automatically. Unticked suggestions won't be proposed again.</i>"
            }
        }
    )
    return {
        "cardId": f"category_review_{review_id}",
        "card": {
            "header": {
                "title": "Category Review",
                "subtitle": f"{len(items)} merchants · {txn_total} transactions · ${amount_total:,.0f}",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [{"widgets": widgets}],
        },
    }


def build_review_result_card(review_id: str, applied: list[dict], rejected: list[dict], failed: int) -> dict:
    rule_note = {
        "created": " · Monarch rule added",
        "exists": " · Monarch rule already in place",
        "failed": " · ⚠️ Monarch rule not added",
    }
    lines = [
        f"✅ <b>{html.escape(i['merchant'])}</b> → {html.escape(i['category_name'])} ({i['applied_count']} txns)"
        f"{rule_note.get(i.get('rule_status'), '')}"
        for i in applied
    ]
    lines += [f"🚫 {html.escape(i['merchant'])} (kept as is)" for i in rejected]
    if failed:
        lines.append(f"⚠️ {failed} transaction update(s) failed; they will be offered again.")
    return {
        "cardId": f"category_review_done_{review_id}",
        "card": {
            "header": {"title": "Category Review Applied" if applied else "Category Review Closed"},
            "sections": [{"widgets": [{"textParagraph": {"text": "<br>".join(lines) or "No changes."}}]}],
        },
    }


# -----------------------------------------------------------------------------
# Applying
# -----------------------------------------------------------------------------


async def apply_category_review(
    review_id: str,
    selected_indexes: list[int] | None,
    user_email: str,
    bq: bigquery.Client | None = None,
    client=None,
) -> dict:
    """
    Writes the selected suggestions back to Monarch and BigQuery. Selected merchants become ACCEPTED
    rules; the rest are recorded as REJECTED. selected_indexes=None rejects the whole review.
    """
    bq = bq or get_bq_client(BQ_PROJECT_ID)
    review = await asyncio.to_thread(load_review, bq, review_id)
    if not review:
        return {"success": False, "error": "This review was not found or has expired."}
    if review.get("status") != "PENDING":
        return {"success": False, "error": f"This review is already {str(review.get('status')).lower()}."}
    await asyncio.to_thread(set_review_status, bq, review_id, "PROCESSING")

    items = review["items"]
    chosen = sorted({n for n in (selected_indexes or []) if 0 <= n < len(items)})
    selected = [items[n] for n in chosen]
    rejected = [i for n, i in enumerate(items) if n not in chosen]

    applied, failed_total = [], 0
    if selected:
        client = client or await get_monarch_client()
        sem = asyncio.Semaphore(5)

        async def _update(txn_id: str, category_id: str):
            async with sem:
                for attempt in range(2):
                    try:
                        await client.update_transaction(transaction_id=txn_id, category_id=category_id)
                        return True
                    except Exception as e:
                        if attempt == 1:
                            logger.warning(f"Category review {review_id}: update failed for a transaction: {e}")
                            return False
                        await asyncio.sleep(0.5)

        existing_rules = []
        if any(i.get("create_rule") for i in selected):
            try:
                existing_rules = await fetch_monarch_rules(client)
            except Exception as e:
                logger.warning(f"Category review {review_id}: could not re-read Monarch rules: {e}")

        for item in selected:
            results = await asyncio.gather(*(_update(t, item["category_id"]) for t in item["transaction_ids"]))
            ok_ids = [t for t, ok in zip(item["transaction_ids"], results, strict=True) if ok]
            failed_total += len(item["transaction_ids"]) - len(ok_ids)
            if not ok_ids:
                continue
            await asyncio.to_thread(_mirror_to_bq, bq, ok_ids, item["category_id"], item["category_name"])
            done = {**item, "applied_count": len(ok_ids)}
            if item.get("create_rule"):
                covering = matching_monarch_rule(item["merchant"], existing_rules)
                if covering:
                    done.update(monarch_rule_id=covering["id"], rule_status="exists")
                else:
                    try:
                        rule_id = await create_monarch_rule(client, item["merchant"], item["category_id"])
                        done.update(monarch_rule_id=rule_id, rule_status="created")
                    except Exception as e:
                        logger.warning(f"Category review {review_id}: Monarch rule not created: {e}")
                        done["rule_status"] = "failed"
            applied.append(done)

    await asyncio.to_thread(record_decisions, bq, applied, "ACCEPTED", "review", review_id, user_email)
    await asyncio.to_thread(record_decisions, bq, rejected, "REJECTED", "review", review_id, user_email)

    status = "APPLIED" if applied and not failed_total else ("PARTIAL_SUCCESS" if applied else "REJECTED")
    if selected and not applied:
        status = "FAILED"
    await asyncio.to_thread(set_review_status, bq, review_id, status)
    log_mutation_audit(
        action_type="CATEGORY_REVIEW",
        target_id=review_id,
        user_email=user_email,
        status="SUCCESS" if status in ("APPLIED", "REJECTED") else status,
        previous_value=f"{len(items)} merchants proposed",
        new_value=f"{len(applied)} applied, {len(rejected)} rejected",
        signature_valid=True,
        details=json.dumps(
            {
                "applied": [
                    {"merchant": i["merchant"], "count": i["applied_count"], "rule": i.get("rule_status")}
                    for i in applied
                ],
                "rejected": len(rejected),
                "failed_transactions": failed_total,
            }
        ),
    )
    return {
        "success": True,
        "status": status,
        "applied": applied,
        "rejected": rejected,
        "failed_count": failed_total,
        "card": build_review_result_card(review_id, applied, rejected, failed_total),
    }


def _mirror_to_bq(bq: bigquery.Client, txn_ids: list[str], category_id: str, category_name: str) -> None:
    try:
        cfg = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("cat_id", "STRING", category_id),
                bigquery.ScalarQueryParameter("cat_name", "STRING", category_name),
                bigquery.ArrayQueryParameter("ids", "STRING", txn_ids),
            ]
        )
        bq.query(
            f"UPDATE {_table('raw_transactions')} SET category_id = @cat_id, category_name = @cat_name, "
            "updated_at = CURRENT_TIMESTAMP() WHERE transaction_id IN UNNEST(@ids)",
            job_config=cfg,
        ).result()
    except Exception as e:
        logger.warning(f"BigQuery mirror of category review update failed (next sync will repair it): {e}")
