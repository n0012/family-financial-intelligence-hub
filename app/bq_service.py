"""
BigQuery Service Module for Sage (Family Financial Intelligence Hub)
Provides safe, read-only SQL execution, Conversational Analytics fallback,
session history persistence & hydration, and schema migration utilities.
"""

import json
import logging
import os
import re
from datetime import UTC

import google.auth
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import bigquery

logger = logging.getLogger("monarch-gemini.bq")

FORBIDDEN_SQL_PATTERN = r"\b(insert|update|delete|drop|truncate|alter|create|merge|grant|revoke|export\s+data|call|execute\s+immediate|declare)\b"

_bq_client: bigquery.Client | None = None

# In-memory caches for fast multi-turn responses within container lifetime
THREAD_HISTORY: dict[str, list[dict]] = {}
SPACE_HISTORY: dict[str, list[dict]] = {}


def get_target_project(project_id: str | None = None) -> str:
    """Resolves active Google Cloud Project ID."""
    return (
        project_id
        or os.getenv("BQ_PROJECT_ID")
        or os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or "family-finance-hub"
    )


def get_target_dataset(dataset_id: str | None = None) -> str:
    """Resolves active BigQuery dataset ID."""
    return dataset_id or os.getenv("BQ_DATASET_ID") or "family_finance"


def get_bq_client(project_id: str | None = None) -> bigquery.Client:
    """
    Returns a cached BigQuery client singleton for the target project.
    """
    global _bq_client
    target_project = get_target_project(project_id)
    if _bq_client is None:
        _bq_client = bigquery.Client(project=target_project)
    return _bq_client


def clear_session_history() -> None:
    """Clears in-memory session history caches (useful for unit test isolation)."""
    THREAD_HISTORY.clear()
    SPACE_HISTORY.clear()


def run_readonly_sql(
    sql_query: str,
    project_id: str | None = None,
    client: bigquery.Client | None = None,
    max_bytes_billed: int = 100_000_000,
    max_results: int = 50,
) -> str:
    """
    Executes a read-only GoogleSQL query against the family_finance BigQuery dataset
    (e.g. v_heloc_daily_cost, v_active_subscriptions, v_subscription_price_creep,
    v_subscription_overlap, v_utility_seasonal_baseline, v_food_efficiency,
    v_micro_transaction_leakage, raw_accounts, raw_transactions).

    Strictly enforces read-only access and caps query byte scans to prevent cost overruns.
    """
    match = re.search(FORBIDDEN_SQL_PATTERN, sql_query, re.IGNORECASE)
    if match:
        return f"Error: Only SELECT queries are permitted. Found forbidden keyword: {match.group(0)}"

    try:
        logger.info(f"Executing Gemini SQL tool call: {sql_query}")
        bq = client or get_bq_client(project_id)
        job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=max_bytes_billed  # Default 100 MB scan budget safety cap
        )
        query_job = bq.query(sql_query, job_config=job_config)
        rows = list(query_job.result(max_results=max_results))
        if not rows:
            return "No rows returned from query."
        dict_rows = [dict(r.items()) for r in rows]
        return json.dumps(dict_rows, default=str)
    except Exception as e:
        logger.error(f"BigQuery execution error for query '{sql_query}': {e}")
        return f"BigQuery execution error: {e}"


def ask_conversational_analytics(
    question: str,
    history: list | None = None,
    project_id: str | None = None,
) -> dict:
    """
    Sends natural language question to Gemini Conversational Analytics Agent
    grounded in family_finance BigQuery dataset, with optional multi-turn conversation history.
    """
    target_project = get_target_project(project_id)
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        token = creds.token

        ca_parent = f"projects/{target_project}/locations/global"
        url = f"https://geminidataanalytics.googleapis.com/v1beta/{ca_parent}:chat"

        messages_list = list(history or [])
        messages_list.append({"userMessage": {"text": question}})

        payload = {
            "parent": ca_parent,
            "messages": messages_list,
            "data_agent_context": {"data_agent": f"{ca_parent}/dataAgents/family-finance-advisor"},
        }

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "x-goog-user-project": target_project,
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
            "suggestions": suggestions,
        }
    except Exception as e:
        logger.error(f"Failed to query Conversational Analytics: {e}")
        return {
            "answer": f"Sorry, I had trouble processing your question: {e}",
            "sql": None,
            "suggestions": [],
        }


def get_session_history(
    thread_name: str | None,
    space_name: str | None,
    project_id: str | None = None,
    dataset_id: str | None = None,
    client: bigquery.Client | None = None,
    limit: int = 5,
) -> list[dict]:
    """
    Retrieves conversational turns prioritizing the specific thread, falling back to the space session,
    and hydrated from BigQuery if in-memory cache is empty (e.g. post-deployment/cold start).
    """
    if thread_name and THREAD_HISTORY.get(thread_name):
        return list(THREAD_HISTORY[thread_name])

    if space_name and SPACE_HISTORY.get(space_name):
        return list(SPACE_HISTORY[space_name])

    sessions = [s for s in [thread_name, space_name] if s]
    if not sessions:
        return []

    target_project = get_target_project(project_id)
    target_dataset = get_target_dataset(dataset_id)

    try:
        bq = client or get_bq_client(target_project)
        query = f"""
            SELECT user_text, model_response
            FROM `{target_project}.{target_dataset}.chat_history`
            WHERE session_id IN UNNEST(@sessions)
            ORDER BY created_at DESC
            LIMIT {limit}
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


def save_session_history(
    thread_name: str | None,
    space_name: str | None,
    user_email: str,
    user_text: str,
    model_text: str,
    project_id: str | None = None,
    dataset_id: str | None = None,
    client: bigquery.Client | None = None,
) -> bool:
    """
    Saves conversation turn to in-memory caches and persists to BigQuery chat_history.
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
    if not session_id:
        return False

    target_project = get_target_project(project_id)
    target_dataset = get_target_dataset(dataset_id)

    try:
        from datetime import datetime

        bq = client or get_bq_client(target_project)
        table_ref = f"{target_project}.{target_dataset}.chat_history"
        errors = bq.insert_rows_json(
            table_ref,
            [
                {
                    "session_id": session_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "user_email": user_email,
                    "user_text": user_text,
                    "model_response": model_text,
                }
            ],
        )
        if errors:
            logger.warning(f"BigQuery chat history insert errors: {errors}")
            return False
        return True
    except Exception as e:
        logger.warning(f"Failed to persist chat history to BigQuery: {e}")
        return False


def apply_bigquery_schema(
    schema_file_path: str | None = None,
    project_id: str | None = None,
    client: bigquery.Client | None = None,
) -> dict:
    """
    Applies DDL statements and analytical views from schema.sql to the target project dataset.
    """
    target_project = get_target_project(project_id)
    if schema_file_path:
        if not os.path.exists(schema_file_path):
            raise FileNotFoundError(f"Schema file not found at {schema_file_path}")
        path = schema_file_path
    else:
        candidate_paths = [
            os.path.join(os.path.dirname(__file__), "..", "schema.sql"),
            os.path.join(os.path.dirname(__file__), "schema.sql"),
            "schema.sql",
        ]
        path = next((p for p in candidate_paths if p and os.path.exists(p)), None)
        if not path:
            raise FileNotFoundError(f"Schema file not found at any candidate paths: {candidate_paths}")

    with open(path, encoding="utf-8") as f:
        sql_content = f.read()

    bq = client or get_bq_client(target_project)
    query_job = bq.query(sql_content)
    query_job.result()  # Wait for DDL execution

    return {
        "status": "success",
        "project": target_project,
        "schema_file": path,
    }
