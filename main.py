import asyncio
from datetime import date, datetime, timedelta
import json
import logging
import os
import re
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, Security
from fastapi.security import APIKeyHeader
import google.auth
from google.auth.transport import requests as google_requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import bigquery, secretmanager
from google.oauth2 import id_token
from monarchmoney import MonarchMoney
import pyotp
import requests
import yaml

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monarch-gemini")

app = FastAPI(
    title="Monarch Money Gemini & BigQuery API",
    version="2.0.0",
    description="Secure wrapper exposing Monarch Money data to Gemini models and syncing to BigQuery.",
)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

BQ_PROJECT_ID = os.getenv("PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "your-gcp-project-id"))
BQ_DATASET_ID = os.getenv("BQ_DATASET_ID", "family_finance")


def resolve_secret(secret_name: str, env_var: str) -> Optional[str]:
    """Resolves secret from Secret Manager latest version, falling back to environment variable."""
    # Try fetching latest version from Secret Manager first
    try:
        sm_client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{BQ_PROJECT_ID}/secrets/{secret_name}/versions/latest"
        resp = sm_client.access_secret_version(name=name)
        val = resp.payload.data.decode("utf-8").strip()
        if val and val != "NONE" and val != "placeholder":
            return val
    except Exception as err:
        logger.debug(f"Direct Secret Manager fetch for {secret_name} failed: {err}")

    # Fallback to env var
    val = os.getenv(env_var)
    if val and val != "NONE" and val != "placeholder":
        return val.strip()
    return None


_monarch_client: Optional[MonarchMoney] = None
_lock = asyncio.Lock()


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
            raise HTTPException(
                status_code=500,
                detail="MONARCH_EMAIL or MONARCH_PASSWORD not configured.",
            )

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
                clean_secret = mfa_secret.replace(" ", "").replace("-", "").strip() if (mfa_secret and mfa_secret != "NONE") else None
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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Monarch authentication failed: {str(e)}",
            )


def verify_api_key(api_key: Optional[str] = Security(API_KEY_HEADER)):
    """Validates the incoming X-API-Key against the secret stored in Secret Manager / environment."""
    expected = resolve_secret("gemini-wrapper-key", "GEMINI_WRAPPER_KEY")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="Server GEMINI_WRAPPER_KEY is not configured.",
        )
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Missing or invalid X-API-Key header.",
        )
    return api_key


@app.get("/health", tags=["System"])
async def health(api_key: str = Security(verify_api_key)):
    """Health check endpoint to verify wrapper availability and key validity."""
    return {"status": "ok", "service": "monarch-gemini-bigquery-wrapper"}


