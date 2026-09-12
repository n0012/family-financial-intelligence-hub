import asyncio
import base64
import json
import logging
import os
import re
import secrets

import google.auth
import requests
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Security, UploadFile
from fastapi.security import APIKeyHeader
from google.auth.transport import requests as google_requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import bigquery
from google.oauth2 import id_token

from app.alerts import (
    build_chat_card_v2,
    build_executive_digest_card,
    build_snooze_success_card,
    check_paycheck_surplus_sweep,
    collect_all_alerts,
    execute_alert_scan,
    generate_daily_brief_synopsis,
    generate_executive_digest,
    get_daily_morning_brief,
    get_executive_cfo_digest,
    get_paycheck_surplus_analysis,
    snooze_spend_alert,
    suppress_alert,
)
from app.bq_service import (
    ask_conversational_analytics,
    get_bq_client,
    get_session_history,
    run_readonly_sql,
    save_session_history,
)
from app.config import (
    BQ_DATASET_ID,
    BQ_PROJECT_ID,
    IS_PROD,
    resolve_secret,
)
from app.memory_service import (
    format_memories_for_prompt,
    retrieve_user_memories,
    store_user_preference,
)
from app.monarch_service import (
    CURRENT_PROPOSED_CARD,
    CURRENT_USER_EMAIL,
    build_batch_recategorization_cancelled_card,
    build_batch_recategorization_success_card,
    build_recategorization_cancelled_card,
    build_recategorization_success_card,
    check_mutation_idempotency,
    check_mutation_rate_limit,
    execute_guarded_batch_recategorization,
    execute_guarded_recategorization,
    execute_sync,
    extract_card_action_parameters,
    find_next_recategorization_recommendation_async,
    get_live_account_balance,
    get_live_transaction,
    get_monarch_client,
    get_recategorization_recommendations,
    log_mutation_audit,
    mark_batch_status_async,
    propose_batch_recategorization,
    propose_transaction_recategorization,
    record_mutation_idempotency,
    request_plaid_refresh,
    verify_batch_signature,
    verify_mutation_signature,
)
from app.receipt_service import (
    format_tax_summary_text,
    get_tax_deductible_summary,
    get_tax_deduction_analysis,
    process_receipt_bytes,
)

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monarch-gemini")

is_prod = IS_PROD


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = None
    should_run_worker = os.getenv("ENABLE_CHAT_PULL_WORKER", "false").lower() in ("true", "1", "yes") or (
        os.getenv("K_SERVICE") and os.getenv("DISABLE_CHAT_PULL_WORKER", "false").lower() not in ("true", "1", "yes")
    )
    if should_run_worker:
        try:
            from app.chat_worker import start_chat_worker_background

            project = BQ_PROJECT_ID
            sub = os.getenv("CHAT_SUBSCRIPTION", "monarch-chat-sub")
            logger.info(f"Starting embedded Pub/Sub chat pull worker for {project}/{sub} (Zero Ingress Mode)...")
            worker = start_chat_worker_background(project_id=project, subscription_name=sub)
        except Exception as e:
            logger.warning(f"Could not start embedded Pub/Sub chat worker: {e}")

    yield

    if worker:
        worker.stop()


app = FastAPI(
    title="Monarch Money Gemini & BigQuery API",
    version="2.0.0",
    description="Secure wrapper exposing Monarch Money data to Gemini models and syncing to BigQuery.",
    docs_url=None if is_prod else "/docs",
    redoc_url=None if is_prod else "/redoc",
    openapi_url=None if is_prod else "/openapi.json",
    lifespan=lifespan,
)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


# MonarchMoney client management and API calls are handled in monarch_service.py


def verify_api_key(api_key: str | None = Security(API_KEY_HEADER)):
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
        raise HTTPException(status_code=502, detail=f"Monarch call get_accounts failed: {e}") from e


