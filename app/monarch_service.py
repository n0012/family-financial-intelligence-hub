"""
Monarch Money Service Module.

Provides:
1. Robust session management and MFA authentication for Monarch Money.
2. Synchronous & asynchronous data ingestion from Monarch Money into BigQuery
   (`raw_accounts`, `raw_categories`, `raw_transactions`).
3. Safe live confirmation read tools for Gemini AFC / Google Chat:
   - `get_live_account_balance`: Live account balance & status direct from Monarch.
   - `get_live_transaction`: Live transaction details & pending status.
   - `request_plaid_refresh`: Cooldown-guarded on-demand institution sync.
"""

import asyncio
import concurrent.futures
import contextvars
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyotp
from google.cloud import bigquery
from monarchmoney import MonarchMoney

from app.bq_service import get_bq_client
from app.config import (
    BQ_DATASET_ID,
    BQ_PROJECT_ID,
    get_account_overrides,
    get_chat_action_target,
    get_decommissioned_account_ids,
    get_excluded_institutions,
    get_rates_config,
    resolve_secret,
)

logger = logging.getLogger("monarch-gemini.monarch_service")

_monarch_client: MonarchMoney | None = None
_lock = asyncio.Lock()

# In-memory cooldown tracking for upstream Plaid refreshes (institution_name_lower -> last_requested_utc)
_PLAID_REFRESH_COOLDOWNS: dict[str, datetime] = {}
PLAID_REFRESH_COOLDOWN_MINUTES = 60

# Context variables for thread-safe request contextualization
CURRENT_USER_EMAIL: contextvars.ContextVar[str] = contextvars.ContextVar("CURRENT_USER_EMAIL", default="unknown")
CURRENT_PROPOSED_CARD: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "CURRENT_PROPOSED_CARD", default=None
)

# HMAC signing configuration for human-in-the-loop mutation confirmation
HMAC_EXPIRATION_SECONDS = 900  # 15 minutes

# Cached category registry
_CATEGORY_CACHE: dict[str, Any] = {
    "by_id": {},
    "by_name": {},
    "last_fetched": 0.0,
}