@app.get("/avatar.png", tags=["System"])
@app.get("/static/avatar.png", tags=["System"])
async def avatar():
    """Serves the Family Finance Copilot avatar icon."""
    avatar_path = os.path.join(os.path.dirname(__file__), "static", "avatar.png")
    if os.path.exists(avatar_path):
        with open(avatar_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    return Response(status_code=404)


@app.post("/auth/mfa", tags=["Auth"])
async def authenticate_with_mfa(
    code: str = Query(..., description="6-digit code from Google Authenticator"),
    api_key: str = Security(verify_api_key),
):
    """Manually authenticate using a 6-digit code from Google Authenticator."""
    await get_monarch_client(mfa_code=code)
    return {"status": "authenticated", "message": "Successfully authenticated with Monarch Money!"}


@app.get("/accounts", tags=["Monarch Data"])
async def get_accounts(api_key: str = Security(verify_api_key)):
    """Retrieve all connected financial accounts and current balances."""
    client = await get_monarch_client()
    try:
        return await client.get_accounts()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_accounts failed: {str(e)}")


@app.get("/transactions", tags=["Monarch Data"])
async def get_transactions(
    start_date: Optional[str] = Query(None, description="Start date in YYYY-MM-DD format (inclusive)"),
    end_date: Optional[str] = Query(None, description="End date in YYYY-MM-DD format (inclusive)"),
    limit: int = Query(50, le=200, description="Maximum number of transactions to return"),
    api_key: str = Security(verify_api_key),
):
    """Fetch transactions filtered by date range and limit."""
    client = await get_monarch_client()
    try:
        return await client.get_transactions(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_transactions failed: {str(e)}")


@app.get("/categories", tags=["Monarch Data"])
async def get_categories(api_key: str = Security(verify_api_key)):
    """Fetch list of spending and income categories configured in Monarch Money."""
    client = await get_monarch_client()
    try:
        return await client.get_transaction_categories()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_categories failed: {str(e)}")


@app.get("/cashflow", tags=["Monarch Data"])
async def get_cashflow(
    start_date: Optional[str] = Query(None, description="Start date in YYYY-MM-DD format"),
    end_date: Optional[str] = Query(None, description="End date in YYYY-MM-DD format"),
    api_key: str = Security(verify_api_key),
):
    """Fetch cashflow breakdown for a specific time window."""
    client = await get_monarch_client()
    try:
        return await client.get_cashflow(start_date=start_date, end_date=end_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_cashflow failed: {str(e)}")


# Default configurations (fully configurable via Secret Manager, config.json, or environment variables)
DEFAULT_DECOMMISSIONED_ACCOUNT_IDS: set[str] = set()
DEFAULT_ACCOUNT_OVERRIDES: dict[str, dict] = {}
DEFAULT_EXCLUDED_INSTITUTIONS: set[str] = set()


def load_local_config() -> dict:
    """Loads optional local configuration from config.yaml, config.yml, or config.json."""
    config_file = os.getenv("CONFIG_FILE")
    candidate_files = [config_file] if config_file else ["config.yaml", "config.yml", "config.json"]

    for file_path in candidate_files:
        if file_path and os.path.exists(file_path):
            try:
                if file_path.endswith((".yaml", ".yml")):
                    with open(file_path, "r") as f:
                        data = yaml.safe_load(f)
                        if isinstance(data, dict):
                            return data
                else:
                    with open(file_path, "r") as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            return data
            except Exception as e:
                logger.warning(f"Failed to load config from {file_path}: {e}")
    return {}


def get_rates_config() -> dict:
    """Retrieves default interest rates from local config (YAML/JSON), Secret Manager, or ENV."""
    cfg = load_local_config().get("rates")
    if cfg and isinstance(cfg, dict):
        return dict(cfg)

    raw = resolve_secret("rates-config", "RATES_CONFIG_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to parse RATES_CONFIG_JSON: {e}")
    return {}


def get_decommissioned_account_ids() -> set[str]:
    """Retrieves set of account IDs to ignore from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("decommissioned_account_ids")
    if cfg:
        return set(cfg)

    raw = resolve_secret("decommissioned-account-ids", "DECOMMISSIONED_ACCOUNT_IDS")
    if raw:
        try:
            if raw.startswith("["):
                return set(json.loads(raw))
            return {x.strip() for x in raw.split(",") if x.strip()}
        except Exception as e:
            logger.warning(f"Failed to parse DECOMMISSIONED_ACCOUNT_IDS: {e}")
    return set(DEFAULT_DECOMMISSIONED_ACCOUNT_IDS)


def get_account_overrides() -> dict:
    """Retrieves account attribute overrides (e.g. custom APRs) from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("account_overrides")
    if cfg:
        return dict(cfg)

    raw = resolve_secret("account-overrides", "ACCOUNT_OVERRIDES_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to parse ACCOUNT_OVERRIDES_JSON: {e}")
    return dict(DEFAULT_ACCOUNT_OVERRIDES)


def get_excluded_institutions() -> set[str]:
    """Retrieves list of institution name keywords to exclude from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("excluded_institutions")
    if cfg:
        return {str(x).strip().lower() for x in cfg if str(x).strip()}

    raw = resolve_secret("excluded-institutions", "EXCLUDED_INSTITUTIONS")
    if raw:
        return {x.strip().lower() for x in raw.split(",") if x.strip()}
    return set(DEFAULT_EXCLUDED_INSTITUTIONS)


async def execute_sync(days_back: Optional[int] = 90, mfa_code: Optional[str] = None) -> dict:
    """Internal sync logic from Monarch Money to BigQuery with pagination."""
    client = await get_monarch_client(mfa_code=mfa_code)
    bq = bigquery.Client(project=BQ_PROJECT_ID)
    now_ts = datetime.utcnow().isoformat()

    decommissioned_ids = get_decommissioned_account_ids()
    account_overrides = get_account_overrides()
    excluded_institutions = get_excluded_institutions()
    rates_cfg = get_rates_config()
    default_heloc_apr = rates_cfg.get("default_heloc_apr") or rates_cfg.get("heloc_apr")
    default_mortgage_apr = rates_cfg.get("default_mortgage_apr") or rates_cfg.get("mortgage_apr")
    default_debt_apr = rates_cfg.get("default_debt_apr")

    synced_counts = {"accounts": 0, "categories": 0, "transactions": 0}

    try:
        # 1. Sync Accounts
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
            # Skip excluded institutions (e.g. acquired / duplicate banks)
            if any(exc in inst_name.lower() for exc in excluded_institutions):
                continue

            acc_type = acc.get("type") or {}
            acc_subtype = acc.get("subtype") or {}
            
            # Resolve account interest rate (APR) hierarchically:
            # 1. Explicit account-level override in config (account_overrides[acc_id].interest_rate)
            # 2. Aggregator reported rate from Monarch API (acc.interestRate)
            # 3. Class-specific default rate from config file (e.g. default_heloc_apr, default_mortgage_apr)
            # 4. Fallback general debt rate (default_debt_apr)
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

        if account_rows:
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
            bq.load_table_from_json(account_rows, table_ref, job_config=job_config).result()
            synced_counts["accounts"] = len(account_rows)

        # 2. Sync Categories
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

        if cat_rows:
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
            bq.load_table_from_json(cat_rows, table_ref, job_config=job_config).result()
            synced_counts["categories"] = len(cat_rows)

        # 3. Sync Transactions (with pagination across full history or custom date window)
        if days_back is not None and days_back > 0:
            start_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        else:
            start_date = "2020-01-01"  # Earliest date to fetch all historical transactions
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

            if offset >= 50000:  # Safety guardrail
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

        if txn_rows:
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
            bq.load_table_from_json(txn_rows, staging_ref, job_config=job_config).result()
            
            # Merge staging into main table
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
            bq.query(merge_query).result()
            synced_counts["transactions"] = len(txn_rows)

        return {
            "status": "success",
            "synced_counts": synced_counts,
            "timestamp": now_ts,
        }
    except Exception as e:
        logger.error(f"BigQuery sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"BigQuery sync failed: {str(e)}")


@app.post("/sync/bigquery", tags=["BigQuery Sync"])
async def sync_to_bigquery(
    days_back: Optional[int] = Query(90, description="How many days back to sync transactions (pass 0 or None for all history)"),
    mfa_code: Optional[str] = Query(None, description="Optional 6-digit code from Google Authenticator"),
    api_key: str = Security(verify_api_key),
):
    """
    Synchronizes Monarch Money accounts, categories, and recent transactions into BigQuery.
    Used for daily automated synchronization via Cloud Scheduler or manual refresh.
    """
    effective_days = days_back if (days_back is not None and days_back > 0) else None
    return await execute_sync(days_back=effective_days, mfa_code=mfa_code)


async def execute_alert_scan() -> dict:
    """
    Scans BigQuery financial optimization views and generates proactive alerts
    with concrete suggestions to reduce spend and accelerate HELOC paydown.
    Optionally posts the summary to ALERT_WEBHOOK_URL (Google Chat/Slack/Discord).
    """
    if not BQ_PROJECT_ID:
        raise HTTPException(status_code=500, detail="PROJECT_ID not set.")

    bq = bigquery.Client(project=BQ_PROJECT_ID)
    alerts = []

    # 1. Price Creep Check
    price_creep_sql = f"""
    SELECT merchant, min_charge, max_charge, estimated_annual_cost
    FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_active_subscriptions`
    WHERE has_price_increased = TRUE
    ORDER BY estimated_annual_cost DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(price_creep_sql).result())
        for r in rows:
            alerts.append({
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "title": f"Subscription Price Hike: {r.merchant}",
                "detail": f"Charge increased from ${r.min_charge:.2f} to ${r.max_charge:.2f} (Annual cost: ${r.estimated_annual_cost:.2f}).",
                "suggested_fix": f"Audit usage for {r.merchant} or cancel/rotate to save up to ${r.estimated_annual_cost:.2f}/year.",
            })
    except Exception as e:
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription check failed: {e}"})

    # 2. Food Efficiency Ratio Check
    food_sql = f"""
    SELECT month, grocery_spend, dining_delivery_spend, total_food_spend, dining_percentage_of_food_budget
    FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_food_efficiency`
    ORDER BY month DESC
    LIMIT 1;
    """
    try:
        rows = list(bq.query(food_sql).result())
        if rows:
            r = rows[0]
            if r.dining_percentage_of_food_budget > 35.0:
                potential_savings = round(r.dining_delivery_spend * 0.30, 2)
                alerts.append({
                    "type": "FOOD_LEAKAGE",
                    "severity": "WARNING",
                    "title": f"High Dining/Delivery Ratio ({r.dining_percentage_of_food_budget:.1f}% of food budget)",
                    "detail": f"In {r.month}, dining & delivery accounted for ${r.dining_delivery_spend:.2f} out of ${r.total_food_spend:.2f} total food spend.",
                    "suggested_fix": f"Shifting 2 delivery meals/month to home cooking could liberate ~${potential_savings:.2f}/month.",
                })
    except Exception as e:
        alerts.append({"type": "QUERY_ERROR", "detail": f"Food check failed: {e}"})

    # 3. HELOC Daily Cost & Opportunity Check
    heloc_sql = f"""
    SELECT display_name, current_balance, apr, daily_interest_cost, monthly_interest_cost
    FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_heloc_daily_cost`
    LIMIT 1;
    """
    try:
        rows = list(bq.query(heloc_sql).result())
        if rows and rows[0].current_balance > 0:
            h = rows[0]
            alerts.append({
                "type": "HELOC_OPPORTUNITY",
                "severity": "INFO",
                "title": f"HELOC Cost: ${h.daily_interest_cost:.2f}/day (${h.monthly_interest_cost:.2f}/mo)",
                "detail": f"Current balance is ${h.current_balance:,.2f} at {h.apr*100:.2f}% APR.",
                "suggested_fix": f"Every $100 trimmed from discretionary spend and swept into this debt eliminates ${h.apr*100:.1f}0 in compounding annual interest.",
            })
    except Exception as e:
        alerts.append({"type": "QUERY_ERROR", "detail": f"HELOC check failed: {e}"})

    # Post to Webhook if configured (Google Chat, Slack, Discord)
    webhook_url = resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if webhook_url and alerts:
        try:
            if "chat.googleapis.com" in webhook_url:
                # Native Google Chat Card V2 format with high-fidelity widgets
                widgets = []
                for a in alerts:
                    if a.get("title"):
                        widgets.append({
                            "decoratedText": {
                                "topLabel": a.get("type", "FINANCIAL ADVISORY").replace("_", " "),
                                "text": f"<b>{a['title']}</b><br><font color=\"#5f6368\">{a['detail']}</font><br>👉 <b>Action:</b> {a['suggested_fix']}",
                                "wrapText": True,
                            }
                        })
                payload = {
                    "text": "🔔 *Family Financial Copilot*: Proactive Advisory Scan completed with new recommendations.",
                    "cardsV2": [
                        {
                            "cardId": "financialAdvisorDailyAlert",
                            "card": {
                                "header": {
                                    "title": "Family Financial Copilot",
                                    "subtitle": "Daily Spend Optimization & Debt Advisory",
                                    "imageUrl": f"{os.getenv('SERVICE_URL', '').rstrip('/')}/avatar.png" if os.getenv("SERVICE_URL") else "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                                    "imageType": "CIRCLE",
                                },
                                "sections": [
                                    {
                                        "header": "Daily Optimization Opportunities",
                                        "widgets": widgets,
                                    }
                                ],
                            },
                        }
                    ],
                }
            else:
                # Discord / Slack Markdown fallback
                lines = ["**🔔 Family Financial Copilot: Proactive Advisory Scan**\n"]
                for a in alerts:
                    if a.get("title"):
                        lines.append(f"• **{a['title']}**\n  _{a['detail']}_\n  👉 **Action**: {a['suggested_fix']}\n")
                msg = "\n".join(lines)
                payload = {"content": msg, "text": msg}

            resp = requests.post(webhook_url, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            print(f"Webhook post failed: {e}")

    return {
        "status": "success",
        "alert_count": len([a for a in alerts if a.get("type") != "QUERY_ERROR"]),
        "alerts": alerts,
        "webhook_dispatched": webhook_sent,
    }


@app.post("/advisor/scan-alerts", dependencies=[Depends(verify_api_key)], tags=["Spend Optimization Advisor"])
async def scan_alerts():
    """
    Scans BigQuery financial optimization views and generates proactive alerts
    with concrete suggestions to reduce spend and accelerate HELOC paydown.
    Optionally posts the summary to ALERT_WEBHOOK_URL (Google Chat/Slack/Discord).
    """
    return await execute_alert_scan()


def ask_conversational_analytics(question: str, history: Optional[list] = None) -> dict:
    """
    Sends natural language question to Gemini Conversational Analytics Agent
    grounded in family_finance BigQuery dataset, with optional multi-turn conversation history.
    """
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        token = creds.token

        ca_parent = f"projects/{BQ_PROJECT_ID}/locations/global"
        url = f"https://geminidataanalytics.googleapis.com/v1beta/{ca_parent}:chat"

        messages_list = list(history or [])
        messages_list.append({"userMessage": {"text": question}})

        payload = {
            "parent": ca_parent,
            "messages": messages_list,
            "data_agent_context": {
                "data_agent": f"{ca_parent}/dataAgents/family-finance-advisor"
            },
        }

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "x-goog-user-project": BQ_PROJECT_ID,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )

        if resp.status_code != 200:
            logger.error(f"Conversational Analytics error {resp.status_code}: {resp.text}")
            return {
                "answer": f"I ran into an issue analyzing that: {resp.text[:200]}",
                "sql": None,
                "suggestions": [],
            }

        data = resp.json()
        messages = data if isinstance(data, list) else data.get("messages", [])

        answer_parts = []
        suggestions = []
        generated_sql = None

        for m in messages:
            sm = m.get("systemMessage", {})
            if "data" in sm and sm["data"].get("generatedSql"):
                generated_sql = sm["data"]["generatedSql"].strip()
            if "text" in sm and sm["text"].get("textType") != "THOUGHT":
                parts = sm["text"].get("parts", [])
                text_content = " ".join(parts).strip()
                if text_content:
                    answer_parts.append(text_content)

        if answer_parts:
            main_answer = "\n\n".join(answer_parts)
        else:
            main_answer = "I analyzed the financial database, but no specific recommendation was generated."

        return {
            "answer": main_answer,
            "sql": generated_sql,
            "suggestions": [],
        }
    except Exception as e:
        logger.error(f"Failed to query Conversational Analytics: {e}")
        return {
            "answer": f"Sorry, I had trouble processing your question: {e}",
            "sql": None,
            "suggestions": [],
        }


def run_readonly_sql(sql_query: str) -> str:
    """Executes a read-only GoogleSQL query against the family_finance BigQuery dataset (e.g. v_heloc_daily_cost, v_active_subscriptions, v_subscription_overlap, v_food_efficiency, v_micro_transaction_leakage, raw_accounts, raw_transactions)."""
    # Robust word-boundary regex check prevents bypasses via newlines, tabs, comments, or punctuation
    forbidden_pattern = r"\b(insert|update|delete|drop|truncate|alter|create|merge|grant|revoke)\b"
    match = re.search(forbidden_pattern, sql_query, re.IGNORECASE)
    if match:
        return f"Error: Only SELECT queries are permitted. Found forbidden keyword: {match.group(0)}"

    try:
        logger.info(f"Executing Gemini SQL tool call: {sql_query}")
        bq = bigquery.Client(project=BQ_PROJECT_ID)
        job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=100_000_000  # 100 MB scan budget safety cap
        )
        query_job = bq.query(sql_query, job_config=job_config)
        rows = list(query_job.result(max_results=50))
        if not rows:
            return "No rows returned from query."
        dict_rows = [dict(r.items()) for r in rows]
        return json.dumps(dict_rows, default=str)
    except Exception as e:
        logger.error(f"BigQuery execution error for query '{sql_query}': {e}")
        return f"BigQuery execution error: {e}"


def ask_gemini_brain(
    question: str,
    history: Optional[list] = None,
    images: Optional[list[tuple[bytes, str]]] = None,
) -> dict:
    """
    Primary AI Brain: Queries Google Gemini 3.8 Flash with MEDIUM thinking and live BigQuery tools.
    Falls back seamlessly to BigQuery Conversational Analytics if Gemini API Key is unconfigured.
    """
    gemini_key = resolve_secret("gemini-api-key", "GEMINI_API_KEY")

    try:
        if not genai:
            logger.warning("google-genai library not available, falling back to Conversational Analytics")
            return ask_conversational_analytics(question)

        # Prioritize API Key if available (which supports gemini-3.8-flash), falling back to Vertex AI
        if gemini_key:
            client = genai.Client(api_key=gemini_key)
        else:
            client = genai.Client(vertexai=True, project=BQ_PROJECT_ID, location=os.getenv("REGION", "us-central1"))

        gemini_history = []
        if history:
            for turn in history:
                if "userMessage" in turn:
                    u_text = turn["userMessage"].get("text", "")
                    if u_text:
                        gemini_history.append(types.Content(role="user", parts=[types.Part.from_text(text=u_text)]))
                elif "systemMessage" in turn:
                    parts = turn["systemMessage"].get("text", {}).get("parts", [])
                    s_text = " ".join(parts).strip()
                    if s_text:
                        gemini_history.append(types.Content(role="model", parts=[types.Part.from_text(text=s_text)]))

        system_instruction = (
            "You are an expert personal financial advisor and spend optimization strategist for a family. "
            f"Your single source of truth is Monarch Money synchronized into Google BigQuery dataset `{BQ_PROJECT_ID}.{BQ_DATASET_ID}`.\n\n"
            "CORE MISSION: Help the family optimize spending, eliminate waste, establish budget discipline, and aggressively pay down HELOC debt.\n\n"
            "ANALYTICAL VIEWS AND COLUMN SCHEMAS:\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_account_lifecycle`:\n"
            "   Columns: account_id, display_name, institution_name, account_class, type_name, subtype_name, current_balance, credit_limit, apr, lifecycle_status, is_primary_active, last_tx_date, tx_total, tx_45d, tx_90d, institution_latest_tx_date, institution_tx_count, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_heloc_daily_cost`:\n"
            "   Columns: account_id, display_name, institution_name, current_balance, credit_limit, available_credit, apr, daily_interest_cost, monthly_interest_cost, annual_interest_saved_per_500_monthly_reduction, is_apr_estimated, lifecycle_status, is_primary_active, last_tx_date, institution_latest_tx_date, institution_tx_count, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_active_subscriptions`:\n"
            "   Columns: merchant, category_name, charge_count, avg_charge, min_charge, max_charge, has_price_increased, billing_cadence, estimated_annual_cost, first_seen, last_seen, avg_cadence_days\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_subscription_overlap`:\n"
            "   Columns: category_name, active_subscriptions_count, category_annual_run_rate, combined_monthly_cost, active_services\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_food_efficiency`:\n"
            "   Columns: month, grocery_spend, dining_delivery_spend, total_food_spend, dining_percentage_of_food_budget\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_micro_transaction_leakage`:\n"
            "   Columns: merchant, category_name, frequency_90d, avg_ticket, total_spend_90d, annualized_run_rate\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_spend_classification`:\n"
            "   Columns: month, category_name, spend_type, total_amount, transaction_count\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_accounts`:\n"
            "   Columns: account_id, account_name, display_name, type_name, subtype_name, current_balance, available_balance, credit_limit, interest_rate, institution_name, is_asset, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_transactions`:\n"
            "   Columns: transaction_id, account_id, transaction_date, amount, merchant_name, clean_merchant_name, category_id, category_name, notes, is_recurring, pending, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.raw_categories`:\n"
            "   Columns: category_id, category_name, group_name, is_income, monthly_budget, updated_at\n\n"
            "CRITICAL RULES:\n"
            "1. Maximum Efficiency: Run at most 2-3 focused SQL queries per turn. Never loop through exploratory queries. As soon as you retrieve relevant rows, synthesize them immediately into clear, actionable advice.\n"
            "2. NEVER query INFORMATION_SCHEMA. All schemas are explicitly provided above; you must use the exact column names specified.\n"
            "3. If the user's message is brief, conversational, or a topic continuation (e.g. 'how do i fix my cash flow deficit', 'what about HELOC?', 'show more details', 'try again'), refer to the prior conversation history and run the single most relevant analytical view immediately.\n"
            "4. Always call `run_readonly_sql` to fetch exact figures. Never guess, estimate, or hallucinate numbers.\n"
            "5. For every dollar of recommended savings, calculate the exact debt acceleration impact: daily and annual interest eliminated on the HELOC and months shaved off payoff.\n"
            "6. Dynamic Account & Migration Intelligence (Zero Hardcoding): When answering questions about an account category (e.g. 'HELOC', 'mortgage', 'checking', 'credit card') where multiple accounts exist:\n"
            "   a. Inspect `is_primary_active` in the analytical views or evaluate transaction recency, active balance, and linking timestamps.\n"
            "   b. Focus your calculations and advice on the account flagged `is_primary_active = TRUE`.\n"
            "   c. Be transparent and explainable: Always name the institution and account you are quoting (e.g. 'Based on your active [Institution] [Account Name]...').\n"
            "   d. If a secondary or legacy account with a lingering balance exists, proactively add a brief advisory note highlighting it.\n"
            "7. For date-range or transaction volume inquiries (e.g. 'how much history do you have?'), run a single SQL aggregation query with MIN(transaction_date), MAX(transaction_date), and COUNT(*) from raw_transactions. Do NOT run multiple exploratory queries.\n"
            "8. Keep responses structured, concise, and formatted in clean markdown with bold metrics and bullet points.\n"
            "9. Format all currency as $X,XXX.XX.\n"
            "10. Multimodal Understanding: When the user provides images, screenshots, paystubs, statements, or compensation/outlook plans, thoroughly examine the visual data, parse every figure and projection, and integrate them directly into your financial analysis and debt paydown calculations."
        )

        try:
            thinking_config = types.ThinkingConfig(thinking_level="MEDIUM")
        except Exception:
            thinking_config = types.ThinkingConfig(thinking_budget=2048)

        chat = client.chats.create(
            model="gemini-3.8-flash",
            history=gemini_history,
            config=types.GenerateContentConfig(
                temperature=0.0,
                system_instruction=system_instruction,
                thinking_config=thinking_config,
                tools=[run_readonly_sql],
            ),
        )

        # Assemble user input with optional images
        message_parts = []
        if images:
            for img_bytes, mime_type in images:
                try:
                    message_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
                except Exception as ex:
                    logger.warning(f"Failed to convert image bytes to Gemini Part: {ex}")
        message_parts.append(types.Part.from_text(text=question))

        resp = chat.send_message(message_parts)
        answer_text = resp.text

        # If AFC completed tools or hit limit without a final text response, prompt for final synthesis
        if not answer_text or not answer_text.strip():
            logger.info("Gemini response text was empty after tool execution; prompting for final synthesis.")
            synthesis_resp = chat.send_message("Based on the data and query results above, provide your comprehensive financial analysis and actionable recommendations.")
            answer_text = synthesis_resp.text or "I analyzed your financial data, but no specific response was generated."

        # Extract any SQL queries executed during tool calls across conversation history
        executed_sqls = []
        try:
            for message in chat.get_history():
                for part in getattr(message, "parts", []):
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None) == "run_readonly_sql":
                        args = getattr(fc, "args", {}) or {}
                        q = args.get("sql_query")
                        if q and q not in executed_sqls:
                            executed_sqls.append(q)
        except Exception as e:
            logger.warning(f"Could not extract executed SQL from chat history: {e}")

        sql_summary = "\n\n".join(executed_sqls) if executed_sqls else None

        return {
            "answer": answer_text,
            "sql": sql_summary,
            "suggestions": [],
        }
    except Exception as e:
        logger.error(f"Gemini 3.8 Flash query failed: {e}; falling back to Conversational Analytics API.")
        return ask_conversational_analytics(question, history)



def format_advisory_reply(answer: str, sql: Optional[str] = None, suggestions: Optional[list] = None) -> str:
    """Formats the financial advisor response with optional SQL block and follow-up suggestions."""
    reply_lines = [f"💡 *Financial Advisory Response*:\n{answer}"]
    if sql:
        reply_lines.append(f"\n```sql\n{sql}\n```")
    if suggestions:
        reply_lines.append("\n*Suggested Follow-ups*:\n" + "\n".join([f"• {s}" for s in suggestions[:3]]))
    return "\n".join(reply_lines)


def format_chat_response(text: str, thread_name: Optional[str] = None, space_name: Optional[str] = None, is_addon: bool = True) -> dict:
    """
    Returns a clean, robust message response that strictly conforms to Google Workspace Add-ons
    (google.apps.card.v1.DataActions) and Google Chat API.
    Crucially anchors to thread_name and space_name so replies stay in the exact conversational thread.
    """
    formatted_text = text.replace("**", "*")
    msg_dict: dict = {"text": formatted_text}
    if thread_name:
        msg_dict["thread"] = {"name": thread_name}
    if space_name:
        msg_dict["space"] = {"name": space_name}

    if is_addon:
        # Strictly google.apps.card.v1.DataActions
        return {
            "hostAppDataAction": {
                "chatDataAction": {
                    "createMessageAction": {
                        "message": msg_dict
                    }
                }
            }
        }
    else:
        # Direct Google Chat API endpoint response
        return msg_dict


def post_to_chat_thread(text: str, thread_name: Optional[str] = None, space_name: Optional[str] = None) -> bool:
    """
    Posts a follow-up message into a specific Google Chat thread or space.
    Attempts Google Chat REST API via Application Default Credentials (chat.bot scope),
    falling back to ALERT_WEBHOOK_URL if in the default space.
    """
    if not space_name and thread_name and thread_name.startswith("spaces/"):
        space_name = thread_name.split("/threads/")[0]

    formatted_text = text.replace("**", "*")

    # 1. Primary: Use Google Chat API with ADC (Service Account)
    if space_name:
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
            creds.refresh(GoogleAuthRequest())
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            }
            url = f"https://chat.googleapis.com/v1/{space_name}/messages?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
            payload = {"text": formatted_text}
            if thread_name:
                payload["thread"] = {"name": thread_name}
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            logger.info(f"Google Chat API async reply to {space_name} (thread={thread_name}): status={resp.status_code}")
            if resp.status_code == 200:
                return True
        except Exception as e:
            logger.warning(f"Google Chat API async post encountered error: {e}; falling back to webhook.")

    # 2. Fallback: Incoming Webhook URL
    webhook_url = resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("No webhook URL configured to post follow-up chat message.")
        return False

    url = webhook_url
    if "messageReplyOption" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    payload = {"text": formatted_text}
    if thread_name:
        payload["thread"] = {"name": thread_name}

    try:
        resp = requests.post(url, json=payload, timeout=10)
        logger.info(f"Posted async reply via webhook to thread {thread_name}: status={resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to post async reply via webhook: {e}")
        return False