@app.get("/transactions", tags=["Monarch Data"])
async def get_transactions(
    start_date: str | None = Query(None, description="Start date in YYYY-MM-DD format (inclusive)"),
    end_date: str | None = Query(None, description="End date in YYYY-MM-DD format (inclusive)"),
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
        raise HTTPException(status_code=502, detail=f"Monarch call get_transactions failed: {e}") from e


@app.get("/categories", tags=["Monarch Data"])
async def get_categories(api_key: str = Security(verify_api_key)):
    """Fetch list of spending and income categories configured in Monarch Money."""
    client = await get_monarch_client()
    try:
        return await client.get_transaction_categories()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_categories failed: {e}") from e


@app.get("/cashflow", tags=["Monarch Data"])
async def get_cashflow(
    start_date: str | None = Query(None, description="Start date in YYYY-MM-DD format"),
    end_date: str | None = Query(None, description="End date in YYYY-MM-DD format"),
    api_key: str = Security(verify_api_key),
):
    """Fetch cashflow breakdown for a specific time window."""
    client = await get_monarch_client()
    try:
        return await client.get_cashflow(start_date=start_date, end_date=end_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Monarch call get_cashflow failed: {e}") from e


# execute_sync is imported from monarch_service.py


@app.post("/sync/bigquery", tags=["BigQuery Sync"])
async def sync_to_bigquery(
    days_back: int | None = Query(
        90, description="How many days back to sync transactions (pass 0 or None for all history)"
    ),
    mfa_code: str | None = Query(None, description="Optional 6-digit code from Google Authenticator"),
    api_key: str = Security(verify_api_key),
):
    """
    Synchronizes Monarch Money accounts, categories, and recent transactions into BigQuery.
    Used for daily automated synchronization via Cloud Scheduler or manual refresh.
    """
    effective_days = days_back if (days_back is not None and days_back > 0) else None
    return await execute_sync(days_back=effective_days, mfa_code=mfa_code)


@app.post("/advisor/scan-alerts", dependencies=[Depends(verify_api_key)], tags=["Spend Optimization Advisor"])
async def scan_alerts():
    """
    Scans BigQuery financial optimization views and generates proactive alerts
    with concrete suggestions to reduce spend and accelerate HELOC paydown.
    Optionally posts the summary to ALERT_WEBHOOK_URL (Google Chat/Slack/Discord).
    """
    return await execute_alert_scan()


@app.get("/advisor/morning-brief", dependencies=[Depends(verify_api_key)], tags=["Spend Optimization Advisor"])
async def morning_brief():
    """
    Retrieves the executive FinSage morning synopsis: liquid reserves, monthly fixed burn buffer,
    HELOC daily carry, month-to-date spending pacing, and what to pay attention to today.
    """
    bq = get_bq_client()
    alerts = await asyncio.to_thread(collect_all_alerts, bq, BQ_PROJECT_ID, BQ_DATASET_ID)
    return await asyncio.to_thread(generate_daily_brief_synopsis, bq, BQ_PROJECT_ID, BQ_DATASET_ID, alerts)


@app.get("/advisor/digest", dependencies=[Depends(verify_api_key)], tags=["Spend Optimization Advisor"])
async def executive_digest(period: str = "weekly"):
    """
    Retrieves the executive FinSage CFO digest (weekly or monthly) summarizing spend volume,
    category and merchant concentration, debt carry, and strategic capital allocation actions.
    """
    bq = get_bq_client()
    p = "MONTHLY" if period.upper().startswith("M") else "WEEKLY"
    return await asyncio.to_thread(generate_executive_digest, bq, BQ_PROJECT_ID, BQ_DATASET_ID, p)


@app.get("/advisor/surplus-sweep", dependencies=[Depends(verify_api_key)], tags=["Spend Optimization Advisor"])
async def paycheck_surplus_sweep():
    """
    Evaluates whether the family has safe surplus cash following recent paycheck deposits
    to sweep into variable-rate debt (HELOC), protecting 30-day fixed overhead reserves
    and computing exact daily, monthly, and annual interest savings.
    """
    bq = get_bq_client()
    alerts = await asyncio.to_thread(check_paycheck_surplus_sweep, bq, BQ_PROJECT_ID, BQ_DATASET_ID)
    return {
        "status": "success",
        "has_sweep_opportunity": len(alerts) > 0,
        "alerts": alerts,
    }


@app.post("/advisor/receipts/upload", dependencies=[Depends(verify_api_key)], tags=["Receipts & Tax Intelligence"])
async def upload_receipt(
    file: UploadFile = File(..., description="Receipt or invoice file (PNG, JPG, WEBP, PDF)"),
):
    """
    Ingests a receipt or invoice document, executes Zero-PII multimodal extraction with Gemini Vision,
    reconciles against BigQuery raw_transactions within -3d to +10d, saves record to BigQuery,
    and returns tax deductibility breakdown and Google Chat Card v2.
    """
    MAX_UPLOAD_BYTES = 15 * 1024 * 1024
    content_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds 15MB limit.")

    ext = os.path.splitext(file.filename or "")[1].lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
    }
    mime = mime_map.get(ext) or file.content_type or "image/jpeg"
    bq = get_bq_client()
    result = await asyncio.to_thread(process_receipt_bytes, content_bytes, mime_type=mime, bq=bq)
    return result


@app.get("/advisor/tax-summary", dependencies=[Depends(verify_api_key)], tags=["Receipts & Tax Intelligence"])
async def tax_summary(tax_year: int | None = Query(None, description="Tax year (e.g. 2026)")):
    """
    Retrieves annual tax deductible summary by category (Schedule C, HSA/FSA, Charity, Childcare)
    from BigQuery v_tax_deductible_summary.
    """
    import datetime

    bq = get_bq_client()
    target_year = tax_year or datetime.date.today().year
    rows = await asyncio.to_thread(get_tax_deductible_summary, bq, BQ_PROJECT_ID, BQ_DATASET_ID, target_year)
    return {
        "tax_year": target_year,
        "summary": rows,
        "formatted_text": format_tax_summary_text(rows, target_year),
    }


def run_readonly_sql_tool(sql_query: str) -> str:
    """
    Executes a read-only GoogleSQL query against the family_finance BigQuery dataset
    (e.g. v_debt_daily_cost, v_debt_summary, v_heloc_daily_cost, v_active_subscriptions,
    v_subscription_price_creep, v_subscription_overlap, v_utility_seasonal_baseline,
    v_food_efficiency, v_micro_transaction_leakage, raw_accounts, raw_transactions).

    Args:
        sql_query: The GoogleSQL SELECT query to execute.
    """
    return run_readonly_sql(sql_query)


def ask_gemini_brain(
    question: str,
    history: list | None = None,
    images: list[tuple[bytes, str]] | None = None,
    user_email: str | None = None,
) -> dict:
    """
    Primary AI Brain: Queries Google Gemini 3.8 Flash with MEDIUM thinking, live BigQuery analytical views,
    Vertex AI Agent Platform Memory Bank long-term preferences, and Monarch tools.
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

        target_user = user_email or CURRENT_USER_EMAIL.get() or "user@example.com"
        user_memories = retrieve_user_memories(target_user)
        memory_block = format_memories_for_prompt(user_memories)

        system_instruction = (
            "You are FinSage, an expert personal financial advisor and spend optimization strategist for a family. "
            f"Your single source of truth is Monarch Money synchronized into Google BigQuery dataset `{BQ_PROJECT_ID}.{BQ_DATASET_ID}`.\n\n"
            "CORE MISSION: Help the family optimize spending, eliminate waste, establish budget discipline, track liabilities across mortgage, HELOC, and other loans, and aggressively optimize debt carrying costs and paydown.\n\n"
            "ANALYTICAL VIEWS AND COLUMN SCHEMAS:\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_account_lifecycle`:\n"
            "   Columns: account_id, display_name, institution_name, account_class, type_name, subtype_name, current_balance, credit_limit, apr, lifecycle_status, is_primary_active, last_tx_date, tx_total, tx_45d, tx_90d, institution_latest_tx_date, institution_tx_count, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_debt_daily_cost`:\n"
            "   Columns: account_id, display_name, institution_name, debt_type, account_class, current_balance, credit_limit, available_credit, apr, daily_interest_cost, monthly_interest_cost, annual_interest_saved_per_500_monthly_reduction, is_apr_estimated, lifecycle_status, is_primary_active, last_tx_date, institution_latest_tx_date, institution_tx_count, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_debt_summary`:\n"
            "   Columns: total_debt_balance, total_daily_interest_cost, total_monthly_interest_cost, mortgage_balance, mortgage_daily_interest_cost, heloc_balance, heloc_daily_interest_cost, other_debt_balance, other_debt_daily_interest_cost, total_debt_accounts\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_heloc_daily_cost`:\n"
            "   Columns: account_id, display_name, institution_name, current_balance, credit_limit, available_credit, apr, daily_interest_cost, monthly_interest_cost, annual_interest_saved_per_500_monthly_reduction, is_apr_estimated, lifecycle_status, is_primary_active, last_tx_date, institution_latest_tx_date, institution_tx_count, updated_at\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_paycheck_surplus_sweep`:\n"
            "   Columns: target_debt_name, target_debt_balance, target_debt_apr, recommended_sweep_amount, daily_interest_saved, monthly_interest_saved, annual_interest_saved, heloc_name, heloc_balance, heloc_apr, total_debt_balance, total_daily_debt_cost, mortgage_balance, alert_key\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_active_subscriptions`:\n"
            "   Columns: merchant, category_name, functional_domain, disposition, is_overlap_eligible, overlap_min_count, charge_count, typical_charge, avg_charge, min_charge, max_charge, charge_variability, billing_cadence, estimated_annual_cost, monthly_run_rate, first_seen, last_seen, avg_cadence_days, days_since_last_charge, is_currently_active\n"
            "   Note: one row per merchant, incidental point-of-sale purchases already removed. Use typical_charge (the recurring tier) not avg_charge when quoting a subscription's price, and monthly_run_rate when summing across mixed billing cadences.\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_subscription_price_creep`:\n"
            "   Columns: merchant, category_name, functional_domain, disposition, billing_cadence, latest_charge, prior_charge, price_increase_amount, pct_increase, annual_impact, estimated_annual_cost, effective_date, days_since_prior_charge\n"
            "   Note: annual_impact is the annualised cost of the INCREASE. estimated_annual_cost is the whole plan. Never quote the plan cost as the saving from a price rise.\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_subscription_overlap`:\n"
            "   Columns: functional_domain, active_service_count, combined_monthly_cost, combined_annual_cost, consolidation_savings_monthly, consolidation_savings_annual, active_services\n"
            f"- `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.v_utility_seasonal_baseline`:\n"
            "   Columns: merchant, spend_month, month_total, seasonal_avg, seasonal_stddev, years_observed, variance_vs_season, variance_pct\n"
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
            "5. For every dollar of recommended savings, calculate the exact debt acceleration impact: daily and annual interest eliminated across liabilities (prioritizing high-interest variable debt like HELOC) and months shaved off payoff.\n"
            "5b. Respect `disposition` before recommending any action on a recurring charge. CANCELLABLE may be cancelled, downgraded or rotated. RESHOPPABLE (insurance, telecom, broadband, monitored security) is contractual: recommend re-quoting or negotiating the renewal, never 'cancel to save'. ESSENTIAL_METERED (electric, gas, water) is a regulated monopoly with no substitute: only ever discuss consumption or rate schedules, and compare against v_utility_seasonal_baseline rather than against the previous month, because heating and cooling swings are seasonal, not price rises. UNKNOWN means unclassified: describe the charge, do not advise cancelling it.\n"
            "5c. Never present a subscription's total run rate as the saving from a price increase. The recoverable amount is the increase itself (`annual_impact`), and for an overlap it is `consolidation_savings_monthly`, which already assumes one service is kept.\n"
            "6. Dynamic Account & Migration Intelligence (Zero Hardcoding): When answering questions about an account category (e.g. 'HELOC', 'mortgage', 'checking', 'credit card') where multiple accounts exist:\n"
            "   a. Inspect `is_primary_active` in the analytical views or evaluate transaction recency, active balance, and linking timestamps.\n"
            "   b. Focus your calculations and advice on the account flagged `is_primary_active = TRUE`.\n"
            "   c. Be transparent and explainable: Always name the institution and account you are quoting (e.g. 'Based on your active [Institution] [Account Name]...').\n"
            "   d. If a secondary or legacy account with a lingering balance exists, proactively add a brief advisory note highlighting it.\n"
            "7. For date-range or transaction volume inquiries (e.g. 'how much history do you have?'), run a single SQL aggregation query with MIN(transaction_date), MAX(transaction_date), and COUNT(*) from raw_transactions. Do NOT run multiple exploratory queries.\n"
            "8. Keep responses structured, concise, and formatted in clean markdown with bold metrics and bullet points.\n"
            "9. Format all currency as $X,XXX.XX.\n"
            "10. Multimodal Understanding: When the user provides images, screenshots, paystubs, statements, or compensation/outlook plans, thoroughly examine the visual data, parse every figure and projection, and integrate them directly into your financial analysis and debt paydown calculations.\n"
            "11. Live Monarch Confirmation & Plaid Tools: BigQuery is your primary historical analytical engine. If the user asks for up-to-the-minute balance checks (e.g. 'what is my balance right now?', 'did that payment post?'), call `get_live_account_balance(account_identifier)` to confirm live figures directly from Monarch. To inspect a specific transaction's pending status, call `get_live_transaction(transaction_id)`. If an institution's data appears stale, call `request_plaid_refresh(institution_name)`.\n"
            "12. Human-in-the-Loop Recategorizations: When the user requests to reclassify, recategorize, or fix transactions:\n"
            "    - For recurring merchants or multiple transactions (e.g. streaming services like Netflix, Hulu, Prime Video), call `propose_batch_recategorization(merchant_name, new_category, current_category)`. This prepares an interactive confirmation card in Google Chat that allows the user to reclassify all matching transactions in a single click.\n"
            "    - For a single specific transaction, call `propose_transaction_recategorization(transaction_id, new_category)`.\n"
            "    - Proactive Next Recommendations: You are equipped with `get_recategorization_recommendations(exclude_merchant)`. After proposing or executing a batch recategorization, or when auditing transactions, proactively run this tool to identify the next high-confidence misclassified merchant and suggest fixing it.\n"
            "    - Never attempt to mutate transactions directly; both tools strictly prepare HMAC-signed confirmation cards requiring the user's interactive confirmation in Google Chat.\n"
            "13. Persistent User Preferences: You are equipped with `store_user_preference(preference_or_rule)` to remember the user's explicit goals, spending limits, debt acceleration targets, budget caps, or alert preferences. Whenever the user asks you to remember something, sets a budget cap, specifies a target date, or establishes a financial rule, call `store_user_preference` to persist it into their long-term Memory Bank.\n"
            "14. Proactive Alert Suppression & Snooze: If the user asks to dismiss, snooze, or stop alerting about a specific merchant, habit, overlap, or price increase (e.g. 'snooze Netflix alert for 30 days', 'mute food leakage alerts'), call `snooze_spend_alert(alert_key_or_name, days)`. This updates BigQuery alert suppression so the item will not be repeatedly flagged in daily scans.\n"
            "15. Daily Morning Brief & Synopsis: You are equipped with `get_daily_morning_brief()` to retrieve the executive morning synopsis (liquid cash reserves, monthly fixed burn buffer, debt daily carry across Mortgage and HELOC, MTD spend pacing, and high-priority items to pay attention to today). Call this whenever the user asks for the morning brief, daily financial synopsis, or daily overview.\n"
            "16. Tax Deductibility & Receipts: You are equipped with `get_tax_deduction_analysis(tax_year)` to retrieve annual tax-deductible expense summaries (Schedule C business expenses, HSA/FSA medical expenses, 501(c)(3) charitable contributions, and childcare/dependent care). Call this whenever the user asks about tax deductions, write-offs, HSA spending, or annual tax summaries.\n"
            f"{memory_block}"
        )

        try:
            thinking_config = types.ThinkingConfig(thinking_level="MEDIUM")
        except Exception:
            thinking_config = types.ThinkingConfig(thinking_budget=2048)

        CURRENT_PROPOSED_CARD.set(None)

        chat = client.chats.create(
            model="gemini-3.8-flash",
            history=gemini_history,
            config=types.GenerateContentConfig(
                temperature=0.0,
                system_instruction=system_instruction,
                thinking_config=thinking_config,
                tools=[
                    run_readonly_sql_tool,
                    get_live_account_balance,
                    get_live_transaction,
                    request_plaid_refresh,
                    propose_transaction_recategorization,
                    propose_batch_recategorization,
                    get_recategorization_recommendations,
                    store_user_preference,
                    snooze_spend_alert,
                    get_daily_morning_brief,
                    get_executive_cfo_digest,
                    get_paycheck_surplus_analysis,
                    get_tax_deduction_analysis,
                ],
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
            synthesis_resp = chat.send_message(
                "Based on the data and query results above, provide your comprehensive financial analysis and actionable recommendations."
            )
            answer_text = (
                synthesis_resp.text or "I analyzed your financial data, but no specific response was generated."
            )

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
        proposed_card = CURRENT_PROPOSED_CARD.get()

        return {
            "answer": answer_text,
            "sql": sql_summary,
            "suggestions": [],
            "card": proposed_card,
        }
    except Exception as e:
        logger.error(f"Gemini 3.8 Flash query failed: {e}; falling back to Conversational Analytics API.")
        return ask_conversational_analytics(question, history)


def format_advisory_reply(answer: str, sql: str | None = None, suggestions: list | None = None) -> str:
    """Formats the financial advisor response with optional SQL block and follow-up suggestions."""
    reply_lines = [f"💡 *Financial Advisory Response*:\n{answer}"]
    if sql:
        reply_lines.append(f"\n```sql\n{sql}\n```")
    if suggestions:
        reply_lines.append("\n*Suggested Follow-ups*:\n" + "\n".join([f"• {s}" for s in suggestions[:3]]))
    return "\n".join(reply_lines)


def format_chat_response(
    text: str,
    thread_name: str | None = None,
    space_name: str | None = None,
    is_addon: bool = True,
    cards_v2: list | None = None,
) -> dict:
    """
    Returns a clean, robust message response that strictly conforms to Google Workspace Add-ons
    (google.apps.card.v1.DataActions) and Google Chat API.
    Crucially anchors to thread_name and space_name so replies stay in the exact conversational thread.
    Optionally attaches cardsV2 for interactive mutations or rich widgets.
    """
    formatted_text = text.replace("**", "*")
    msg_dict: dict = {"text": formatted_text}
    if thread_name:
        msg_dict["thread"] = {"name": thread_name}
    if space_name:
        msg_dict["space"] = {"name": space_name}
    if cards_v2:
        msg_dict["cardsV2"] = cards_v2

    if is_addon:
        # Strictly google.apps.card.v1.DataActions
        return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": msg_dict}}}}
    else:
        # Direct Google Chat API endpoint response
        return msg_dict


def post_to_chat_thread(
    text: str,
    thread_name: str | None = None,
    space_name: str | None = None,
    cards_v2: list | None = None,
) -> bool:
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
            payload: dict = {"text": formatted_text}
            if thread_name:
                payload["thread"] = {"name": thread_name}
            if cards_v2:
                payload["cardsV2"] = cards_v2
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            logger.info(
                f"Google Chat API async reply to {space_name} (thread={thread_name}): status={resp.status_code}"
            )
            if resp.status_code == 200:
                return True
            else:
                logger.warning(
                    f"Google Chat API async post returned {resp.status_code}: {resp.text}"
                )
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
    if thread_name and thread_name != "None" and thread_name != "spaces/None":
        payload["thread"] = {"name": thread_name}
    if cards_v2:
        payload["cardsV2"] = cards_v2

    try:
        resp = requests.post(url, json=payload, timeout=10)
        logger.info(f"Posted async reply via webhook to thread {thread_name}: status={resp.status_code}")
        if resp.status_code != 200:
            logger.warning(f"Webhook post returned {resp.status_code}: {resp.text}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to post async reply via webhook: {e}")
        return False


def patch_chat_card(message_name: str, cards_v2: list, text: str | None = None) -> bool:
    """Updates the cards of an existing message in-place (e.g. replacing action buttons upon confirmation)."""
    if not message_name:
        return False
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/chat.bot"])
        creds.refresh(GoogleAuthRequest())
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
        update_mask = "cardsV2,text" if text is not None else "cardsV2"
        url = f"https://chat.googleapis.com/v1/{message_name}?updateMask={update_mask}"
        payload: dict = {"cardsV2": cards_v2}
        if text is not None:
            payload["text"] = text
        resp = requests.patch(url, headers=headers, json=payload, timeout=10)
        logger.info(f"Google Chat API patch {message_name}: status={resp.status_code}")
        if resp.status_code != 200:
            logger.warning(f"Google Chat API patch returned {resp.status_code}: {resp.text}")
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Google Chat API patch encountered error: {e}")
        return False


def download_chat_attachment(attachment: dict) -> tuple[bytes, str] | None:
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
                    if len(resp.content) > 15 * 1024 * 1024:
                        logger.warning(f"Attachment exceeds 15MB byte limit ({len(resp.content)} bytes); skipping.")
                        return None
                    content_name = attachment.get("contentName", "").lower()
                    mime_map = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".webp": "image/webp",
                        ".pdf": "application/pdf",
                    }
                    ext = os.path.splitext(content_name)[1]
                    content_type = mime_map.get(ext) or "image/jpeg"
                    logger.info(f"Successfully downloaded attachment ({len(resp.content)} bytes, type={content_type})")
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


def verify_chat_origin(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
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

    # 2. Local development skip (STRICTLY non-production only)
    if not IS_PROD and os.getenv("CHAT_AUTH_DISABLED", "false").lower() == "true":
        logger.warning("CHAT_AUTH_DISABLED is active in non-production environment.")
        return True

    # 3. Custom chat verification token fallback
    chat_secret = resolve_secret("chat-verification-token", "CHAT_VERIFICATION_TOKEN")
    if chat_secret and authorization and secrets.compare_digest(authorization, chat_secret):
        return True

    # 4. Google Chat OIDC Bearer token verification
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
        try:
            chat_audience = resolve_secret("chat-audience", "CHAT_AUDIENCE") or os.getenv("CLOUD_RUN_URL")
            if not chat_audience:
                logger.error("Chat audience is not configured; refusing to verify token without audience")
                raise HTTPException(status_code=401, detail="Chat audience configuration missing")
            claims = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                audience=chat_audience,
            )
            email = claims.get("email")
            iss = claims.get("iss", "")

            # Issuer must strictly be Google Accounts
            if iss not in ("accounts.google.com", "https://accounts.google.com"):
                logger.warning(f"Google Chat Bearer token has invalid issuer: iss={iss}")
                raise HTTPException(status_code=401, detail=f"Invalid Google Chat token issuer: {iss}")

            # Caller identity must be the official Google Chat service account
            # or an explicitly allowed project service account
            pubsub_sa = resolve_secret("pubsub-service-account", "PUBSUB_SERVICE_ACCOUNT")
            allowed_service_accounts = {
                "chat@system.gserviceaccount.com",
                f"monarch-scheduler-sa@{BQ_PROJECT_ID}.iam.gserviceaccount.com",
                f"monarch-gemini-run@{BQ_PROJECT_ID}.iam.gserviceaccount.com",
            }
            if pubsub_sa:
                allowed_service_accounts.add(pubsub_sa.strip())

            if email not in allowed_service_accounts:
                logger.warning(f"Google Chat Bearer token caller not authorized: email={email}")
                raise HTTPException(status_code=403, detail=f"Unauthorized caller service account: {email}")

            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Google Chat Bearer verification failed: {e}")
            raise HTTPException(status_code=401, detail=f"Invalid Google Chat authentication token: {e}") from e

    # 5. Block all unauthenticated requests
    raise HTTPException(
        status_code=401, detail="Unauthorized: Missing valid Google Chat Bearer token, verification secret, or API key."
    )


@app.post("/chat/event", dependencies=[Depends(verify_chat_origin)])
@app.post("/chat/pubsub", dependencies=[Depends(verify_chat_origin)])
async def google_chat_webhook(request: dict, is_pubsub_override: bool = False):
    """
    Handles interactive events from Google Chat (mentions, direct messages, slash commands).
    Supports direct HTTP webhooks and private Google Cloud Pub/Sub push/pull delivery.
    """
    is_pubsub = is_pubsub_override
    raw_payload = request

    # Unpack Pub/Sub Push wrapper if present
    if "message" in request and isinstance(request["message"], dict) and "data" in request["message"]:
        try:
            b64_data = request["message"]["data"]
            decoded_str = base64.b64decode(b64_data).decode("utf-8")
            raw_payload = json.loads(decoded_str)
            is_pubsub = True
            logger.info("Successfully unpacked Google Chat event from Pub/Sub push message.")
        except Exception as e:
            logger.error(f"Failed to decode Pub/Sub message data: {e}")
            return {"status": "error", "message": str(e)}

    logger.info(f"Incoming Chat Payload (pubsub={is_pubsub}): {json.dumps(raw_payload)}")

    is_addon = bool(raw_payload.get("commonEventObject") or raw_payload.get("chat"))

    # Extract chat and message objects across all Google Chat & Workspace Add-on variations
    chat_obj = raw_payload.get("chat", {}) or {}
    message_payload = chat_obj.get("messagePayload", {}) or {}
    app_command_payload = chat_obj.get("appCommandPayload", {}) or {}
    button_clicked_payload = chat_obj.get("buttonClickedPayload", {}) or {}
    message = (
        message_payload.get("message")
        or app_command_payload.get("message")
        or button_clicked_payload.get("message")
        or chat_obj.get("message")
        or raw_payload.get("message")
        or {}
    )
    event_type = (
        raw_payload.get("type")
        or (
            "CARD_CLICKED"
            if raw_payload.get("action")
            or button_clicked_payload
            or (raw_payload.get("commonEventObject", {}).get("invokedFunction"))
            or (raw_payload.get("common", {}).get("invokedFunction"))
            or (raw_payload.get("commonEventObject", {}).get("parameters"))
            or (raw_payload.get("common", {}).get("parameters"))
            else None
        )
        or ("SLASH_COMMAND" if app_command_payload else None)
        or ("ADDED_TO_SPACE" if "addedToSpacePayload" in chat_obj else None)
        or ("MESSAGE" if message else "UNKNOWN")
    )

    # Extract user info
    user_info = chat_obj.get("user") or raw_payload.get("user") or message.get("sender") or {}
    user_email = user_info.get("email") or user_info.get("displayName") or "unknown"
    sender_name = user_info.get("displayName", "there")
    CURRENT_USER_EMAIL.set(user_email)

    logger.info(f"Parsed Chat Event: type={event_type}, user={user_email}, text={message.get('text')}")

    # Parse space.name robustly across all Google Chat event shapes
    space_obj = (
        button_clicked_payload.get("space")
        or message.get("space")
        or message_payload.get("space")
        or app_command_payload.get("space")
        or chat_obj.get("space")
        or raw_payload.get("space")
        or {}
    )
    space_name = (
        space_obj.get("name") if isinstance(space_obj, dict) else (space_obj if isinstance(space_obj, str) else None)
    )

    # Parse thread.name robustly across all Google Chat event shapes
    thread_obj = (
        message.get("thread")
        or button_clicked_payload.get("thread")
        or message_payload.get("thread")
        or app_command_payload.get("thread")
        or chat_obj.get("thread")
        or raw_payload.get("thread")
        or {}
    )
    thread_name = (
        thread_obj.get("name")
        if isinstance(thread_obj, dict)
        else (thread_obj if isinstance(thread_obj, str) else None)
    )

    # If thread_name was omitted, derive from message.name if possible
    msg_name = message.get("name", "")
    if not thread_name and msg_name and "/messages/" in msg_name:
        parts = msg_name.split("/messages/")
        if len(parts) == 2:
            base_thread_id = parts[1].split(".")[0]
            thread_name = f"{parts[0]}/threads/{base_thread_id}"

    logger.info(f"Interaction Session Anchors: space={space_name}, thread={thread_name}")

    def respond(text: str, cards_v2: list | None = None) -> dict:
        if is_pubsub:
            post_to_chat_thread(text, thread_name=thread_name, space_name=space_name, cards_v2=cards_v2)
            return {"status": "ok"}
        resp = format_chat_response(
            text, thread_name=thread_name, space_name=space_name, is_addon=is_addon, cards_v2=cards_v2
        )
        logger.info(f"Outgoing Chat Response: {json.dumps(resp)}")
        return resp

    # Enforce family member allowlist if configured
    allowed_users_raw = resolve_secret("allowed-chat-users", "ALLOWED_CHAT_USERS")
    if allowed_users_raw:
        allowed_users = [u.strip().lower() for u in allowed_users_raw.split(",") if u.strip()]
        if allowed_users and user_email.lower() not in allowed_users:
            logger.warning(f"Unauthorized chat access attempt from '{user_email}'")
            return respond(f"🔒 Access Denied: User '{user_email}' is not authorized to query family finances.")

    # 0. Interactive Card Action Click (Guarded recategorization confirmation)
    if event_type == "CARD_CLICKED":
        action_name, action_params = extract_card_action_parameters(raw_payload)
        logger.info(f"Card Action Clicked: action={action_name}, params={action_params}")

        if action_name == "confirm_recategorize":
            txn_id = action_params.get("transaction_id", "")
            cat_id = action_params.get("category_id", "")
            cat_name = action_params.get("category_name", "Updated Category")
            merchant_name = action_params.get("merchant_name")
            raw_amount = action_params.get("amount")
            try:
                amount_val = float(raw_amount) if raw_amount else None
            except (ValueError, TypeError):
                amount_val = None
            ts_str = action_params.get("timestamp", "0")
            target_user = action_params.get("user_email", "unknown")
            sig = action_params.get("signature", "")

            # 1. Rate Limiting Check
            allowed, rate_msg = check_mutation_rate_limit(user_email)
            if not allowed:
                logger.warning(f"Mutation rate limit exceeded for user '{user_email}' on txn #{txn_id}")
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="RATE_LIMITED",
                    new_value=cat_name,
                    details=rate_msg,
                )
                return respond(f"⛔ {rate_msg}")

            # 2. Timestamp Validation
            try:
                ts = int(ts_str)
            except ValueError:
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="REJECTED",
                    new_value=cat_name,
                    signature_valid=False,
                    details="Invalid timestamp in confirmation card",
                )
                return respond("⛔ Invalid timestamp in confirmation card.")

            # 3. Cryptographic Signature & Freshness Validation
            is_valid, reason = verify_mutation_signature(txn_id, cat_id, target_user, ts, sig)
            if not is_valid:
                logger.warning(f"Mutation signature rejected: {reason} (txn={txn_id}, user={user_email})")
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="REJECTED",
                    new_value=cat_name,
                    signature_valid=False,
                    details=reason,
                )
                return respond(f"⛔ Confirmation rejected: {reason}")

            # 4. Idempotency Check (prevent duplicate replay mutations for authenticated requests)
            cached_result = check_mutation_idempotency(user_email, "RECATEGORIZE_TRANSACTION", txn_id, cat_id)
            if cached_result:
                logger.info(f"Idempotent replay detected for txn #{txn_id} to category #{cat_id}")
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="NOOP",
                    new_value=cat_name,
                    signature_valid=True,
                    details="Idempotent replay: already executed within cooldown window.",
                )
                success_card = build_recategorization_success_card(
                    transaction_id=txn_id,
                    category_name=cat_name,
                    merchant_name=merchant_name,
                    amount=amount_val,
                )
                if msg_name:
                    patch_chat_card(msg_name, [success_card], text="✅ Transaction already reclassified.")
                    return respond(f"✅ Transaction #{txn_id} was already reclassified to *{cat_name}*.")
                return respond(
                    f"✅ Transaction #{txn_id} was already reclassified to *{cat_name}*.",
                    cards_v2=[success_card],
                )

            # 5. Execute Guarded Mutation
            mutation_result = await execute_guarded_recategorization(
                transaction_id=txn_id,
                category_id=cat_id,
                category_name=cat_name,
            )

            if mutation_result.get("success"):
                record_mutation_idempotency(user_email, "RECATEGORIZE_TRANSACTION", txn_id, cat_id, mutation_result)
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="SUCCESS",
                    new_value=cat_name,
                    signature_valid=True,
                    details="Successfully recategorized and synchronized to BigQuery",
                )
                success_card = build_recategorization_success_card(
                    transaction_id=txn_id,
                    category_name=cat_name,
                    merchant_name=merchant_name,
                    amount=amount_val,
                )
                if msg_name:
                    patch_chat_card(msg_name, [success_card], text="✅ Transaction successfully updated.")
                    success_msg = (
                        f"✅ Transaction #{txn_id} was successfully reclassified to *{cat_name}* in Monarch Money."
                    )
                    return respond(success_msg)
                success_msg = (
                    f"✅ Transaction #{txn_id} was successfully reclassified to *{cat_name}* in Monarch Money."
                )
                return respond(success_msg, cards_v2=[success_card])
            else:
                err = mutation_result.get("error", "Unknown error")
                log_mutation_audit(
                    action_type="RECATEGORIZE_TRANSACTION",
                    target_id=txn_id,
                    user_email=user_email,
                    status="FAILED",
                    new_value=cat_name,
                    signature_valid=True,
                    details=err,
                )
                return respond(f"⚠️ Failed to update transaction #{txn_id}: {err}")

        elif action_name == "cancel_recategorize":
            txn_id = action_params.get("transaction_id", "")
            log_mutation_audit(
                action_type="RECATEGORIZE_TRANSACTION",
                target_id=txn_id,
                user_email=user_email,
                status="CANCELLED",
                details="User clicked Cancel on confirmation card",
            )
            cancel_card = build_recategorization_cancelled_card(txn_id)
            if msg_name:
                patch_chat_card(msg_name, [cancel_card], text="🚫 Recategorization cancelled.")
            return respond(f"🚫 Recategorization for transaction #{txn_id} was cancelled. No changes were made.")

        elif action_name == "confirm_batch_recategorize":
            batch_id = action_params.get("batch_id", "")
            cat_id = action_params.get("category_id", "")
            cat_name = action_params.get("category_name", "Updated Category")
            merchant_name = action_params.get("merchant_name", "Merchant")
            count_str = action_params.get("count", "0")
            amt_str = action_params.get("total_amount", "0.0")
            ts_str = action_params.get("timestamp", "0")
            target_user = action_params.get("user_email", "unknown")
            sig = action_params.get("signature", "")

            try:
                count = int(count_str)
            except ValueError:
                count = 0
            try:
                total_amount = float(amt_str)
            except ValueError:
                total_amount = 0.0

            # 1. Rate Limiting Check (batch counts as 1 mutation rate token)
            allowed, rate_msg = check_mutation_rate_limit(user_email)
            if not allowed:
                logger.warning(f"Batch mutation rate limit exceeded for user '{user_email}' on batch #{batch_id}")
                log_mutation_audit(
                    action_type="BATCH_RECATEGORIZE",
                    target_id=batch_id,
                    user_email=user_email,
                    status="RATE_LIMITED",
                    new_value=cat_name,
                    details=rate_msg,
                )
                return respond(f"⛔ {rate_msg}")

            # 2. Addressee Validation
            if target_user and target_user != "unknown" and user_email.lower() != target_user.lower():
                logger.warning(f"Batch mutation rejected: user {user_email} attempted to confirm batch assigned to {target_user}")
                return respond(f"⛔ Only {target_user} can confirm this batch recategorization.")

            # 3. Timestamp Validation
            try:
                ts = int(ts_str)
            except ValueError:
                log_mutation_audit(
                    action_type="BATCH_RECATEGORIZE",
                    target_id=batch_id,
                    user_email=user_email,
                    status="REJECTED",
                    new_value=cat_name,
                    signature_valid=False,
                    details="Invalid timestamp in batch confirmation card",
                )
                return respond("⛔ Invalid timestamp in confirmation card.")

            # 4. Cryptographic Signature Validation
            is_valid, reason = verify_batch_signature(batch_id, cat_id, count, target_user, ts, sig)
            if not is_valid:
                logger.warning(f"Batch signature rejected: {reason} (batch={batch_id}, user={user_email})")
                log_mutation_audit(
                    action_type="BATCH_RECATEGORIZE",
                    target_id=batch_id,
                    user_email=user_email,
                    status="REJECTED",
                    new_value=cat_name,
                    signature_valid=False,
                    details=reason,
                )
                return respond(f"⛔ Batch confirmation rejected: {reason}")

            # 5. Execute Guarded Batch Mutation
            batch_result = await execute_guarded_batch_recategorization(
                batch_id=batch_id,
                user_email=user_email,
            )

            if batch_result.get("success"):
                confirmed_cnt = batch_result.get("confirmed_count", count)
                failed_cnt = batch_result.get("failed_count", 0)

                # Query next high-confidence candidate to proactively suggest
                next_rec = await find_next_recategorization_recommendation_async(exclude_merchant=merchant_name)
                success_card = build_batch_recategorization_success_card(
                    batch_id=batch_id,
                    merchant_name=merchant_name,
                    category_name=cat_name,
                    confirmed_count=confirmed_cnt,
                    failed_count=failed_cnt,
                    total_amount=total_amount,
                    next_recommendation=next_rec,
                )
                patch_text = f"✅ Batch recategorization completed: {confirmed_cnt} transactions reclassified."

                rec_text = ""
                if next_rec:
                    rec_m = next_rec["merchant"]
                    rec_c = next_rec["count"]
                    rec_a = next_rec["total_amount"]
                    rec_t = next_rec["target_category"]
                    rec_cur = next_rec["current_category"]
                    rec_text = (
                        f"\n\n💡 *Next Recommended Batch:*\n"
                        f"We found *{rec_c}* transactions for *{rec_m}* (${rec_a:,.2f}) currently filed under _{rec_cur}_ that belong in *{rec_t}*.\n"
                        f"👉 Reply *\"Fix {rec_m}\"* to review and confirm this batch next!"
                    )

                resp_msg = (
                    f"✅ Successfully reclassified *{confirmed_cnt}* transactions for *{merchant_name}* "
                    f"(${total_amount:,.2f}) to *{cat_name}* in Monarch Money and BigQuery.{rec_text}"
                )

                if msg_name:
                    patch_chat_card(msg_name, [success_card], text=patch_text)
                    return respond(resp_msg)
                return respond(resp_msg, cards_v2=[success_card])
            else:
                err = batch_result.get("error", "Unknown batch execution error")
                return respond(f"⚠️ Failed to execute batch recategorization for *{merchant_name}*: {err}")

        elif action_name == "cancel_batch_recategorize":
            batch_id = action_params.get("batch_id", "")
            merchant_name = action_params.get("merchant_name", "Merchant")
            await mark_batch_status_async(batch_id, "CANCELLED")
            log_mutation_audit(
                action_type="BATCH_RECATEGORIZE",
                target_id=batch_id,
                user_email=user_email,
                status="CANCELLED",
                details=f"User clicked Cancel on batch proposal for {merchant_name}",
            )
            cancel_card = build_batch_recategorization_cancelled_card(batch_id, merchant_name)
            if msg_name:
                patch_chat_card(msg_name, [cancel_card], text=f"🚫 Batch recategorization for {merchant_name} was cancelled.")
            return respond(f"🚫 Batch recategorization proposal for *{merchant_name}* was cancelled. No changes were made.")

        elif action_name == "snooze_alert":
            alert_key = action_params.get("alert_key", "")
            alert_type = action_params.get("alert_type", "GENERAL")
            days_str = action_params.get("days", "7")
            ts_str = action_params.get("ts", "")
            sig = action_params.get("sig", "")
            try:
                days = int(days_str)
            except ValueError:
                days = 7
            # Clamp days strictly between 1 and 90
            days = max(1, min(days, 90))

            # Cryptographically verify snooze action if signature parameters are provided
            sig_valid = None
            if sig and ts_str:
                try:
                    ts = int(ts_str)
                    from app.monarch_service import verify_snooze_signature

                    is_valid, reason = verify_snooze_signature(alert_key, days, ts, sig)
                    sig_valid = is_valid
                    if not is_valid:
                        logger.warning(f"Snooze signature rejected: {reason} (key={alert_key})")
                        log_mutation_audit(
                            action_type="SNOOZE_ALERT",
                            target_id=alert_key,
                            user_email=user_email,
                            status="REJECTED",
                            signature_valid=False,
                            details=reason,
                        )
                        return respond(f"⛔ Snooze rejected: {reason}")
                except (ValueError, TypeError):
                    log_mutation_audit(
                        action_type="SNOOZE_ALERT",
                        target_id=alert_key,
                        user_email=user_email,
                        status="REJECTED",
                        signature_valid=False,
                        details="Invalid timestamp in snooze action",
                    )
                    return respond("⛔ Invalid timestamp in snooze action.")

            logger.info(
                f"Snoozing alert from Card v2: key={alert_key}, type={alert_type}, days={days}, user={user_email}"
            )
            target_project = BQ_PROJECT_ID
            target_dataset = BQ_DATASET_ID

            bq_client = get_bq_client(target_project) if "get_bq_client" in globals() else None
            if not bq_client and bigquery:
                bq_client = bigquery.Client(project=target_project)

            suppressed = await asyncio.to_thread(
                suppress_alert,
                bq_client,
                target_project,
                target_dataset,
                alert_key,
                alert_type,
                days,
                f"Snoozed by user {user_email} via Google Chat Card v2",
            )

            if suppressed:
                log_mutation_audit(
                    action_type="SNOOZE_ALERT",
                    target_id=alert_key,
                    user_email=user_email,
                    status="SUCCESS",
                    signature_valid=sig_valid,
                    details=f"Snoozed for {days} days",
                )
                snooze_card = build_snooze_success_card(alert_key, alert_type, days)
                success_text = (
                    f"💤 Alert *{alert_type.replace('_', ' ').title()}* (`{alert_key}`) snoozed for {days} days."
                )
                return respond(success_text, cards_v2=[snooze_card])
            else:
                log_mutation_audit(
                    action_type="SNOOZE_ALERT",
                    target_id=alert_key,
                    user_email=user_email,
                    status="FAILED",
                    signature_valid=sig_valid,
                    details="BigQuery suppression insert failed",
                )
                return respond(f"⚠️ Failed to snooze alert '{alert_key}'.")

        return respond("ℹ️ Action received.")

    # 1. Bot added to space or 1:1 DM
    if event_type == "ADDED_TO_SPACE":
        welcome_text = (
            "👋 I'm *FinSage*, your personal family finance advisor!\n\n"
            "I am connected directly to *Monarch Money* and *BigQuery* to help optimize family spend and accelerate debt freedom.\n\n"
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
            content_name = att.get("contentName", "")
            if content_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".pdf")):
                img_tuple = download_chat_attachment(att)
                if img_tuple:
                    downloaded_images.append(img_tuple)

    # 3. Extract text and strip @mention anywhere (beginning, middle, or end)
    raw_text = message.get("argumentText") or message.get("text") or ""
    clean_text = re.sub(r"@(FinSage|Sage|Family\s*Finance\s*Copilot)", "", raw_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"@\S+", "", clean_text).strip()

    if not clean_text and downloaded_images:
        clean_text = "Please carefully examine the attached financial screenshot/document. Parse all numbers, projections, and line items, and provide a strategic financial analysis and recommendations."

    if not clean_text or clean_text.lower() in ("help", "/help"):
        help_text = (
            f"Hi {sender_name}! Here are some questions you can ask me:\n"
            "• _What is our daily debt interest burden across mortgage and HELOC?_\n"
            "• _What subscriptions had price increases recently?_\n"
            "• _How much did we spend on dining out last month?_\n"
            "• _What frequent small purchases are we making?_\n"
            "• `/sync` to pull latest transactions\n"
            "• `/alerts` to run proactive spend scan\n"
            "• `/digest` for weekly or monthly executive CFO brief\n"
            "• `/sweep` to calculate safe surplus cash to pay down variable debt (HELOC)\n"
            "• `/tax` to review annual tax-deductible expense summaries\n"
            "• *You can also paste receipts, invoices, or financial documents!*"
        )
        return respond(help_text)

    # Instant greeting responses
    greeting_patterns = ("hello", "hi", "hey", "are you there", "you there", "ping")
    if clean_text.lower().rstrip("?!. ") in greeting_patterns:
        greet_text = (
            f"👋 Yes {sender_name}, I'm here! I'm connected to your Monarch Money and BigQuery financial database.\n\n"
            'Ask me any question about your spending, subscriptions, or debt carry across mortgage and HELOC (e.g. *"What is our daily debt interest cost?"*).'
        )
        return respond(greet_text)

    # Command: /sync or natural sync intent
    lower_text = clean_text.lower()
    is_sync_intent = lower_text.startswith(("/sync", "sync", "/refresh", "refresh", "/backfill", "backfill")) or any(
        phrase in lower_text
        for phrase in [
            "please sync",
            "can you sync",
            "trigger sync",
            "run sync",
            "sync now",
            "sync data",
            "sync monarch",
            "refresh data",
            "pull history",
            "sync history",
            "backfill history",
        ]
    )
    if is_sync_intent:
        if any(term in lower_text for term in ["all", "everything", "history", "full", "backfill"]):
            days = None
            history_desc = "all available history"
        else:
            days_match = re.search(r"\b(\d+)\b", lower_text)
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

    # Command: /brief, /alerts
    is_brief_or_alerts_intent = clean_text.lower().startswith(("/brief", "brief", "/alerts", "alerts")) or any(
        phrase in lower_text for phrase in ["morning brief", "daily brief", "daily synopsis", "morning synopsis"]
    )
    if is_brief_or_alerts_intent:
        try:
            scan_res = await execute_alert_scan(user_email=user_email)
            alerts_list = scan_res.get("alerts", [])
            synopsis = scan_res.get("brief_synopsis")
            active_alerts = [a for a in alerts_list if a.get("type") != "QUERY_ERROR"]
            if any(term in lower_text for term in ["brief", "synopsis"]):
                card_payload = build_chat_card_v2(active_alerts, synopsis=synopsis)
                return respond(
                    card_payload.get("text", "🌅 *FinSage Morning Financial Synopsis*"),
                    cards_v2=card_payload.get("cardsV2"),
                )
            else:
                if not active_alerts:
                    return respond("✅ No active financial anomalies or spending leaks detected right now!")
                card_payload = build_chat_card_v2(active_alerts)
                return respond(
                    card_payload.get("text", "🔔 *FinSage*: Alerts Scan completed."), cards_v2=card_payload.get("cardsV2")
                )
        except Exception as e:
            return respond(f"⚠️ Brief / Alert scan failed: {e}")

    # Command: /digest [weekly|monthly] or natural digest intent
    is_digest_intent = lower_text.startswith(("/digest", "digest")) or any(
        phrase in lower_text
        for phrase in [
            "weekly digest",
            "monthly digest",
            "executive digest",
            "cfo digest",
            "cfo brief",
            "cfo report",
            "monthly recap",
            "weekly recap",
            "spending recap",
            "executive recap",
            "executive summary",
        ]
    )
    if is_digest_intent:
        period = "MONTHLY" if any(w in lower_text for w in ["month", "monthly"]) else "WEEKLY"
        try:
            target_project = BQ_PROJECT_ID
            target_dataset = BQ_DATASET_ID
            bq = get_bq_client(target_project)
            digest_data = await asyncio.to_thread(generate_executive_digest, bq, target_project, target_dataset, period)
            card_payload = build_executive_digest_card(digest_data)
            return respond(
                card_payload.get("text", f"📊 *FinSage*: {period.title()} Executive CFO Digest"),
                cards_v2=card_payload.get("cardsV2"),
            )
        except Exception as e:
            return respond(f"⚠️ Failed to generate {period.lower()} digest: {e}")

    # Command: /sweep or natural surplus sweep intent
    is_sweep_intent = lower_text.startswith(("/sweep", "sweep")) or any(
        phrase in lower_text
        for phrase in [
            "surplus sweep",
            "paycheck sweep",
            "safe to sweep",
            "safe surplus",
            "can i sweep",
            "sweep money",
            "sweep cash",
            "heloc sweep",
            "debt sweep",
        ]
    )
    if is_sweep_intent:
        try:
            target_project = BQ_PROJECT_ID
            target_dataset = BQ_DATASET_ID
            bq = get_bq_client(target_project)
            analysis_text = await asyncio.to_thread(get_paycheck_surplus_analysis, bq, target_project, target_dataset)
            return respond(analysis_text)
        except Exception as e:
            return respond(f"⚠️ Failed to evaluate paycheck surplus sweep: {e}")

    # Command: /receipt, receipt image drop, or receipt extraction intent
    is_receipt_intent = lower_text.startswith(("/receipt", "receipt", "/invoice")) or any(
        phrase in lower_text
        for phrase in [
            "process receipt",
            "extract receipt",
            "parse receipt",
            "scan receipt",
            "read receipt",
            "receipt deduction",
            "upload receipt",
        ]
    )
    if downloaded_images and is_receipt_intent:
        try:
            img_bytes, mime = downloaded_images[0]
            bq = get_bq_client()
            res = await asyncio.to_thread(
                process_receipt_bytes,
                img_bytes,
                mime_type=mime,
                user_email=user_email,
                bq=bq,
            )
            chat_card = res.get("chat_card", {})
            ext = res.get("extraction", {})
            m_name = ext.get("merchant_name", "Receipt")
            tot = ext.get("total_amount", 0.0)
            ded = ext.get("deductible_amount", 0.0)
            is_ded = ext.get("is_tax_deductible", False)
            summary_msg = f"🧾 *Processed Receipt*: {m_name} (${tot:,.2f})"
            if is_ded:
                summary_msg += f" • 🟢 ${ded:,.2f} Deductible"
            return respond(summary_msg, cards_v2=[chat_card.get("card", {})] if "card" in chat_card else None)
        except Exception as e:
            logger.error(f"Error processing receipt attachment: {e}")
            return respond(f"⚠️ Failed to process receipt attachment: {e}")

    # Command: /tax, /deductions, or tax deductibility intent
    is_tax_intent = lower_text.startswith(("/tax", "/deduct")) or any(
        phrase in lower_text
        for phrase in [
            "tax deductions",
            "tax summary",
            "tax write-offs",
            "my deductions",
            "deductible expenses",
            "hsa expenses",
            "schedule c expenses",
            "charitable deductions",
        ]
    )
    if is_tax_intent:
        year_match = re.search(r"\b(20\d{2})\b", lower_text)
        target_year = int(year_match.group(1)) if year_match else None
        try:
            summary_text = await asyncio.to_thread(get_tax_deduction_analysis, target_year)
            return respond(summary_text)
        except Exception as e:
            return respond(f"⚠️ Failed to retrieve tax deduction summary: {e}")

    # Natural language query -> Conversational Analytics Agent
    # If the response completes within 20s, return synchronously.
    # If it takes longer (deep multi-table BigQuery scans), acknowledge synchronously and post the full result into the thread via background task.
    session_history = get_session_history(thread_name, space_name)
    logger.info(
        f"Querying Gemini Brain with {len(session_history)} prior turns and {len(downloaded_images)} images (thread={thread_name}, space={space_name})"
    )

    CURRENT_USER_EMAIL.set(user_email)
    CURRENT_PROPOSED_CARD.set(None)

    loop = asyncio.get_event_loop()
    analysis_future = loop.run_in_executor(
        None, ask_gemini_brain, clean_text, session_history, downloaded_images, user_email
    )

    try:
        result = await asyncio.wait_for(asyncio.shield(analysis_future), timeout=12.0)
        answer = result.get("answer", "No response generated.")
        sql = result.get("sql")
        suggestions = result.get("suggestions", [])
        proposed_card = result.get("card")
        cards_list = [proposed_card] if proposed_card else None

        if "no specific response was generated" not in answer.lower():
            save_session_history(thread_name, space_name, user_email, clean_text, answer)

        return respond(format_advisory_reply(answer, sql, suggestions), cards_v2=cards_list)
    except TimeoutError:
        logger.info(
            f"Query '{clean_text}' exceeded 12s budget; continuing in background to post to {space_name} (thread={thread_name})"
        )

        async def complete_and_post():
            try:
                res = await analysis_future
                ans = res.get("answer", "No response generated.")
                s = res.get("sql")
                suggs = res.get("suggestions", [])
                p_card = res.get("card")
                p_cards_list = [p_card] if p_card else None

                if "no specific response was generated" not in ans.lower():
                    save_session_history(thread_name, space_name, user_email, clean_text, ans)

                post_to_chat_thread(
                    format_advisory_reply(ans, s, suggs), thread_name, space_name, cards_v2=p_cards_list
                )
            except Exception as ex:
                logger.error(f"Background query post failed: {ex}")
                post_to_chat_thread(f"⚠️ Query processing failed: {ex}", thread_name, space_name)

        asyncio.create_task(complete_and_post())
        return respond(
            "⏳ *Analyzing...* Deep scan in progress across your BigQuery models. Posting full recommendation to this thread in just a few moments!"
        )