def _run_async(coro):
    """Executes an async coroutine synchronously, safe inside worker threads or existing loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


async def get_monarch_client(mfa_code: str | None = None) -> MonarchMoney:
    """
    Initializes and authenticates the MonarchMoney client.
    Reuses existing authenticated session where possible.
    """
    global _monarch_client
    async with _lock:
        if _monarch_client is not None and not mfa_code:
            return _monarch_client

        email = resolve_secret("monarch-email", "MONARCH_EMAIL")
        password = resolve_secret("monarch-password", "MONARCH_PASSWORD")
        mfa_secret = resolve_secret("monarch-mfa-secret", "MONARCH_MFA_SECRET")

        if not email or not password:
            raise RuntimeError("MONARCH_EMAIL or MONARCH_PASSWORD not configured.")

        client = MonarchMoney()
        try:
            if mfa_code:
                logger.info(f"Authenticating {email} with explicit MFA code")
                try:
                    await client.login(email=email, password=password)
                except Exception as login_err:
                    if "mfa" in type(login_err).__name__.lower() or "mfa" in str(login_err).lower():
                        await client.multi_factor_authenticate(
                            email=email,
                            password=password,
                            multi_factor_code=mfa_code.strip(),
                        )
                    else:
                        raise login_err
            else:
                clean_secret = (
                    mfa_secret.replace(" ", "").replace("-", "").strip()
                    if (mfa_secret and mfa_secret != "NONE")
                    else None
                )
                try:
                    if clean_secret:
                        logger.info(f"Authenticating {email} using mfa_secret_key")
                        await client.login(
                            email=email,
                            password=password,
                            mfa_secret_key=clean_secret,
                        )
                    else:
                        await client.login(email=email, password=password)
                except Exception as login_err:
                    is_mfa_error = "mfa" in type(login_err).__name__.lower() or "mfa" in str(login_err).lower()
                    if is_mfa_error and clean_secret:
                        totp_code = pyotp.TOTP(clean_secret).now()
                        logger.info(f"Submitting generated TOTP code for {email}")
                        await client.multi_factor_authenticate(
                            email=email,
                            password=password,
                            multi_factor_code=totp_code,
                        )
                    else:
                        raise login_err

            _monarch_client = client
            return client
        except Exception as e:
            logger.error(f"Monarch authentication failed: {e}")
            raise


# =====================================================================
# INGESTION & BIGQUERY SYNC
# =====================================================================


async def sync_all_accounts(
    client: MonarchMoney,
    bq: bigquery.Client,
    now_ts: str,
) -> int:
    """Fetches all accounts from Monarch, applies overrides, and loads into raw_accounts."""
    decommissioned_ids = get_decommissioned_account_ids()
    account_overrides = get_account_overrides()
    excluded_institutions = get_excluded_institutions()
    rates_cfg = get_rates_config()
    default_heloc_apr = rates_cfg.get("default_heloc_apr") or rates_cfg.get("heloc_apr") or 0.0675
    default_mortgage_apr = rates_cfg.get("default_mortgage_apr") or rates_cfg.get("mortgage_apr") or 0.0350
    default_debt_apr = rates_cfg.get("default_debt_apr")

    raw_accounts_data = await client.get_accounts()
    accounts_list = raw_accounts_data.get("accounts", []) if isinstance(raw_accounts_data, dict) else []
    account_rows = []

    for acc in accounts_list:
        acc_id = str(acc.get("id"))
        if acc_id in decommissioned_ids:
            continue

        inst = acc.get("institution") or {}
        inst_raw_name = inst.get("name") if isinstance(inst, dict) else str(inst or "")
        inst_name = inst_raw_name or ""
        if any(exc in inst_name.lower() for exc in excluded_institutions):
            continue

        acc_type = acc.get("type") or {}
        acc_subtype = acc.get("subtype") or {}

        raw_apr = acc.get("interestRate")
        override_apr = account_overrides.get(acc_id, {}).get("interest_rate")

        subtype_str = (acc_subtype.get("name") if isinstance(acc_subtype, dict) else str(acc_subtype or "")).lower()
        type_str = (acc_type.get("name") if isinstance(acc_type, dict) else str(acc_type or "")).lower()
        disp_name_str = str(acc.get("displayName") or "").lower()

        if override_apr is not None:
            try:
                int_rate = float(override_apr)
            except (ValueError, TypeError):
                int_rate = None
        elif raw_apr is not None:
            try:
                int_rate = float(raw_apr)
            except (ValueError, TypeError):
                int_rate = None
        elif "home_equity" in subtype_str or "heloc" in subtype_str or "heloc" in disp_name_str:
            int_rate = float(default_heloc_apr) if default_heloc_apr is not None else None
        elif "mortgage" in subtype_str or "mortgage" in type_str or "mortgage" in disp_name_str:
            int_rate = float(default_mortgage_apr) if default_mortgage_apr is not None else None
        elif type_str == "loan" and not acc.get("isAsset", False):
            int_rate = float(default_debt_apr) if default_debt_apr is not None else None
        else:
            int_rate = None

        account_rows.append(
            {
                "account_id": str(acc.get("id")),
                "account_name": acc.get("displayName") or acc.get("id"),
                "display_name": acc.get("displayName"),
                "type_name": acc_type.get("name") if isinstance(acc_type, dict) else str(acc_type),
                "subtype_name": acc_subtype.get("name") if isinstance(acc_subtype, dict) else str(acc_subtype),
                "current_balance": float(acc.get("currentBalance") or 0.0),
                "available_balance": float(acc.get("availableBalance") or 0.0)
                if acc.get("availableBalance") is not None
                else None,
                "credit_limit": float(acc.get("creditLimit") or 0.0) if acc.get("creditLimit") is not None else None,
                "interest_rate": int_rate,
                "institution_name": inst_name,
                "is_asset": acc.get("isAsset", False),
                "updated_at": now_ts,
            }
        )

    if not account_rows:
        return 0

    account_schema = [
        bigquery.SchemaField("account_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("account_name", "STRING"),
        bigquery.SchemaField("display_name", "STRING"),
        bigquery.SchemaField("type_name", "STRING"),
        bigquery.SchemaField("subtype_name", "STRING"),
        bigquery.SchemaField("current_balance", "NUMERIC"),
        bigquery.SchemaField("available_balance", "NUMERIC"),
        bigquery.SchemaField("credit_limit", "NUMERIC"),
        bigquery.SchemaField("interest_rate", "NUMERIC"),
        bigquery.SchemaField("institution_name", "STRING"),
        bigquery.SchemaField("is_asset", "BOOLEAN"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    job_config = bigquery.LoadJobConfig(
        schema=account_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    table_ref = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_accounts"
    await asyncio.to_thread(lambda: bq.load_table_from_json(account_rows, table_ref, job_config=job_config).result())
    return len(account_rows)


async def sync_all_categories(
    client: MonarchMoney,
    bq: bigquery.Client,
    now_ts: str,
) -> int:
    """Fetches all categories from Monarch and writes to raw_categories."""
    raw_cat_data = await client.get_transaction_categories()
    cats_list = raw_cat_data.get("categories", []) if isinstance(raw_cat_data, dict) else []
    cat_rows = []

    for cat in cats_list:
        group = cat.get("group") or {}
        cat_rows.append(
            {
                "category_id": str(cat.get("id")),
                "category_name": cat.get("name"),
                "group_name": group.get("name") if isinstance(group, dict) else str(group),
                "is_income": cat.get("isIncome", False),
                "monthly_budget": float(cat.get("budgetAmount") or 0.0) if cat.get("budgetAmount") else None,
                "updated_at": now_ts,
            }
        )

    if not cat_rows:
        return 0

    cat_schema = [
        bigquery.SchemaField("category_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("category_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("group_name", "STRING"),
        bigquery.SchemaField("is_income", "BOOLEAN"),
        bigquery.SchemaField("monthly_budget", "NUMERIC"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    job_config = bigquery.LoadJobConfig(
        schema=cat_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    table_ref = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_categories"
    await asyncio.to_thread(lambda: bq.load_table_from_json(cat_rows, table_ref, job_config=job_config).result())
    return len(cat_rows)


async def sync_transactions(
    client: MonarchMoney,
    bq: bigquery.Client,
    days_back: int | None,
    now_ts: str,
) -> int:
    """Paginates transactions from Monarch and merges into raw_transactions via staging."""
    decommissioned_ids = get_decommissioned_account_ids()
    excluded_institutions = get_excluded_institutions()

    if days_back is not None and days_back > 0:
        start_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    else:
        start_date = "2020-01-01"
    end_date = date.today().strftime("%Y-%m-%d")

    txns_list = []
    offset = 0
    batch_limit = 1000

    while True:
        logger.info(
            f"Syncing transactions from Monarch: start_date={start_date}, end_date={end_date}, offset={offset}, limit={batch_limit}"
        )
        raw_txn_data = await client.get_transactions(
            start_date=start_date,
            end_date=end_date,
            offset=offset,
            limit=batch_limit,
        )
        batch = []
        if isinstance(raw_txn_data, dict):
            batch = raw_txn_data.get("allTransactions", {}).get("results", []) or raw_txn_data.get("transactions", [])

        if not batch:
            break

        txns_list.extend(batch)
        logger.info(f"Retrieved batch of {len(batch)} transactions (cumulative: {len(txns_list)})")

        if len(batch) < batch_limit:
            break
        offset += batch_limit

        if offset >= 50000:
            break

    txn_rows = []
    for txn in txns_list:
        acc = txn.get("account") or {}
        acc_id = str(acc.get("id") or txn.get("accountId") or "")
        if acc_id in decommissioned_ids:
            continue

        inst_obj = acc.get("institution") or {}
        inst_raw_name = inst_obj.get("name") if isinstance(inst_obj, dict) else str(inst_obj or "")
        inst_name = (inst_raw_name or "").lower()
        if any(exc in inst_name for exc in excluded_institutions):
            continue

        cat = txn.get("category") or {}
        merchant = txn.get("merchant") or {}
        txn_rows.append(
            {
                "transaction_id": str(txn.get("id")),
                "account_id": str(acc.get("id") or txn.get("accountId") or ""),
                "transaction_date": txn.get("date"),
                "amount": float(txn.get("amount") or 0.0),
                "merchant_name": merchant.get("name")
                if isinstance(merchant, dict)
                else (txn.get("plaidName") or txn.get("name")),
                "clean_merchant_name": merchant.get("name") if isinstance(merchant, dict) else None,
                "category_id": str(cat.get("id") or ""),
                "category_name": cat.get("name") if isinstance(cat, dict) else str(cat),
                "notes": txn.get("notes"),
                "is_recurring": txn.get("isRecurring", False),
                "pending": txn.get("pending", False),
                "updated_at": now_ts,
            }
        )

    if not txn_rows:
        return 0

    staging_ref = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.staging_transactions"
    target_ref = f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions"

    txn_schema = [
        bigquery.SchemaField("transaction_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("account_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("transaction_date", "DATE", mode="REQUIRED"),
        bigquery.SchemaField("amount", "NUMERIC", mode="REQUIRED"),
        bigquery.SchemaField("merchant_name", "STRING"),
        bigquery.SchemaField("clean_merchant_name", "STRING"),
        bigquery.SchemaField("category_id", "STRING"),
        bigquery.SchemaField("category_name", "STRING"),
        bigquery.SchemaField("notes", "STRING"),
        bigquery.SchemaField("is_recurring", "BOOLEAN"),
        bigquery.SchemaField("pending", "BOOLEAN"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    job_config = bigquery.LoadJobConfig(
        schema=txn_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    await asyncio.to_thread(lambda: bq.load_table_from_json(txn_rows, staging_ref, job_config=job_config).result())

    merge_query = f"""
    MERGE `{target_ref}` T
    USING `{staging_ref}` S
    ON T.transaction_id = S.transaction_id
    WHEN MATCHED THEN
      UPDATE SET
        T.account_id = S.account_id,
        T.transaction_date = S.transaction_date,
        T.amount = S.amount,
        T.merchant_name = S.merchant_name,
        T.clean_merchant_name = S.clean_merchant_name,
        T.category_id = S.category_id,
        T.category_name = S.category_name,
        T.notes = S.notes,
        T.is_recurring = S.is_recurring,
        T.pending = S.pending,
        T.updated_at = S.updated_at
    WHEN NOT MATCHED THEN
      INSERT (transaction_id, account_id, transaction_date, amount, merchant_name, clean_merchant_name, category_id, category_name, notes, is_recurring, pending, updated_at)
      VALUES (S.transaction_id, S.account_id, S.transaction_date, S.amount, S.merchant_name, S.clean_merchant_name, S.category_id, S.category_name, S.notes, S.is_recurring, S.pending, S.updated_at);
    """
    await asyncio.to_thread(lambda: bq.query(merge_query).result())
    return len(txn_rows)


async def execute_sync(days_back: int | None = 30, mfa_code: str | None = None) -> dict:
    """Orchestrates full sync of accounts, categories, and transactions into BigQuery."""
    client = await get_monarch_client(mfa_code=mfa_code)
    bq = get_bq_client(BQ_PROJECT_ID)
    now_ts = datetime.now(UTC).isoformat()

    synced_counts = {"accounts": 0, "categories": 0, "transactions": 0}
    try:
        synced_counts["accounts"] = await sync_all_accounts(client, bq, now_ts)
        synced_counts["categories"] = await sync_all_categories(client, bq, now_ts)
        synced_counts["transactions"] = await sync_transactions(client, bq, days_back, now_ts)

        return {
            "status": "success",
            "synced_counts": synced_counts,
            "timestamp": now_ts,
        }
    except Exception as e:
        logger.error(f"Monarch -> BigQuery sync failed: {e}")
        raise


# =====================================================================
# LIVE CONFIRMATION READ TOOLS (Safe for Gemini AFC / Agent / CLI)
# =====================================================================


async def get_live_account_balance_async(account_identifier: str) -> dict:
    """
    Fetches the live balance, available credit, and sync recency directly from Monarch Money.
    `account_identifier` can be an account ID or fuzzy display name (e.g. 'HELOC', 'Checking', 'Sapphire').
    """
    client = await get_monarch_client()
    raw = await client.get_accounts()
    accounts = raw.get("accounts", []) if isinstance(raw, dict) else []

    target = account_identifier.strip().lower()
    matches = []

    for acc in accounts:
        acc_id = str(acc.get("id", "")).lower()
        disp_name = str(acc.get("displayName", "")).lower()
        inst = acc.get("institution") or {}
        inst_name = (inst.get("name") if isinstance(inst, dict) else str(inst or "")).lower()

        if target == acc_id or target in disp_name or target in inst_name:
            matches.append(
                {
                    "account_id": acc.get("id"),
                    "display_name": acc.get("displayName"),
                    "institution_name": inst.get("name") if isinstance(inst, dict) else str(inst or ""),
                    "current_balance": float(acc.get("currentBalance") or 0.0),
                    "available_balance": float(acc.get("availableBalance") or 0.0)
                    if acc.get("availableBalance") is not None
                    else None,
                    "credit_limit": float(acc.get("creditLimit") or 0.0)
                    if acc.get("creditLimit") is not None
                    else None,
                    "is_asset": acc.get("isAsset", False),
                    "updated_at": acc.get("updatedAt"),
                }
            )

    if not matches:
        return {
            "found": False,
            "message": f"No active account matching '{account_identifier}' found in Monarch Money.",
        }

    return {
        "found": True,
        "count": len(matches),
        "accounts": matches,
    }


def get_live_account_balance(account_identifier: str) -> str:
    """
    Tool: Queries Monarch Money's live API to verify real-time account balances, credit limits,
    and last-synced recency when up-to-the-minute confirmation is needed beyond BigQuery.
    """
    try:
        res = _run_async(get_live_account_balance_async(account_identifier))
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error fetching live account balance for '{account_identifier}': {e}")
        return json.dumps({"error": f"Failed to fetch live balance: {str(e)}"})


async def get_live_transaction_async(transaction_id: str) -> dict:
    """Fetches details for a single transaction directly from Monarch Money."""
    client = await get_monarch_client()
    try:
        raw_txn = await client.get_transaction_details(transaction_id)
        if not raw_txn:
            return {"found": False, "message": f"Transaction {transaction_id} not found."}

        # Monarch GraphQL wraps the transaction drawer under 'getTransaction'
        txn = raw_txn.get("getTransaction") or raw_txn.get("transaction") or raw_txn
        if not isinstance(txn, dict):
            return {"found": False, "message": f"Transaction {transaction_id} not found."}

        cat = txn.get("category") or {}
        acc = txn.get("account") or {}
        merchant = txn.get("merchant") or {}

        # Handle nested merchant object or string / fallback to plaidName / name
        merchant_name = None
        if isinstance(merchant, dict) and merchant.get("name"):
            merchant_name = merchant["name"]
        elif isinstance(merchant, str) and merchant:
            merchant_name = merchant
        else:
            merchant_name = txn.get("plaidName") or txn.get("name")

        category_name = None
        if isinstance(cat, dict) and cat.get("name"):
            category_name = cat["name"]
        elif isinstance(cat, str) and cat:
            category_name = cat

        amount_val = float(txn.get("amount") or 0.0)
        txn_date = txn.get("date")

        # Fallback to BigQuery raw_transactions if live metadata is missing or incomplete
        if not merchant_name or amount_val == 0.0 or not txn_date or not category_name:
            try:
                bq_client = get_bq_client(BQ_PROJECT_ID)
                query = (
                    f"SELECT merchant_name, clean_merchant_name, amount, transaction_date, category_name "
                    f"FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions` "
                    f"WHERE transaction_id = @txn_id LIMIT 1"
                )
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("txn_id", "STRING", str(transaction_id))
                    ]
                )
                bq_rows = list(bq_client.query(query, job_config=job_config).result(max_results=1))
                if bq_rows:
                    row = dict(bq_rows[0].items())
                    if not merchant_name:
                        merchant_name = row.get("clean_merchant_name") or row.get("merchant_name")
                    if amount_val == 0.0 and row.get("amount") is not None:
                        amount_val = float(row.get("amount"))
                    if not txn_date and row.get("transaction_date"):
                        txn_date = str(row.get("transaction_date"))
                    if not category_name and row.get("category_name"):
                        category_name = str(row.get("category_name"))
            except Exception as bq_err:
                logger.warning(f"BigQuery fallback lookup failed for txn {transaction_id}: {bq_err}")

        return {
            "found": True,
            "transaction_id": txn.get("id") or transaction_id,
            "date": txn_date,
            "amount": amount_val,
            "merchant_name": merchant_name,
            "category_name": category_name or "Uncategorized",
            "account_name": acc.get("displayName") if isinstance(acc, dict) else str(acc),
            "pending": txn.get("pending", False),
            "is_recurring": txn.get("isRecurring", False),
            "notes": txn.get("notes"),
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


def get_live_transaction(transaction_id: str) -> str:
    """
    Tool: Queries Monarch Money's live API to inspect an individual transaction's pending status,
    notes, merchant info, or category.
    """
    try:
        res = _run_async(get_live_transaction_async(transaction_id))
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error fetching live transaction '{transaction_id}': {e}")
        return json.dumps({"error": f"Failed to fetch live transaction: {str(e)}"})


async def request_plaid_refresh_async(institution_name: str) -> dict:
    """
    Requests Monarch Money to initiate an upstream Plaid refresh for all accounts belonging to an institution.
    Protected by an in-memory 60-minute cooldown rate limiter per institution.
    """
    inst_key = institution_name.strip().lower()
    now = datetime.now(UTC)

    # Cooldown check
    last_req = _PLAID_REFRESH_COOLDOWNS.get(inst_key)
    if last_req:
        elapsed = (now - last_req).total_seconds() / 60.0
        if elapsed < PLAID_REFRESH_COOLDOWN_MINUTES:
            remaining = int(PLAID_REFRESH_COOLDOWN_MINUTES - elapsed)
            return {
                "status": "rate_limited",
                "message": (
                    f"Plaid refresh for '{institution_name}' was requested {int(elapsed)}m ago. "
                    f"Please wait {remaining} more minutes before requesting another refresh."
                ),
            }

    client = await get_monarch_client()
    raw = await client.get_accounts()
    accounts = raw.get("accounts", []) if isinstance(raw, dict) else []

    target_acc_ids = []
    matched_institutions = set()

    for acc in accounts:
        inst = acc.get("institution") or {}
        inst_label = (inst.get("name") if isinstance(inst, dict) else str(inst or "")).strip()
        if inst_key in inst_label.lower():
            target_acc_ids.append(str(acc.get("id")))
            matched_institutions.add(inst_label)

    if not target_acc_ids:
        return {
            "status": "not_found",
            "message": f"No active Monarch accounts matched institution '{institution_name}'.",
        }

    try:
        success = await client.request_accounts_refresh(target_acc_ids)
        _PLAID_REFRESH_COOLDOWNS[inst_key] = now
        return {
            "status": "triggered" if success else "failed",
            "accounts_count": len(target_acc_ids),
            "institutions": list(matched_institutions),
            "message": (
                f"Successfully requested Monarch to trigger Plaid refresh for {len(target_acc_ids)} "
                f"account(s) at {', '.join(matched_institutions)}. Data will reflect in Monarch shortly."
            ),
        }
    except Exception as e:
        logger.error(f"Failed to request Plaid refresh for '{institution_name}': {e}")
        return {
            "status": "error",
            "message": f"Monarch Plaid refresh request failed: {str(e)}",
        }


def request_plaid_refresh(institution_name: str) -> str:
    """
    Tool: Requests an on-demand upstream Plaid refresh for a financial institution (e.g. 'Chase', 'Credit Union')
    to pull latest settled transactions into Monarch Money. Cooldown limited to once per hour per institution.
    """
    try:
        res = _run_async(request_plaid_refresh_async(institution_name))
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error requesting Plaid refresh for '{institution_name}': {e}")
        return json.dumps({"error": f"Failed to request Plaid refresh: {str(e)}"})


# -------------------------------------------------------------------------
# PR 4: Carefully Guarded Mutations & Interactive Recategorization
# -------------------------------------------------------------------------


def get_mutation_hmac_secret() -> str:
    """Retrieves or derives the secret key used to sign mutation confirmation cards."""
    return (
        resolve_secret("mutation-hmac-secret", "MUTATION_HMAC_SECRET")
        or resolve_secret("chat-verification-token", "CHAT_VERIFICATION_TOKEN")
        or resolve_secret("gemini-wrapper-key", "GEMINI_WRAPPER_KEY")
        or "sage-guarded-mutation-signing-secret"
    )


def generate_mutation_signature(
    transaction_id: str,
    category_id: str,
    user_email: str,
    timestamp: int,
) -> str:
    """Generates a SHA-256 HMAC signature tying transaction, category, user, and timestamp."""
    key = get_mutation_hmac_secret().encode("utf-8")
    payload = f"{transaction_id}:{category_id}:{user_email.strip().lower()}:{timestamp}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_mutation_signature(
    transaction_id: str,
    category_id: str,
    user_email: str,
    timestamp: int,
    signature: str,
    max_age_seconds: int = HMAC_EXPIRATION_SECONDS,
) -> tuple[bool, str]:
    """Validates signature authenticity and timestamp freshness."""
    if not signature:
        return False, "Missing cryptographic signature."
    now = int(datetime.now(UTC).timestamp())
    age = abs(now - timestamp)
    if age > max_age_seconds:
        return (
            False,
            f"Confirmation expired (card age: {age}s > limit: {max_age_seconds}s). Please request a fresh confirmation.",
        )
    expected = generate_mutation_signature(transaction_id, category_id, user_email, timestamp)
    if not secrets.compare_digest(expected, signature):
        return False, "Cryptographic signature mismatch. Action parameters may have been altered."
    return True, "Valid"


def generate_snooze_signature(
    alert_key: str,
    days: int,
    timestamp: int,
) -> str:
    """Generates a SHA-256 HMAC signature tying alert_key, days, and timestamp."""
    key = get_mutation_hmac_secret().encode("utf-8")
    payload = f"snooze:{alert_key.strip().lower()}:{days}:{timestamp}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_snooze_signature(
    alert_key: str,
    days: int,
    timestamp: int,
    signature: str,
    max_age_seconds: int = 86400 * 7,  # Card action valid up to 7 days
) -> tuple[bool, str]:
    """Validates snooze signature authenticity and freshness."""
    if not signature:
        return False, "Missing cryptographic signature for snooze action."
    now = int(datetime.now(UTC).timestamp())
    age = abs(now - timestamp)
    if age > max_age_seconds:
        return (
            False,
            f"Snooze confirmation expired (card age: {age}s > limit: {max_age_seconds}s).",
        )
    expected = generate_snooze_signature(alert_key, days, timestamp)
    if not secrets.compare_digest(expected, signature):
        return False, "Cryptographic signature mismatch on snooze action."
    return True, "Valid"


def generate_batch_signature(
    batch_id: str,
    category_id: str,
    count: int,
    user_email: str,
    timestamp: int,
) -> str:
    """Generates a SHA-256 HMAC signature tying batch_id, category, count, user, and timestamp."""
    key = get_mutation_hmac_secret().encode("utf-8")
    payload = f"batch_recat:v1:{batch_id}:{category_id}:{count}:{user_email.strip().lower()}:{timestamp}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_batch_signature(
    batch_id: str,
    category_id: str,
    count: int,
    user_email: str,
    timestamp: int,
    signature: str,
    max_age_seconds: int = HMAC_EXPIRATION_SECONDS,
) -> tuple[bool, str]:
    """Validates batch signature authenticity and timestamp freshness."""
    if not signature:
        return False, "Missing cryptographic signature for batch recategorization."
    now = int(datetime.now(UTC).timestamp())
    age = abs(now - timestamp)
    if age > max_age_seconds:
        return (
            False,
            f"Batch confirmation expired (card age: {age}s > limit: {max_age_seconds}s). Please request a fresh confirmation.",
        )
    expected = generate_batch_signature(batch_id, category_id, count, user_email, timestamp)
    if not secrets.compare_digest(expected, signature):
        return False, "Cryptographic signature mismatch. Batch parameters may have been altered."
    return True, "Valid"


# =============================================================================
# PR 10: Conversational Guardrails, Idempotency & BigQuery Audit Log
# =============================================================================

_MUTATION_RATE_LIMITS: dict[str, list[float]] = {}
_MUTATION_RATE_LOCK = threading.Lock()
MUTATION_RATE_LIMIT_MAX = 10
MUTATION_RATE_LIMIT_WINDOW_SECONDS = 60

_MUTATION_IDEMPOTENCY_CACHE: dict[tuple[str, str, str, str], tuple[float, dict[str, Any]]] = {}
IDEMPOTENCY_WINDOW_SECONDS = 300  # 5 minutes


def check_mutation_rate_limit(user_email: str | None = None) -> tuple[bool, str]:
    """
    Checks if user has exceeded the mutation rate limit (max 10 actions per 60s).
    Returns (True, 'OK') or (False, rejection_reason).
    """
    clean_user = (user_email or CURRENT_USER_EMAIL.get() or "unknown").lower().strip()
    now = time.time()
    with _MUTATION_RATE_LOCK:
        timestamps = _MUTATION_RATE_LIMITS.setdefault(clean_user, [])
        cutoff = now - MUTATION_RATE_LIMIT_WINDOW_SECONDS
        valid_timestamps = [t for t in timestamps if t > cutoff]
        _MUTATION_RATE_LIMITS[clean_user] = valid_timestamps
        if len(valid_timestamps) >= MUTATION_RATE_LIMIT_MAX:
            return (
                False,
                f"Rate limit exceeded ({MUTATION_RATE_LIMIT_MAX} actions per {MUTATION_RATE_LIMIT_WINDOW_SECONDS}s). Please wait before trying again.",
            )
        _MUTATION_RATE_LIMITS[clean_user].append(now)
        return True, "OK"


def check_mutation_idempotency(
    user_email: str,
    action_type: str,
    target_id: str,
    new_value: str,
) -> dict[str, Any] | None:
    """
    Checks whether the identical mutation was executed within the idempotency window (300s).
    Returns cached result dict if found, else None.
    """
    clean_user = user_email.lower().strip()
    key = (clean_user, action_type.strip().upper(), str(target_id).strip(), str(new_value).strip())
    now = time.time()
    with _MUTATION_RATE_LOCK:
        if key in _MUTATION_IDEMPOTENCY_CACHE:
            ts, result = _MUTATION_IDEMPOTENCY_CACHE[key]
            if now - ts <= IDEMPOTENCY_WINDOW_SECONDS:
                return result
            del _MUTATION_IDEMPOTENCY_CACHE[key]
    return None


def record_mutation_idempotency(
    user_email: str,
    action_type: str,
    target_id: str,
    new_value: str,
    result: dict[str, Any],
):
    """Caches executed mutation for idempotency deduplication."""
    clean_user = user_email.lower().strip()
    key = (clean_user, action_type.strip().upper(), str(target_id).strip(), str(new_value).strip())
    now = time.time()
    with _MUTATION_RATE_LOCK:
        _MUTATION_IDEMPOTENCY_CACHE[key] = (now, result)


def reset_mutation_guardrails():
    """Resets in-memory rate limiting and idempotency caches (primarily for unit tests)."""
    with _MUTATION_RATE_LOCK:
        _MUTATION_RATE_LIMITS.clear()
        _MUTATION_IDEMPOTENCY_CACHE.clear()


def log_mutation_audit(
    action_type: str,
    target_id: str,
    user_email: str | None = None,
    status: str = "SUCCESS",
    previous_value: str | None = None,
    new_value: str | None = None,
    signature_valid: bool | None = None,
    details: str | None = None,
    mutation_id: str | None = None,
    bq: bigquery.Client | None = None,
    project_id: str | None = None,
    dataset_id: str | None = None,
) -> str:
    """
    Inserts an audit record into family_finance.mutation_audit_log.
    Guarantees non-blocking and fault-tolerant behavior (catches and logs errors without raising).
    Returns the mutation_id.
    """
    m_id = mutation_id or str(uuid.uuid4())
    target_user = (user_email or CURRENT_USER_EMAIL.get() or "unknown").strip().lower()
    target_project = project_id or BQ_PROJECT_ID
    target_dataset = dataset_id or BQ_DATASET_ID
    now_utc = datetime.now(UTC)

    insert_sql = f"""
    INSERT INTO `{target_project}.{target_dataset}.mutation_audit_log`
    (mutation_id, timestamp, user_email, action_type, target_id, previous_value, new_value, status, signature_valid, details, created_at)
    VALUES (
        @mutation_id,
        @timestamp,
        @user_email,
        @action_type,
        @target_id,
        @previous_value,
        @new_value,
        @status,
        @signature_valid,
        @details,
        CURRENT_TIMESTAMP()
    )
    """
    try:
        client = bq or get_bq_client(target_project)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("mutation_id", "STRING", m_id),
                bigquery.ScalarQueryParameter("timestamp", "TIMESTAMP", now_utc),
                bigquery.ScalarQueryParameter("user_email", "STRING", target_user),
                bigquery.ScalarQueryParameter("action_type", "STRING", action_type.strip().upper()),
                bigquery.ScalarQueryParameter("target_id", "STRING", str(target_id)),
                bigquery.ScalarQueryParameter("previous_value", "STRING", previous_value),
                bigquery.ScalarQueryParameter("new_value", "STRING", new_value),
                bigquery.ScalarQueryParameter("status", "STRING", status.strip().upper()),
                bigquery.ScalarQueryParameter("signature_valid", "BOOL", signature_valid),
                bigquery.ScalarQueryParameter("details", "STRING", details),
            ]
        )
        client.query(insert_sql, job_config=job_config).result()
        logger.info(f"Audit log recorded: id={m_id}, action={action_type}, target={target_id}, status={status}")
    except Exception as e:
        logger.warning(f"Could not write audit log entry to BigQuery (id={m_id}): {e}")

    return m_id


async def get_cached_categories(client: MonarchMoney | None = None, force_refresh: bool = False) -> dict[str, Any]:
    """Fetches and caches categories indexed by ID and normalized name."""
    global _CATEGORY_CACHE
    now = time.time()
    if not force_refresh and _CATEGORY_CACHE["by_id"] and (now - _CATEGORY_CACHE["last_fetched"] < 3600):
        return _CATEGORY_CACHE

    by_id = {}
    by_name = {}

    # Fast path: Try BigQuery raw_categories
    try:
        bq = get_bq_client(BQ_PROJECT_ID)
        query = f"SELECT category_id, category_name, group_name FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_categories`"
        rows = list(bq.query(query).result())
        for r in rows:
            cid = str(r.category_id)
            cname = str(r.category_name)
            grp = str(r.group_name) if r.group_name else ""
            entry = {"id": cid, "name": cname, "group": grp}
            by_id[cid] = entry
            by_name[cname.lower().strip()] = entry
            norm = re.sub(r"[^a-z0-9]", "", cname.lower())
            if norm:
                by_name[norm] = entry
    except Exception as e:
        logger.debug(f"Could not load categories from BigQuery: {e}")

    # Fallback to Monarch API
    if not by_id and client:
        try:
            raw_data = await client.get_transaction_categories()
            for c in raw_data.get("categories", []):
                cid = str(c.get("id"))
                cname = str(c.get("name"))
                grp = c.get("group") or {}
                grp_name = grp.get("name") if isinstance(grp, dict) else str(grp)
                entry = {"id": cid, "name": cname, "group": grp_name}
                by_id[cid] = entry
                by_name[cname.lower().strip()] = entry
                norm = re.sub(r"[^a-z0-9]", "", cname.lower())
                if norm:
                    by_name[norm] = entry
        except Exception as e:
            logger.warning(f"Could not load categories from Monarch API: {e}")

    if by_id:
        _CATEGORY_CACHE["by_id"] = by_id
        _CATEGORY_CACHE["by_name"] = by_name
        _CATEGORY_CACHE["last_fetched"] = now

    return _CATEGORY_CACHE


async def resolve_category(category_query: str, client: MonarchMoney | None = None) -> dict[str, Any] | None:
    """Resolves category name or ID using exact, normalized, and fuzzy matching."""
    cats = await get_cached_categories(client=client)
    by_id = cats.get("by_id", {})
    by_name = cats.get("by_name", {})

    q = str(category_query).strip()
    if not q:
        return None

    # 1. Exact ID
    if q in by_id:
        return by_id[q]

    # 2. Exact name (case-insensitive)
    q_lower = q.lower()
    if q_lower in by_name:
        return by_name[q_lower]

    # 3. Normalized alphanumeric
    norm_q = re.sub(r"[^a-z0-9]", "", q_lower)
    if norm_q and norm_q in by_name:
        return by_name[norm_q]

    # 4. Partial substring matching
    for name_key, entry in by_name.items():
        if norm_q and (norm_q in name_key or name_key in norm_q):
            return entry

    return None


def build_recategorization_card(
    transaction_id: str,
    merchant_name: str,
    amount: float,
    txn_date: str,
    current_category: str,
    new_category: str,
    category_id: str,
    user_email: str,
    timestamp: int,
    signature: str,
) -> dict:
    target_action = get_chat_action_target("confirm_recategorize")
    cancel_action = get_chat_action_target("cancel_recategorize")
    amount_clean = abs(float(amount or 0.0))
    date_display = f"{txn_date} " if txn_date else ""
    esc_merchant = html.escape(str(merchant_name or "Merchant"))
    esc_current = html.escape(str(current_category or "Uncategorized"))
    esc_proposed = html.escape(str(new_category or ""))
    return {
        "cardId": f"recat_{transaction_id}_{timestamp}",
        "card": {
            "header": {
                "title": "Sage Transaction Recategorization",
                "subtitle": "Interactive Confirmation Required",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "header": "Transaction Details",
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Merchant & Amount",
                                "text": f"<b>{esc_merchant}</b> • <b>${amount_clean:,.2f}</b>",
                                "startIcon": {"knownIcon": "STORE"},
                            }
                        },
                        {
                            "decoratedText": {
                                "topLabel": "Date & Transaction ID",
                                "text": f"{date_display}(ID: {transaction_id})",
                                "startIcon": {"knownIcon": "CLOCK"},
                            }
                        },
                        {
                            "decoratedText": {
                                "topLabel": "Category Reclassification",
                                "text": f"Current: <i>{esc_current}</i> → Proposed: <b>{esc_proposed}</b>",
                                "startIcon": {"knownIcon": "CONFIRMATION_NUMBER_ICON"},
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [
                                    {
                                        "text": "Confirm Update",
                                        "color": {"red": 0.12, "green": 0.53, "blue": 0.90},
                                        "onClick": {
                                            "action": {
                                                "function": target_action,
                                                "parameters": [
                                                    {"key": "action", "value": "confirm_recategorize"},
                                                    {"key": "transaction_id", "value": str(transaction_id)},
                                                    {"key": "category_id", "value": str(category_id)},
                                                    {"key": "category_name", "value": str(new_category)},
                                                    {"key": "merchant_name", "value": str(merchant_name)},
                                                    {"key": "amount", "value": f"{amount_clean:.2f}"},
                                                    {"key": "user_email", "value": str(user_email)},
                                                    {"key": "timestamp", "value": str(timestamp)},
                                                    {"key": "signature", "value": str(signature)},
                                                ],
                                            }
                                        },
                                    },
                                    {
                                        "text": "Cancel",
                                        "onClick": {
                                            "action": {
                                                "function": cancel_action,
                                                "parameters": [
                                                    {"key": "action", "value": "cancel_recategorize"},
                                                    {"key": "transaction_id", "value": str(transaction_id)},
                                                ],
                                            }
                                        },
                                    },
                                ]
                            }
                        },
                    ],
                }
            ],
        },
    }


def build_recategorization_success_card(
    transaction_id: str,
    category_name: str,
    merchant_name: str | None = None,
    amount: float | None = None,
) -> dict:
    """Builds a confirmation Card v2 acknowledging successful recategorization in Monarch."""
    esc_cat = html.escape(str(category_name))
    details = f"Transaction #{transaction_id}"
    if merchant_name:
        esc_merch = html.escape(str(merchant_name))
        amt_str = f" (${abs(amount):,.2f})" if amount is not None else ""
        details = f"<b>{esc_merch}</b>{amt_str} (ID: {transaction_id})"

    return {
        "cardId": f"recat_success_{transaction_id}",
        "card": {
            "header": {
                "title": "Category Successfully Updated",
                "subtitle": f"Reclassified to {esc_cat}",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Monarch Money Status",
                                "text": f"✅ {details} was successfully reclassified to <b>{esc_cat}</b>.",
                                "startIcon": {"knownIcon": "BOOKMARK"},
                            }
                        }
                    ]
                }
            ],
        },
    }


def build_recategorization_cancelled_card(transaction_id: str) -> dict:
    """Builds a confirmation Card v2 acknowledging user cancellation with buttons deactivated."""
    return {
        "cardId": f"recat_cancel_{transaction_id}",
        "card": {
            "header": {
                "title": "Recategorization Cancelled",
                "subtitle": f"Transaction #{transaction_id}",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Monarch Money Status",
                                "text": f"🚫 Proposal for transaction #{transaction_id} was cancelled. Action buttons deactivated.",
                                "startIcon": {"knownIcon": "DESCRIPTION"},
                            }
                        }
                    ]
                }
            ],
        },
    }


def build_batch_recategorization_card(
    batch_id: str,
    merchant_name: str,
    count: int,
    total_amount: float,
    current_category: str | None,
    new_category: str,
    category_id: str,
    user_email: str,
    timestamp: int,
    signature: str,
) -> dict:
    """Builds an interactive Card v2 proposing batch recategorization for a recurring merchant/pattern."""
    target_action = get_chat_action_target("confirm_batch_recategorize")
    cancel_action = get_chat_action_target("cancel_batch_recategorize")
    amt_clean = abs(float(total_amount or 0.0))
    esc_merchant = html.escape(str(merchant_name or "Merchant"))
    esc_current = html.escape(str(current_category or "Entertainment / Various"))
    esc_proposed = html.escape(str(new_category or ""))

    return {
        "cardId": f"batch_recat_{batch_id}_{timestamp}",
        "card": {
            "header": {
                "title": "Batch Recategorization Proposal",
                "subtitle": f"Reclassify {count} Transactions in Bulk",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "header": "Batch Scope & Details",
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Merchant & Scope",
                                "text": f"<b>{esc_merchant}</b> • <b>{count} transactions</b>",
                                "startIcon": {"knownIcon": "STORE"},
                            }
                        },
                        {
                            "decoratedText": {
                                "topLabel": "Total Spend",
                                "text": f"<b>${amt_clean:,.2f}</b>",
                                "startIcon": {"knownIcon": "DOLLAR"},
                            }
                        },
                        {
                            "decoratedText": {
                                "topLabel": "Classification Change",
                                "text": f"Current: <i>{esc_current}</i> → Proposed: <b>{esc_proposed}</b>",
                                "startIcon": {"knownIcon": "CONFIRMATION_NUMBER_ICON"},
                            }
                        },
                        {
                            "buttonList": {
                                "buttons": [
                                    {
                                        "text": f"Confirm Batch Update ({count} txns)",
                                        "color": {"red": 0.12, "green": 0.53, "blue": 0.90},
                                        "onClick": {
                                            "action": {
                                                "function": target_action,
                                                "parameters": [
                                                    {"key": "action", "value": "confirm_batch_recategorize"},
                                                    {"key": "batch_id", "value": str(batch_id)},
                                                    {"key": "category_id", "value": str(category_id)},
                                                    {"key": "category_name", "value": str(new_category)},
                                                    {"key": "merchant_name", "value": str(merchant_name)},
                                                    {"key": "count", "value": str(count)},
                                                    {"key": "total_amount", "value": f"{amt_clean:.2f}"},
                                                    {"key": "user_email", "value": str(user_email)},
                                                    {"key": "timestamp", "value": str(timestamp)},
                                                    {"key": "signature", "value": str(signature)},
                                                ],
                                            }
                                        },
                                    },
                                    {
                                        "text": "Cancel",
                                        "onClick": {
                                            "action": {
                                                "function": cancel_action,
                                                "parameters": [
                                                    {"key": "action", "value": "cancel_batch_recategorize"},
                                                    {"key": "batch_id", "value": str(batch_id)},
                                                    {"key": "merchant_name", "value": str(merchant_name)},
                                                ],
                                            }
                                        },
                                    },
                                ]
                            }
                        },
                    ],
                }
            ],
        },
    }


def build_batch_recategorization_success_card(
    batch_id: str,
    merchant_name: str,
    category_name: str,
    confirmed_count: int,
    failed_count: int = 0,
    total_amount: float | None = None,
    next_recommendation: dict | None = None,
) -> dict:
    """Builds a confirmation Card v2 showing successful batch recategorization across Monarch & BigQuery."""
    esc_cat = html.escape(str(category_name))
    esc_merch = html.escape(str(merchant_name))
    amt_str = f" (${abs(total_amount):,.2f})" if total_amount is not None else ""
    status_text = (
        f"✅ <b>{confirmed_count}</b> transactions for <b>{esc_merch}</b>{amt_str} were successfully reclassified to <b>{esc_cat}</b>."
    )
    if failed_count > 0:
        status_text += f" (⚠️ {failed_count} transactions could not be updated)."

    sections = [
        {
            "widgets": [
                {
                    "decoratedText": {
                        "topLabel": "Monarch Money & BigQuery Status",
                        "text": status_text,
                        "startIcon": {"knownIcon": "BOOKMARK"},
                    }
                }
            ]
        }
    ]

    if next_recommendation:
        rec_m = html.escape(str(next_recommendation.get("merchant", "")))
        rec_cnt = next_recommendation.get("count", 0)
        rec_amt = abs(float(next_recommendation.get("total_amount", 0.0)))
        rec_target = html.escape(str(next_recommendation.get("target_category", "")))
        rec_cur = html.escape(str(next_recommendation.get("current_category", "")))
        sections.append(
            {
                "header": "Next Recommended Batch",
                "widgets": [
                    {
                        "decoratedText": {
                            "topLabel": "High-Confidence Suggestion",
                            "text": f"<b>{rec_m}</b> ({rec_cnt} txns • ${rec_amt:,.2f})<br><i>{rec_cur}</i> → <b>{rec_target}</b>",
                            "bottomLabel": f"Reply 'Fix {rec_m}' to review and confirm",
                            "startIcon": {"knownIcon": "STAR"},
                        }
                    }
                ],
            }
        )

    return {
        "cardId": f"batch_recat_success_{batch_id}",
        "card": {
            "header": {
                "title": "Batch Update Completed",
                "subtitle": f"{confirmed_count} Transactions Reclassified to {esc_cat}",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }


def build_batch_recategorization_cancelled_card(batch_id: str, merchant_name: str) -> dict:
    """Builds a confirmation Card v2 acknowledging user cancellation of a batch proposal."""
    esc_merch = html.escape(str(merchant_name))
    return {
        "cardId": f"batch_recat_cancel_{batch_id}",
        "card": {
            "header": {
                "title": "Batch Recategorization Cancelled",
                "subtitle": f"Merchant: {esc_merch}",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Monarch Money Status",
                                "text": f"🚫 Batch proposal for <b>{esc_merch}</b> was cancelled. Action buttons deactivated.",
                                "startIcon": {"knownIcon": "DESCRIPTION"},
                            }
                        }
                    ]
                }
            ],
        },
    }


async def propose_transaction_recategorization_async(
    transaction_id: str,
    new_category: str,
) -> dict:
    """
    Validates single transaction limits, pending state, and category existence.
    Generates an HMAC signature and constructs the interactive Google Chat Card v2.
    """
    cleaned_id = str(transaction_id).strip()
    # 1. Single transaction guard
    if any(sep in cleaned_id for sep in [",", ";", " ", "\n", "\t"]):
        return {
            "status": "error",
            "message": "Strict Guardrail: Bulk mutations are blocked. Only 1 transaction may be recategorized at a time.",
        }

    # 2. Inspect live transaction
    txn = await get_live_transaction_async(cleaned_id)
    if not txn or not txn.get("found"):
        return {
            "status": "error",
            "message": f"Transaction #{cleaned_id} was not found in Monarch Money.",
        }

    # 3. Block pending transactions
    if txn.get("pending"):
        return {
            "status": "error",
            "message": f"Refused: Transaction #{cleaned_id} ({txn.get('merchant_name')}, ${txn.get('amount', 0.0):.2f}) is currently PENDING. Monarch Money rules prohibit recategorizing transactions before they post.",
        }

    # 4. Resolve proposed category
    client = await get_monarch_client()
    cat_match = await resolve_category(new_category, client=client)
    if not cat_match:
        cats = await get_cached_categories(client=client)
        sample_cats = sorted({c["name"] for c in cats.get("by_id", {}).values()})[:10]
        return {
            "status": "error",
            "message": f"Category '{new_category}' is not recognized in Monarch Money. Available categories include: {', '.join(sample_cats)}.",
        }

    new_cat_id = cat_match["id"]
    new_cat_name = cat_match["name"]
    current_cat_name = txn.get("category_name") or "Uncategorized"

    if current_cat_name.lower().strip() == new_cat_name.lower().strip():
        return {
            "status": "noop",
            "message": f"Transaction #{cleaned_id} is already categorized as '{new_cat_name}'. No changes needed.",
        }

    # 5. Sign proposal with HMAC
    user_email = CURRENT_USER_EMAIL.get()
    timestamp = int(datetime.now(UTC).timestamp())
    sig = generate_mutation_signature(cleaned_id, new_cat_id, user_email, timestamp)

    # 6. Build Card v2
    merchant_name = txn.get("merchant_name") or "Merchant"
    amount = abs(float(txn.get("amount") or 0.0))
    txn_date = str(txn.get("date") or "")

    card = build_recategorization_card(
        transaction_id=cleaned_id,
        merchant_name=merchant_name,
        amount=amount,
        txn_date=txn_date,
        current_category=current_cat_name,
        new_category=new_cat_name,
        category_id=new_cat_id,
        user_email=user_email,
        timestamp=timestamp,
        signature=sig,
    )

    CURRENT_PROPOSED_CARD.set(card)

    return {
        "status": "confirmation_required",
        "transaction_id": cleaned_id,
        "merchant": merchant_name,
        "amount": amount,
        "date": txn_date,
        "current_category": current_cat_name,
        "proposed_category": new_cat_name,
        "card": card,
        "message": f"Confirmation card generated. Awaiting user click to confirm recategorizing #{cleaned_id} to '{new_cat_name}'.",
    }


def propose_transaction_recategorization(transaction_id: str, new_category: str) -> str:
    """
    Tool: Proposes updating a single transaction's category in Monarch Money.
    Strictly generates a Google Chat confirmation card requiring interactive user approval.
    Never executes mutations directly.
    """
    try:
        res = _run_async(propose_transaction_recategorization_async(transaction_id, new_category))
        if isinstance(res, dict) and res.get("card"):
            CURRENT_PROPOSED_CARD.set(res["card"])
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error proposing recategorization for #{transaction_id}: {e}")
        return json.dumps({"status": "error", "message": str(e)})


async def execute_guarded_recategorization(
    transaction_id: str,
    category_id: str,
    category_name: str | None = None,
) -> dict:
    """
    Executes transaction recategorization in Monarch Money and updates BigQuery raw_transactions.
    Assumes human authorization has already been verified via HMAC signature.
    """
    client = await get_monarch_client()
    try:
        update_res = await client.update_transaction(
            transaction_id=str(transaction_id),
            category_id=str(category_id),
        )
        logger.info(
            f"Monarch Money transaction #{transaction_id} recategorized to category #{category_id}: {update_res}"
        )

        # Synchronize BigQuery raw_transactions in background
        try:
            bq = get_bq_client(BQ_PROJECT_ID)
            update_sql = f"""
                UPDATE `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`
                SET category_id = @cat_id,
                    category_name = @cat_name,
                    updated_at = CURRENT_TIMESTAMP()
                WHERE transaction_id = @txn_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cat_id", "STRING", str(category_id)),
                    bigquery.ScalarQueryParameter("cat_name", "STRING", str(category_name or "")),
                    bigquery.ScalarQueryParameter("txn_id", "STRING", str(transaction_id)),
                ]
            )
            await asyncio.to_thread(lambda: bq.query(update_sql, job_config=job_config).result())
            logger.info(f"BigQuery raw_transactions updated for txn #{transaction_id}")
        except Exception as bq_err:
            logger.warning(f"BigQuery update for txn #{transaction_id} encountered non-fatal error: {bq_err}")

        return {
            "success": True,
            "transaction_id": transaction_id,
            "category_id": category_id,
            "category_name": category_name,
        }
    except Exception as e:
        logger.error(f"Failed to recategorize transaction #{transaction_id} in Monarch: {e}")
        return {
            "success": False,
            "transaction_id": transaction_id,
            "error": str(e),
        }