def download_chat_attachment(attachment: dict) -> Optional[tuple[bytes, str]]:
    """Download an uploaded media attachment from Google Chat using bot credentials."""
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
        creds.refresh(GoogleAuthRequest())
        headers = {"Authorization": f"Bearer {creds.token}"}

        resource_name = attachment.get("name")
        alt_resource_name = None
        if isinstance(attachment.get("attachmentDataRef"), dict):
            alt_resource_name = attachment["attachmentDataRef"].get("resourceName")
            if not resource_name:
                resource_name = alt_resource_name

        if not resource_name:
            logger.warning("No resourceName found for attachment")
            return None

        # Build candidate URLs for media download
        candidate_urls = [
            f"https://chat.googleapis.com/v1/media/{resource_name}?alt=media",
        ]
        if alt_resource_name and alt_resource_name != resource_name:
            candidate_urls.append(f"https://chat.googleapis.com/v1/media/{alt_resource_name}?alt=media")
        candidate_urls.append(f"https://chat.googleapis.com/v1/{resource_name}?alt=media")

        for url in candidate_urls:
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                if resp.status_code == 200 and resp.content:
                    content_type = attachment.get("contentType") or "image/png"
                    logger.info(f"Successfully downloaded attachment {attachment.get('contentName', 'image')} ({len(resp.content)} bytes, type={content_type})")
                    return resp.content, content_type
                else:
                    logger.debug(f"Media download from {url} returned {resp.status_code}")
            except Exception as e:
                logger.debug(f"Media download request to {url} failed: {e}")

        logger.warning(f"Could not download attachment with any candidate URL for {resource_name}")
        return None
    except Exception as e:
        logger.error(f"Error downloading attachment: {e}")
        return None


