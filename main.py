import asyncio
import base64
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
from monarch_service import (
    execute_sync,
    get_live_account_balance,
    get_live_transaction,
    get_monarch_client,
    request_plaid_refresh,
)
import requests
import yaml

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

from config import (
    BQ_PROJECT_ID,
    BQ_DATASET_ID,
    IS_PROD,
    resolve_secret,
    load_local_config,
)
from contextlib import asynccontextmanager
from alerts import execute_alert_scan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monarch-gemini")

is_prod = IS_PROD


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = None
    should_run_worker = (
        os.getenv("ENABLE_CHAT_PULL_WORKER", "false").lower() in ("true", "1", "yes")
        or (os.getenv("K_SERVICE") and os.getenv("DISABLE_CHAT_PULL_WORKER", "false").lower() not in ("true", "1", "yes"))
    )
    if should_run_worker:
        try:
            from chat_worker import start_chat_worker_background
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

# execute_sync is imported from monarch_service.py




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
            "10. Multimodal Understanding: When the user provides images, screenshots, paystubs, statements, or compensation/outlook plans, thoroughly examine the visual data, parse every figure and projection, and integrate them directly into your financial analysis and debt paydown calculations.\n"
            "11. Live Monarch Confirmation & Plaid Tools: BigQuery is your primary historical analytical engine. If the user asks for up-to-the-minute balance checks (e.g. 'what is my balance right now?', 'did that payment post?'), call `get_live_account_balance(account_identifier)` to confirm live figures directly from Monarch. To inspect a specific transaction's pending status, call `get_live_transaction(transaction_id)`. If an institution's data appears stale, call `request_plaid_refresh(institution_name)`."
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
                tools=[
                    run_readonly_sql,
                    get_live_account_balance,
                    get_live_transaction,
                    request_plaid_refresh,
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
            claims = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                audience=chat_audience if chat_audience else None,
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
            raise HTTPException(status_code=401, detail=f"Invalid Google Chat authentication token: {e}")

    # 5. Block all unauthenticated requests
    raise HTTPException(
        status_code=401,
        detail="Unauthorized: Missing valid Google Chat Bearer token, verification secret, or API key."
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
    message = (
        message_payload.get("message")
        or app_command_payload.get("message")
        or chat_obj.get("message")
        or raw_payload.get("message")
        or {}
    )
    event_type = (
        raw_payload.get("type")
        or ("SLASH_COMMAND" if app_command_payload else None)
        or ("ADDED_TO_SPACE" if "addedToSpacePayload" in chat_obj else None)
        or ("MESSAGE" if message else "UNKNOWN")
    )

    # Extract user info
    user_info = (
        chat_obj.get("user")
        or raw_payload.get("user")
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
        or raw_payload.get("space")
        or {}
    )
    space_name = space_obj.get("name") if isinstance(space_obj, dict) else (space_obj if isinstance(space_obj, str) else None)

    # Parse thread.name robustly across all Google Chat event shapes
    thread_obj = (
        message.get("thread")
        or message_payload.get("thread")
        or app_command_payload.get("thread")
        or chat_obj.get("thread")
        or raw_payload.get("thread")
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
        if is_pubsub:
            post_to_chat_thread(text, thread_name=thread_name, space_name=space_name)
            return {"status": "ok"}
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
            "👋 I'm *Sage*, your personal family finance advisor!\n\n"
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
            if content_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                img_tuple = download_chat_attachment(att)
                if img_tuple:
                    downloaded_images.append(img_tuple)

    # 3. Extract text and strip @mention anywhere (beginning, middle, or end)
    raw_text = message.get("argumentText") or message.get("text") or ""
    clean_text = re.sub(r"@(Sage|Family\s*Finance\s*Copilot)", "", raw_text, flags=re.IGNORECASE)
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