# -------------------------------------------------------------------------
# Batch Recategorization Infrastructure (PR: 1-Click Bulk Fix)
# -------------------------------------------------------------------------

_PENDING_BATCHES_CACHE: dict[str, dict] = {}
_BATCH_CACHE_LOCK = asyncio.Lock()


async def save_pending_batch_async(batch: dict) -> None:
    """Saves a pending batch proposal into memory cache and BigQuery."""
    batch_id = batch["batch_id"]
    async with _BATCH_CACHE_LOCK:
        _PENDING_BATCHES_CACHE[batch_id] = batch

    def _save_to_bq():
        try:
            bq = get_bq_client(BQ_PROJECT_ID)
            ddl = f"""
            CREATE TABLE IF NOT EXISTS `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.pending_batches` (
                batch_id STRING NOT NULL,
                user_email STRING NOT NULL,
                merchant_name STRING NOT NULL,
                category_id STRING NOT NULL,
                category_name STRING NOT NULL,
                transaction_ids ARRAY<STRING> NOT NULL,
                transaction_count INT64 NOT NULL,
                total_amount NUMERIC NOT NULL,
                created_at TIMESTAMP NOT NULL,
                status STRING NOT NULL,
                signature STRING NOT NULL
            )
            """
            bq.query(ddl).result()

            insert_sql = f"""
            INSERT INTO `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.pending_batches`
            (batch_id, user_email, merchant_name, category_id, category_name, transaction_ids, transaction_count, total_amount, created_at, status, signature)
            VALUES (
                @batch_id, @user_email, @merchant_name, @category_id, @category_name, @txn_ids, @count, @amount, CURRENT_TIMESTAMP(), 'PENDING', @sig
            )
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id),
                    bigquery.ScalarQueryParameter("user_email", "STRING", batch.get("user_email", "unknown")),
                    bigquery.ScalarQueryParameter("merchant_name", "STRING", batch.get("merchant_name", "")),
                    bigquery.ScalarQueryParameter("category_id", "STRING", batch.get("category_id", "")),
                    bigquery.ScalarQueryParameter("category_name", "STRING", batch.get("category_name", "")),
                    bigquery.ArrayQueryParameter("txn_ids", "STRING", batch.get("transaction_ids", [])),
                    bigquery.ScalarQueryParameter("count", "INT64", int(batch.get("transaction_count", 0))),
                    bigquery.ScalarQueryParameter("amount", "NUMERIC", float(batch.get("total_amount", 0.0))),
                    bigquery.ScalarQueryParameter("sig", "STRING", batch.get("signature", "")),
                ]
            )
            bq.query(insert_sql, job_config=job_config).result()
        except Exception as e:
            logger.warning(f"Could not persist batch #{batch_id} to BigQuery pending_batches: {e}")

    await asyncio.to_thread(_save_to_bq)


async def get_pending_batch_async(batch_id: str) -> dict | None:
    """Retrieves a pending batch proposal by ID from memory cache or BigQuery."""
    async with _BATCH_CACHE_LOCK:
        if batch_id in _PENDING_BATCHES_CACHE:
            return _PENDING_BATCHES_CACHE[batch_id]

    def _read_from_bq():
        try:
            bq = get_bq_client(BQ_PROJECT_ID)
            sql = f"""
            SELECT batch_id, user_email, merchant_name, category_id, category_name, transaction_ids, transaction_count, total_amount, status, signature
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.pending_batches`
            WHERE batch_id = @batch_id
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id)]
            )
            results = list(bq.query(sql, job_config=job_config).result())
            if results:
                row = dict(results[0].items())
                return {
                    "batch_id": row["batch_id"],
                    "user_email": row["user_email"],
                    "merchant_name": row["merchant_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "transaction_ids": list(row["transaction_ids"]),
                    "transaction_count": int(row["transaction_count"]),
                    "total_amount": float(row["total_amount"]),
                    "status": row["status"],
                    "signature": row["signature"],
                }
        except Exception as e:
            logger.warning(f"Could not read batch #{batch_id} from BigQuery: {e}")
        return None

    res = await asyncio.to_thread(_read_from_bq)
    if res:
        async with _BATCH_CACHE_LOCK:
            _PENDING_BATCHES_CACHE[batch_id] = res
    return res


async def mark_batch_status_async(batch_id: str, status: str) -> None:
    """Updates the lifecycle status of a batch proposal."""
    async with _BATCH_CACHE_LOCK:
        if batch_id in _PENDING_BATCHES_CACHE:
            _PENDING_BATCHES_CACHE[batch_id]["status"] = status

    def _update_bq():
        try:
            bq = get_bq_client(BQ_PROJECT_ID)
            sql = f"""
            UPDATE `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.pending_batches`
            SET status = @status
            WHERE batch_id = @batch_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("status", "STRING", status),
                    bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id),
                ]
            )
            bq.query(sql, job_config=job_config).result()
        except Exception as e:
            logger.warning(f"Could not update batch #{batch_id} status in BigQuery: {e}")

    await asyncio.to_thread(_update_bq)


async def propose_batch_recategorization_async(
    merchant_name: str,
    new_category: str,
    current_category: str | None = None,
) -> dict:
    """
    Finds misclassified non-pending transactions matching merchant_name in BigQuery,
    constructs an HMAC-signed batch proposal, and prepares the Google Chat interactive Card v2.
    """
    clean_merchant = str(merchant_name).strip()
    if not clean_merchant:
        return {"status": "error", "message": "Merchant name cannot be empty."}

    client = await get_monarch_client()
    cat_match = await resolve_category(new_category, client=client)
    if not cat_match:
        cats = await get_cached_categories(client=client)
        sample_cats = sorted({c["name"] for c in cats.get("by_id", {}).values()})[:10]
        return {
            "status": "error",
            "message": f"Category '{new_category}' is not recognized in Monarch Money. Available categories include: {', '.join(sample_cats)}.",
        }

    new_cat_id = cat_match["id"]
    new_cat_name = cat_match["name"]

    def _query_txns():
        bq = get_bq_client(BQ_PROJECT_ID)
        pattern = f"%{clean_merchant.lower()}%"
        curr_filter = ""
        params = [
            bigquery.ScalarQueryParameter("pattern", "STRING", pattern),
            bigquery.ScalarQueryParameter("new_cat_id", "STRING", new_cat_id),
        ]
        if current_category:
            curr_filter = "AND LOWER(category_name) = LOWER(@curr_cat)"
            params.append(bigquery.ScalarQueryParameter("curr_cat", "STRING", current_category.strip()))

        sql = f"""
        SELECT transaction_id, amount, transaction_date, merchant_name, clean_merchant_name, category_name, category_id
        FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`
        WHERE (
            LOWER(COALESCE(clean_merchant_name, merchant_name, '')) LIKE @pattern
        )
        AND pending IS NOT TRUE
        AND (category_id != @new_cat_id OR category_id IS NULL)
        {curr_filter}
        ORDER BY transaction_date DESC
        LIMIT 500
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(row.items()) for row in bq.query(sql, job_config=job_config).result()]

    txns = await asyncio.to_thread(_query_txns)
    if not txns:
        return {
            "status": "noop",
            "message": f"No non-pending transactions found matching merchant '{clean_merchant}' needing reclassification to '{new_cat_name}'.",
        }

    txn_ids = [str(t["transaction_id"]) for t in txns]
    total_amount = sum(abs(float(t.get("amount") or 0.0)) for t in txns)
    batch_id = uuid.uuid4().hex[:12]
    user_email = CURRENT_USER_EMAIL.get()
    timestamp = int(datetime.now(UTC).timestamp())
    sig = generate_batch_signature(batch_id, new_cat_id, len(txn_ids), user_email, timestamp)

    batch_record = {
        "batch_id": batch_id,
        "user_email": user_email,
        "merchant_name": clean_merchant,
        "category_id": new_cat_id,
        "category_name": new_cat_name,
        "transaction_ids": txn_ids,
        "transaction_count": len(txn_ids),
        "total_amount": float(total_amount),
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PENDING",
        "signature": sig,
    }
    await save_pending_batch_async(batch_record)

    card = build_batch_recategorization_card(
        batch_id=batch_id,
        merchant_name=clean_merchant,
        count=len(txn_ids),
        total_amount=total_amount,
        current_category=current_category or txns[0].get("category_name"),
        new_category=new_cat_name,
        category_id=new_cat_id,
        user_email=user_email,
        timestamp=timestamp,
        signature=sig,
    )
    CURRENT_PROPOSED_CARD.set(card)

    return {
        "status": "confirmation_required",
        "batch_id": batch_id,
        "merchant": clean_merchant,
        "count": len(txn_ids),
        "total_amount": total_amount,
        "proposed_category": new_cat_name,
        "card": card,
        "message": f"Batch confirmation card generated for {len(txn_ids)} transactions (${total_amount:,.2f}) matching '{clean_merchant}'. Awaiting user confirmation in Google Chat.",
    }