# In-memory caches for fast multi-turn responses within container lifetime
THREAD_HISTORY: dict[str, list[dict]] = {}
SPACE_HISTORY: dict[str, list[dict]] = {}


def get_session_history(thread_name: Optional[str], space_name: Optional[str]) -> list[dict]:
    """
    Retrieves conversational turns prioritizing the specific thread, falling back to the space session,
    and hydrated from BigQuery if in-memory cache is empty (e.g. post-deployment/cold start).
    """
    if thread_name and THREAD_HISTORY.get(thread_name):
        return list(THREAD_HISTORY[thread_name])

    if space_name and SPACE_HISTORY.get(space_name):
        return list(SPACE_HISTORY[space_name])

    # Hydrate from BigQuery chat_history if in-memory cache is empty
    sessions = [s for s in [thread_name, space_name] if s]
    if not sessions:
        return []

    try:
        bq = bigquery.Client(project=BQ_PROJECT_ID)
        query = f"""
            SELECT user_text, model_response 
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.chat_history`
            WHERE session_id IN UNNEST(@sessions)
            ORDER BY created_at DESC 
            LIMIT 5
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("sessions", "STRING", sessions)
            ]
        )
        rows = list(bq.query(query, job_config=job_config).result())
        history = []
        for r in reversed(rows):
            history.append({"userMessage": {"text": r.user_text}})
            history.append({"systemMessage": {"text": {"parts": [r.model_response]}}})

        if space_name and history:
            SPACE_HISTORY[space_name] = list(history)
        if thread_name and history:
            THREAD_HISTORY[thread_name] = list(history)

        return history
    except Exception as e:
        logger.warning(f"Could not hydrate chat history from BigQuery: {e}")
        return []


def save_session_history(thread_name: Optional[str], space_name: Optional[str], user_email: str, user_text: str, model_text: str):
    """
    Saves conversation turn to in-memory caches and persists to BigQuery.
    """
    turn_user = {"userMessage": {"text": user_text}}
    turn_model = {"systemMessage": {"text": {"parts": [model_text]}}}

    if thread_name:
        if thread_name not in THREAD_HISTORY:
            THREAD_HISTORY[thread_name] = []
        THREAD_HISTORY[thread_name].extend([turn_user, turn_model])
        THREAD_HISTORY[thread_name] = THREAD_HISTORY[thread_name][-10:]

    if space_name:
        if space_name not in SPACE_HISTORY:
            SPACE_HISTORY[space_name] = []
        SPACE_HISTORY[space_name].extend([turn_user, turn_model])
        SPACE_HISTORY[space_name] = SPACE_HISTORY[space_name][-10:]

    session_id = thread_name or space_name
    if session_id:
        try:
            bq = bigquery.Client(project=BQ_PROJECT_ID)
            bq.insert_rows_json(
                f"{BQ_PROJECT_ID}.{BQ_DATASET_ID}.chat_history",
                [{
                    "session_id": session_id,
                    "created_at": datetime.utcnow().isoformat(),
                    "user_email": user_email,
                    "user_text": user_text,
                    "model_response": model_text,
                }]
            )
        except Exception as e:
            logger.warning(f"Failed to persist chat history to BigQuery: {e}")



def verify_chat_origin(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Verifies that incoming /chat/event requests originate from Google Chat or an authorized caller.
    Blocks unauthenticated external callers from sending spoofed events to Cloud Run.
    """
    # 1. API key bypass for testing or admin curl
    if x_api_key:
        expected = resolve_secret("gemini-wrapper-key", "GEMINI_WRAPPER_KEY")
        if expected and secrets.compare_digest(x_api_key, expected):
            return True

    # 2. Local development skip
    if os.getenv("CHAT_AUTH_DISABLED", "false").lower() == "true":
        return True

    # 3. Google Chat OIDC Bearer token verification
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        try:
            claims = id_token.verify_oauth2_token(token, google_requests.Request())
            email = claims.get("email")
            iss = claims.get("iss", "")
            if email == "chat@system.gserviceaccount.com" or "accounts.google.com" in iss:
                return True
            logger.warning(f"Google Chat Bearer token has unverified issuer/email: email={email}, iss={iss}")
        except Exception as e:
            logger.error(f"Google Chat Bearer verification failed: {e}")
            raise HTTPException(status_code=401, detail=f"Invalid Google Chat authentication token: {e}")

    # 4. Custom chat verification token fallback
    chat_secret = resolve_secret("chat-verification-token", "CHAT_VERIFICATION_TOKEN")
    if chat_secret and authorization and secrets.compare_digest(authorization, chat_secret):
        return True

    # 5. On Cloud Run, block requests with no authentication credentials
    if os.getenv("K_SERVICE"):
        raise HTTPException(status_code=401, detail="Unauthorized: Missing valid Google Chat Bearer token or API key.")

    return True


