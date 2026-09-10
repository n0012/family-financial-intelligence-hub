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
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from google.cloud import bigquery
from monarchmoney import MonarchMoney
import pyotp

from config import (
    BQ_DATASET_ID,
    BQ_PROJECT_ID,
    get_account_overrides,
    get_decommissioned_account_ids,
    get_excluded_institutions,
    get_rates_config,
    resolve_secret,
)

logger = logging.getLogger("monarch-gemini.monarch_service")

_monarch_client: Optional[MonarchMoney] = None
_lock = asyncio.Lock()

# In-memory cooldown tracking for upstream Plaid refreshes (institution_name_lower -> last_requested_utc)
_PLAID_REFRESH_COOLDOWNS: Dict[str, datetime] = {}
PLAID_REFRESH_COOLDOWN_MINUTES = 60


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


async def get_monarch_client(mfa_code: Optional[str] = None) -> MonarchMoney:
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
    default_heloc_apr = rates_cfg.get("default_heloc_apr") or rates_cfg.get("heloc_apr")
    default_mortgage_apr = rates_cfg.get("default_mortgage_apr") or rates_cfg.get("mortgage_apr")
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
        elif "mortgage" in subtype_str or "mortgage" in type_str:
            int_rate = float(default_mortgage_apr) if default_mortgage_apr is not None else None
        elif type_str in ("loan", "credit") and not acc.get("isAsset", False):
            int_rate = float(default_debt_apr) if default_debt_apr is not None else None
        else:
            int_rate = None

        account_rows.append({
            "account_id": str(acc.get("id")),
            "account_name": acc.get("displayName") or acc.get("id"),
            "display_name": acc.get("displayName"),
            "type_name": acc_type.get("name") if isinstance(acc_type, dict) else str(acc_type),
            "subtype_name": acc_subtype.get("name") if isinstance(acc_subtype, dict) else str(acc_subtype),
            "current_balance": float(acc.get("currentBalance") or 0.0),
            "available_balance": float(acc.get("availableBalance") or 0.0) if acc.get("availableBalance") is not None else None,
            "credit_limit": float(acc.get("creditLimit") or 0.0) if acc.get("creditLimit") is not None else None,
            "interest_rate": int_rate,
            "institution_name": inst_name,
            "is_asset": acc.get("isAsset", False),
            "updated_at": now_ts,
        })

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
        cat_rows.append({
            "category_id": str(cat.get("id")),
            "category_name": cat.get("name"),
            "group_name": group.get("name") if isinstance(group, dict) else str(group),
            "is_income": cat.get("isIncome", False),
            "monthly_budget": float(cat.get("budgetAmount") or 0.0) if cat.get("budgetAmount") else None,
            "updated_at": now_ts,
        })

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
    days_back: Optional[int],
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
        logger.info(f"Syncing transactions from Monarch: start_date={start_date}, end_date={end_date}, offset={offset}, limit={batch_limit}")
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
        txn_rows.append({
            "transaction_id": str(txn.get("id")),
            "account_id": str(acc.get("id") or txn.get("accountId") or ""),
            "transaction_date": txn.get("date"),
            "amount": float(txn.get("amount") or 0.0),
            "merchant_name": merchant.get("name") if isinstance(merchant, dict) else (txn.get("plaidName") or txn.get("name")),
            "clean_merchant_name": merchant.get("name") if isinstance(merchant, dict) else None,
            "category_id": str(cat.get("id") or ""),
            "category_name": cat.get("name") if isinstance(cat, dict) else str(cat),
            "notes": txn.get("notes"),
            "is_recurring": txn.get("isRecurring", False),
            "pending": txn.get("pending", False),
            "updated_at": now_ts,
        })

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


async def execute_sync(days_back: Optional[int] = 30, mfa_code: Optional[str] = None) -> dict:
    """Orchestrates full sync of accounts, categories, and transactions into BigQuery."""
    client = await get_monarch_client(mfa_code=mfa_code)
    bq = bigquery.Client(project=BQ_PROJECT_ID)
    now_ts = datetime.now(timezone.utc).isoformat()

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
            matches.append({
                "account_id": acc.get("id"),
                "display_name": acc.get("displayName"),
                "institution_name": inst.get("name") if isinstance(inst, dict) else str(inst or ""),
                "current_balance": float(acc.get("currentBalance") or 0.0),
                "available_balance": float(acc.get("availableBalance") or 0.0) if acc.get("availableBalance") is not None else None,
                "credit_limit": float(acc.get("creditLimit") or 0.0) if acc.get("creditLimit") is not None else None,
                "is_asset": acc.get("isAsset", False),
                "updated_at": acc.get("updatedAt"),
            })

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
        txn = await client.get_transaction_details(transaction_id)
        if not txn:
            return {"found": False, "message": f"Transaction {transaction_id} not found."}

        cat = txn.get("category") or {}
        acc = txn.get("account") or {}
        merchant = txn.get("merchant") or {}

        return {
            "found": True,
            "transaction_id": txn.get("id"),
            "date": txn.get("date"),
            "amount": float(txn.get("amount") or 0.0),
            "merchant_name": merchant.get("name") if isinstance(merchant, dict) else (txn.get("plaidName") or txn.get("name")),
            "category_name": cat.get("name") if isinstance(cat, dict) else str(cat),
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
    now = datetime.now(timezone.utc)

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
    Tool: Requests an on-demand upstream Plaid refresh for a financial institution (e.g. 'Chase', 'First Tech')
    to pull latest settled transactions into Monarch Money. Cooldown limited to once per hour per institution.
    """
    try:
        res = _run_async(request_plaid_refresh_async(institution_name))
        return json.dumps(res, default=str)
    except Exception as e:
        logger.error(f"Error requesting Plaid refresh for '{institution_name}': {e}")
        return json.dumps({"error": f"Failed to request Plaid refresh: {str(e)}"})