def propose_batch_recategorization(
    merchant_name: str,
    new_category: str,
    current_category: str | None = None,
) -> str:
    """
    Tool: Proposes batch updating misclassified transactions for a recurring merchant/pattern in Monarch Money.
    Strictly generates an interactive Google Chat confirmation card requiring user approval.
    Never executes mutations directly.
    """
    try:
        res = _run_async(propose_batch_recategorization_async(merchant_name, new_category, current_category))
        if isinstance(res, dict) and res.get("card"):
            CURRENT_PROPOSED_CARD.set(res["card"])
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error proposing batch recategorization for '{merchant_name}': {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Failed to propose batch update: {str(e)}"})


async def execute_guarded_batch_recategorization(
    batch_id: str,
    user_email: str | None = None,
) -> dict:
    """
    Executes an approved batch recategorization across Monarch Money with bounded concurrency (semaphore=5)
    and executes an atomic bulk update in BigQuery for confirmed transactions.
    """
    batch = await get_pending_batch_async(batch_id)
    if not batch:
        return {"success": False, "error": f"Pending batch #{batch_id} not found or expired."}
    if batch.get("status") != "PENDING":
        return {
            "success": False,
            "error": f"Batch #{batch_id} is in status '{batch.get('status')}' and cannot be executed.",
        }

    await mark_batch_status_async(batch_id, "PROCESSING")
    cat_id = batch["category_id"]
    cat_name = batch["category_name"]
    txn_ids = batch["transaction_ids"]
    merchant_name = batch.get("merchant_name", "Merchant")

    client = await get_monarch_client()
    sem = asyncio.Semaphore(5)

    async def _update_single(txn_id: str):
        async with sem:
            for attempt in range(2):
                try:
                    await client.update_transaction(
                        transaction_id=str(txn_id),
                        category_id=str(cat_id),
                    )
                    return txn_id, True, None
                except Exception as e:
                    if attempt == 1:
                        return txn_id, False, str(e)
                    await asyncio.sleep(0.5)

    results = await asyncio.gather(*(_update_single(tid) for tid in txn_ids))
    confirmed_ids = [tid for tid, ok, _ in results if ok]
    failed_items = [{"id": tid, "error": err} for tid, ok, err in results if not ok]

    # Bulk update BigQuery for confirmed transactions
    if confirmed_ids:
        def _bq_bulk_update():
            try:
                bq = get_bq_client(BQ_PROJECT_ID)
                update_sql = f"""
                UPDATE `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`
                SET category_id = @cat_id,
                    category_name = @cat_name,
                    updated_at = CURRENT_TIMESTAMP()
                WHERE transaction_id IN UNNEST(@confirmed_ids)
                """
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("cat_id", "STRING", str(cat_id)),
                        bigquery.ScalarQueryParameter("cat_name", "STRING", str(cat_name)),
                        bigquery.ArrayQueryParameter("confirmed_ids", "STRING", confirmed_ids),
                    ]
                )
                bq.query(update_sql, job_config=job_config).result()
            except Exception as bq_err:
                logger.warning(f"BigQuery bulk update for batch #{batch_id} encountered non-fatal error: {bq_err}")

        await asyncio.to_thread(_bq_bulk_update)

    final_status = "CONFIRMED" if not failed_items else ("PARTIAL_SUCCESS" if confirmed_ids else "FAILED")
    await mark_batch_status_async(batch_id, final_status)

    log_mutation_audit(
        action_type="BATCH_RECATEGORIZE",
        target_id=batch_id,
        user_email=user_email or batch.get("user_email") or "unknown",
        status="SUCCESS" if not failed_items else ("PARTIAL_SUCCESS" if confirmed_ids else "FAILED"),
        previous_value=f"{len(txn_ids)} txns for {merchant_name}",
        new_value=cat_name,
        signature_valid=True,
        details=json.dumps({
            "attempted": len(txn_ids),
            "succeeded": len(confirmed_ids),
            "failed_count": len(failed_items),
            "failed_samples": failed_items[:5],
        }),
    )

    return {
        "success": len(confirmed_ids) > 0,
        "batch_id": batch_id,
        "merchant_name": merchant_name,
        "category_name": cat_name,
        "attempted_count": len(txn_ids),
        "confirmed_count": len(confirmed_ids),
        "failed_count": len(failed_items),
        "total_amount": batch.get("total_amount"),
    }