@app.post("/chat/event", dependencies=[Depends(verify_chat_origin)])
async def google_chat_webhook(request: dict):
    """
    Handles interactive events from Google Chat (mentions, direct messages, slash commands).
    Transforms Google Chat into a conversational interface for family finances.
    """
    logger.info(f"Incoming Raw Chat Payload: {json.dumps(request)}")

    is_addon = bool(request.get("commonEventObject") or request.get("chat"))

    # Extract chat and message objects across all Google Chat & Workspace Add-on variations
    chat_obj = request.get("chat", {}) or {}
    message_payload = chat_obj.get("messagePayload", {}) or {}
    app_command_payload = chat_obj.get("appCommandPayload", {}) or {}
    message = (
        message_payload.get("message")
        or app_command_payload.get("message")
        or chat_obj.get("message")
        or request.get("message")
        or {}
    )
    event_type = (
        request.get("type")
        or ("SLASH_COMMAND" if app_command_payload else None)
        or ("ADDED_TO_SPACE" if "addedToSpacePayload" in chat_obj else None)
        or ("MESSAGE" if message else "UNKNOWN")
    )

    # Extract user info
    user_info = (
        chat_obj.get("user")
        or request.get("user")
        or message.get("sender")
        or {}
    )
    user_email = user_info.get("email") or user_info.get("displayName") or "unknown"
    sender_name = user_info.get("displayName", "there")

    logger.info(f"Parsed Chat Event: type={event_type}, user={user_email}, text={message.get('text')}")

    # Parse space.name robustly across all Google Chat event shapes
    space_obj = (
        message.get("space")
        or message_payload.get("space")
        or app_command_payload.get("space")
        or chat_obj.get("space")
        or request.get("space")
        or {}
    )
    space_name = space_obj.get("name") if isinstance(space_obj, dict) else (space_obj if isinstance(space_obj, str) else None)

    # Parse thread.name robustly across all Google Chat event shapes
    thread_obj = (
        message.get("thread")
        or message_payload.get("thread")
        or app_command_payload.get("thread")
        or chat_obj.get("thread")
        or request.get("thread")
        or {}
    )
    thread_name = thread_obj.get("name") if isinstance(thread_obj, dict) else (thread_obj if isinstance(thread_obj, str) else None)

    # If thread_name was omitted, derive from message.name if possible
    msg_name = message.get("name", "")
    if not thread_name and msg_name and "/messages/" in msg_name:
        parts = msg_name.split("/messages/")
        if len(parts) == 2:
            base_thread_id = parts[1].split(".")[0]
            thread_name = f"{parts[0]}/threads/{base_thread_id}"

    logger.info(f"Interaction Session Anchors: space={space_name}, thread={thread_name}")

    def respond(text: str) -> dict:
        resp = format_chat_response(text, thread_name=thread_name, space_name=space_name, is_addon=is_addon)
        logger.info(f"Outgoing Chat Response: {json.dumps(resp)}")
        return resp

    # Enforce family member allowlist if configured
    allowed_users_raw = resolve_secret("allowed-chat-users", "ALLOWED_CHAT_USERS")
    if allowed_users_raw:
        allowed_users = [u.strip().lower() for u in allowed_users_raw.split(",") if u.strip()]
        if allowed_users and user_email.lower() not in allowed_users:
            logger.warning(f"Unauthorized chat access attempt from '{user_email}'")
            return respond(f"🔒 Access Denied: User '{user_email}' is not authorized to query family finances.")

    # 1. Bot added to space or 1:1 DM
    if event_type == "ADDED_TO_SPACE":
        welcome_text = (
            "👋 Welcome to your *Family Financial Hub*!\n\n"
            "I am your personal finance advisor, connected directly to *Monarch Money* and *BigQuery*.\n\n"
            "*Try asking me:*\n"
            "• _What is our current daily HELOC interest cost?_\n"
            "• _What are our top 3 subscription expenses?_\n"
            "• _How much did we spend on groceries vs dining out last month?_\n"
            "• _Which sub-$35 micro-purchases are adding up?_\n\n"
            "*Shortcuts:*\n"
            "• `/sync` — Pull latest 30 days from Monarch\n"
            "• `/alerts` — Scan for subscription hikes and spend leakage"
        )
        return respond(welcome_text)

    if event_type == "REMOVED_FROM_SPACE":
        logger.info("Bot removed from space")
        return {}

    # 2. Extract attachments (images / screenshots)
    attachments = message.get("attachment") or message.get("attachments") or []
    downloaded_images = []
    if attachments and isinstance(attachments, list):
        for att in attachments:
            c_type = str(att.get("contentType", "")).lower()
            c_name = str(att.get("contentName", "")).lower()
            if c_type.startswith("image/") or c_name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                img_tuple = download_chat_attachment(att)
                if img_tuple:
                    downloaded_images.append(img_tuple)

    # 3. Extract text and strip @mention anywhere (beginning, middle, or end)
    raw_text = message.get("argumentText") or message.get("text") or ""
    clean_text = re.sub(r"@Family\s*Finance\s*Copilot", "", raw_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"@\S+", "", clean_text).strip()

    if not clean_text and downloaded_images:
        clean_text = "Please carefully examine the attached financial screenshot/document. Parse all numbers, projections, and line items, and provide a strategic financial analysis and recommendations."

    if not clean_text or clean_text.lower() in ("help", "/help"):
        help_text = (
            f"Hi {sender_name}! Here are some questions you can ask me:\n"
            "• _What is our daily HELOC interest burden?_\n"
            "• _What subscriptions had price increases recently?_\n"
            "• _How much did we spend on dining out last month?_\n"
            "• _What frequent small purchases are we making?_\n"
            "• `/sync` to pull latest transactions\n"
            "• `/alerts` to run proactive spend scan\n"
            "• *You can also paste screenshots or financial documents!*"
        )
        return respond(help_text)

    # Instant greeting responses
    greeting_patterns = ("hello", "hi", "hey", "are you there", "you there", "ping")
    if clean_text.lower().rstrip("?!. ") in greeting_patterns:
        greet_text = (
            f"👋 Yes {sender_name}, I'm here! I'm connected to your Monarch Money and BigQuery financial database.\n\n"
            "Ask me any question about your spending, subscriptions, or HELOC debt paydown (e.g. *\"What is our daily HELOC interest cost?\"*)."
        )
        return respond(greet_text)

    # Command: /sync or natural sync intent
    lower_text = clean_text.lower()
    is_sync_intent = (
        lower_text.startswith(("/sync", "sync", "/refresh", "refresh", "/backfill", "backfill"))
        or any(phrase in lower_text for phrase in ["please sync", "can you sync", "trigger sync", "run sync", "sync now", "sync data", "sync monarch", "refresh data", "pull history", "sync history", "backfill history"])
    )
    if is_sync_intent:
        if any(term in lower_text for term in ["all", "everything", "history", "full", "backfill"]):
            days = None
            history_desc = "all available history"
        else:
            days_match = re.search(r'\b(\d+)\b', lower_text)
            days = int(days_match.group(1)) if days_match else 30
            history_desc = f"last {days} days"
        try:
            sync_res = await execute_sync(days_back=days)
            counts = sync_res.get("synced_counts", {})
            return respond(
                f"✅ *Monarch Sync Complete!*\nSynced {counts.get('accounts', 0)} accounts, {counts.get('categories', 0)} categories, and {counts.get('transactions', 0)} transactions ({history_desc}) into BigQuery."
            )
        except Exception as e:
            return respond(f"⚠️ Sync failed: {e}")

    # Command: /alerts
    if clean_text.lower().startswith(("/alerts", "alerts")):
        try:
            scan_res = await execute_alert_scan()
            alerts = scan_res.get("alerts", [])
            if not alerts:
                return respond("✅ No alerts or price creeps detected right now!")
            lines = ["🔔 *Current Optimization Alerts:*\n"]
            for a in alerts:
                if a.get("title"):
                    lines.append(f"• *{a['title']}*\n  {a['detail']}\n  👉 {a['suggested_fix']}\n")
            return respond("\n".join(lines))
        except Exception as e:
            return respond(f"⚠️ Alert scan failed: {e}")

    # Natural language query -> Conversational Analytics Agent
    # If the response completes within 20s, return synchronously.
    # If it takes longer (deep multi-table BigQuery scans), acknowledge synchronously and post the full result into the thread via background task.
    session_history = get_session_history(thread_name, space_name)
    logger.info(f"Querying Gemini Brain with {len(session_history)} prior turns and {len(downloaded_images)} images (thread={thread_name}, space={space_name})")

    loop = asyncio.get_event_loop()
    analysis_future = loop.run_in_executor(None, ask_gemini_brain, clean_text, session_history, downloaded_images)

    try:
        result = await asyncio.wait_for(asyncio.shield(analysis_future), timeout=12.0)
        answer = result.get("answer", "No response generated.")
        sql = result.get("sql")
        suggestions = result.get("suggestions", [])

        if "no specific response was generated" not in answer.lower():
            save_session_history(thread_name, space_name, user_email, clean_text, answer)

        return respond(format_advisory_reply(answer, sql, suggestions))
    except asyncio.TimeoutError:
        logger.info(f"Query '{clean_text}' exceeded 12s budget; continuing in background to post to {space_name} (thread={thread_name})")

        async def complete_and_post():
            try:
                res = await analysis_future
                ans = res.get("answer", "No response generated.")
                s = res.get("sql")
                suggs = res.get("suggestions", [])

                if "no specific response was generated" not in ans.lower():
                    save_session_history(thread_name, space_name, user_email, clean_text, ans)

                post_to_chat_thread(format_advisory_reply(ans, s, suggs), thread_name, space_name)
            except Exception as ex:
                logger.error(f"Background query post failed: {ex}")
                post_to_chat_thread(f"⚠️ Query processing failed: {ex}", thread_name, space_name)

        asyncio.create_task(complete_and_post())
        return respond("⏳ *Analyzing...* Deep scan in progress across your BigQuery models. Posting full recommendation to this thread in just a few moments!")