async def find_next_recategorization_recommendation_async(
    exclude_merchant: str | None = None,
) -> dict | None:
    """
    Scans BigQuery for the highest-confidence candidate merchant with misclassified
    or fragmented transactions, prioritizing well-known digital subscriptions and
    merchants with high historical category consensus.
    """
    def _query_recommendations():
        try:
            bq = get_bq_client(BQ_PROJECT_ID)
            ex_pattern = f"%{str(exclude_merchant or '').strip().lower()}%" if exclude_merchant else ""

            # 1. Known Streaming & Subscription services frequently misclassified under Entertainment/General
            known_streaming_sql = fr"""
            SELECT
                COALESCE(clean_merchant_name, merchant_name) AS merchant,
                category_name AS current_category,
                'Subscriptions' AS target_category,
                COUNT(*) AS count,
                ROUND(SUM(ABS(amount)), 2) AS total_amount
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`
            WHERE pending IS NOT TRUE
              AND LOWER(category_name) NOT IN ('subscriptions', 'subscription')
              AND (
                  REGEXP_CONTAINS(LOWER(COALESCE(clean_merchant_name, merchant_name, '')), r'(prime video|amazon prime video|hulu|spotify|disney\+|disney plus|apple\.com/bill|youtube premium|youtube tv|peacock|paramount\+|audible|max\.com|hbomax)')
              )
              AND (@ex_pattern = '' OR LOWER(COALESCE(clean_merchant_name, merchant_name, '')) NOT LIKE @ex_pattern)
            GROUP BY 1, 2
            HAVING count >= 2
            ORDER BY count DESC, total_amount DESC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("ex_pattern", "STRING", ex_pattern)]
            )
            rows = list(bq.query(known_streaming_sql, job_config=job_config).result())
            if rows:
                r = dict(rows[0].items())
                return {
                    "merchant": r["merchant"],
                    "current_category": r["current_category"],
                    "target_category": r["target_category"],
                    "count": int(r["count"]),
                    "total_amount": float(r["total_amount"]),
                    "reason": f"Known recurring digital subscription filed under {r['current_category']} instead of Subscriptions.",
                }

            # 2. General fragmentation scan: merchants with >= 3 txns where >= 60% are in a dominant category
            # but >= 2 txns are stranded in a minority category
            general_frag_sql = f"""
            WITH merchant_splits AS (
                SELECT
                    COALESCE(clean_merchant_name, merchant_name) AS merchant,
                    category_name,
                    COUNT(*) AS cat_tx_count,
                    ROUND(SUM(ABS(amount)), 2) AS cat_total_amount,
                    SUM(COUNT(*)) OVER (PARTITION BY COALESCE(clean_merchant_name, merchant_name)) AS total_tx_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(clean_merchant_name, merchant_name)
                        ORDER BY COUNT(*) DESC
                    ) AS rank_order
                FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`
                WHERE pending IS NOT TRUE
                  AND COALESCE(clean_merchant_name, merchant_name) IS NOT NULL
                  AND category_name IS NOT NULL
                GROUP BY 1, 2
            ),
            dominant AS (
                SELECT merchant, category_name AS dominant_category, cat_tx_count AS dominant_count, total_tx_count
                FROM merchant_splits
                WHERE rank_order = 1
                  AND cat_tx_count >= 3
            ),
            fragmented AS (
                SELECT 
                    s.merchant,
                    d.dominant_category AS target_category,
                    s.category_name AS current_category,
                    s.cat_tx_count AS count,
                    s.cat_total_amount AS total_amount
                FROM merchant_splits s
                JOIN dominant d ON s.merchant = d.merchant
                WHERE s.rank_order > 1
                  AND s.cat_tx_count >= 2
                  AND s.category_name != d.dominant_category
                  AND (d.dominant_count * 1.0 / d.total_tx_count) >= 0.60
            )
            SELECT *
            FROM fragmented
            WHERE (@ex_pattern = '' OR LOWER(merchant) NOT LIKE @ex_pattern)
            ORDER BY count DESC, total_amount DESC
            LIMIT 1
            """
            rows2 = list(bq.query(general_frag_sql, job_config=job_config).result())
            if rows2:
                r2 = dict(rows2[0].items())
                return {
                    "merchant": r2["merchant"],
                    "current_category": r2["current_category"],
                    "target_category": r2["target_category"],
                    "count": int(r2["count"]),
                    "total_amount": float(r2["total_amount"]),
                    "reason": f"Historical consensus: {r2['merchant']} transactions are predominantly categorized as {r2['target_category']}.",
                }
        except Exception as e:
            logger.warning(f"Error querying next recategorization recommendation: {e}")
        return None

    return await asyncio.to_thread(_query_recommendations)


def get_recategorization_recommendations(exclude_merchant: str | None = None) -> str:
    """
    Tool: Discovers high-confidence misclassified or fragmented transactions across recurring merchants.
    Returns recommended batch recategorization candidates with transaction counts and spend amounts.
    """
    try:
        res = _run_async(find_next_recategorization_recommendation_async(exclude_merchant))
        if res:
            return json.dumps({"status": "found", "recommendation": res})
        return json.dumps({"status": "none_found", "message": "No high-confidence fragmented merchants found."})
    except Exception as e:
        logger.error(f"Failed to fetch recategorization recommendations: {e}")
        return json.dumps({"status": "error", "message": str(e)})


def extract_card_action_parameters(payload: dict) -> tuple[str | None, dict[str, str]]:
    """
    Extracts action name and string key-value parameters from Google Chat or
    Google Workspace Add-on CARD_CLICKED interaction payloads.
    """
    # 1. Google Workspace Add-on commonEventObject or direct common
    common_obj = payload.get("commonEventObject") or payload.get("common") or {}
    if common_obj:
        action_name = common_obj.get("invokedFunction") or common_obj.get("action")
        params = common_obj.get("parameters") or {}
        params_dict = {}
        if isinstance(params, dict):
            params_dict = {str(k): str(v) for k, v in params.items()}
        elif isinstance(params, list):
            for item in params:
                if isinstance(item, dict) and "key" in item:
                    params_dict[str(item["key"])] = str(item.get("value", ""))

        # If action_name is a pubsub topic path or missing, fallback to explicit "action" parameter
        if (not action_name or "/topics/" in str(action_name) or str(action_name).startswith("projects/")) and "action" in params_dict:
            action_name = params_dict["action"]

        return action_name, params_dict

    # 2. Google Chat direct action object
    action_obj = payload.get("action") or {}
    if action_obj:
        action_name = action_obj.get("actionMethodName") or action_obj.get("function")
        raw_params = action_obj.get("parameters") or []
        params_dict = {}
        if isinstance(raw_params, list):
            for item in raw_params:
                if isinstance(item, dict) and "key" in item:
                    params_dict[str(item["key"])] = str(item.get("value", ""))
        elif isinstance(raw_params, dict):
            params_dict = {str(k): str(v) for k, v in raw_params.items()}

        if (not action_name or "/topics/" in str(action_name) or str(action_name).startswith("projects/")) and "action" in params_dict:
            action_name = params_dict["action"]

        return action_name, params_dict

    return None, {}
