"""
Memory Service for Sage Financial Intelligence Hub.
Provides persistent, user-scoped memory integration using Google Cloud Vertex AI
Agent Platform Memory Bank (Reasoning Engine memory service).

Replaces fragile BigQuery chat_history OLAP logging with semantic memory consolidation.
"""

import hashlib
import logging
import os
import re
from datetime import UTC

import google.auth
from google.auth.transport.requests import Request

from app.monarch_service import CURRENT_USER_EMAIL

logger = logging.getLogger("memory_service")

DEFAULT_MEMORY_BANK_NAME = os.environ.get(
    "VERTEX_MEMORY_BANK_NAME",
    "projects/PROJECT_ID/locations/us-central1/reasoningEngines/ENGINE_ID",
)
DEFAULT_USER_EMAIL = os.environ.get("DEFAULT_USER_EMAIL", "user@example.com")
GCP_REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", os.environ.get("GCP_REGION", "us-central1"))
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", os.environ.get("BQ_PROJECT_ID", "family-finance-hub"))

MAX_PREFERENCE_LENGTH = 500
FORBIDDEN_PREFERENCE_PATTERN = re.compile(
    r"(?i)\b(ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior)?\s*(instructions|prompts|rules)|system\s+prompt|you\s+are\s+now|override\s+instructions|bypass\s+rules|drop\s+table"
)


def validate_user_preference(preference: str) -> tuple[bool, str]:
    """Validates user preference string against length limits and instruction subversion patterns."""
    if not preference or not preference.strip():
        return False, "Preference cannot be empty."
    clean_text = preference.strip()
    if len(clean_text) > MAX_PREFERENCE_LENGTH:
        return (
            False,
            f"Preference length ({len(clean_text)}) exceeds the maximum allowed length of {MAX_PREFERENCE_LENGTH} characters.",
        )
    if FORBIDDEN_PREFERENCE_PATTERN.search(clean_text):
        return (
            False,
            "Preference rejected: Contains forbidden instruction override or prompt manipulation patterns.",
        )
    return True, "Valid"


_cached_client = None


def _resolve_user_email(user_email: str | None = None) -> str:
    """Resolves target user email with fallback to context variable and default."""
    if user_email and user_email.strip() and user_email.strip() != "unknown":
        return user_email.strip()
    ctx_user = CURRENT_USER_EMAIL.get()
    if ctx_user and ctx_user.strip() and ctx_user.strip() != "unknown":
        return ctx_user.strip()
    return DEFAULT_USER_EMAIL


def get_memory_client():
    """Initializes and caches the Agent Platform client with explicit reasoning engine scope."""
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    try:
        import agentplatform

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        _cached_client = agentplatform.Client(
            project=GCP_PROJECT,
            location=GCP_REGION,
            credentials=creds,
        )
        return _cached_client
    except Exception as e:
        logger.debug(f"Agent Platform Reasoning Engine client unavailable: {e}")
        return None


def _call_memory_bank_api(endpoint_action: str, payload: dict) -> dict | None:
    """
    Direct HTTPS REST call to Vertex AI Reasoning Engine Memory Bank.
    endpoint_action is e.g. ':retrieve' or ':generate'.
    Uses Application Default Credentials with automatic token refresh.
    """
    bank_name = get_memory_bank_name()
    if not bank_name or "PROJECT_ID" in bank_name or "ENGINE_ID" in bank_name:
        return None

    try:
        import requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(Request())

        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
        url = f"https://{GCP_REGION}-aiplatform.googleapis.com/v1beta1/{bank_name}/memories{endpoint_action}"
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Memory Bank API {endpoint_action} returned status {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"Memory Bank API call failed ({endpoint_action}): {e}")

    return None


def get_memory_bank_name(client=None) -> str | None:
    """Discovers the active Sage Memory Bank resource path or returns configured default."""
    if DEFAULT_MEMORY_BANK_NAME:
        return DEFAULT_MEMORY_BANK_NAME

    cli = client or get_memory_client()
    if not cli or not hasattr(cli, "memory_banks"):
        return None

    try:
        banks = list(cli.memory_banks.list())
        for bank in banks:
            if getattr(bank, "display_name", "") == "Sage Memory Bank":
                return bank.name
        if banks:
            return banks[0].name
    except Exception as e:
        logger.debug(f"Could not list memory banks: {e}")

    return None


def retrieve_user_memories(user_email: str | None = None, client=None) -> list[str]:
    """
    Retrieves consolidated long-term memories and preferences scoped to the user's email.
    Queries persistent BigQuery user_preferences table, combined with Vertex AI Memory Bank if active.
    Returns a list of extracted fact strings.
    """
    target_user = _resolve_user_email(user_email)
    facts: list[str] = []

    # 1. Primary: Retrieve stored user preferences from BigQuery
    try:
        from google.cloud import bigquery

        from app.bq_service import get_bq_client, get_target_dataset, get_target_project

        target_project = get_target_project()
        target_dataset = get_target_dataset()
        bq = get_bq_client(target_project)
        query = f"""
            SELECT preference_text
            FROM `{target_project}.{target_dataset}.user_preferences`
            WHERE user_email = @email
            ORDER BY created_at ASC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("email", "STRING", target_user)
            ]
        )
        rows = list(bq.query(query, job_config=job_config).result())
        for r in rows:
            p_text = getattr(r, "preference_text", None)
            if p_text and p_text.strip() and p_text.strip() not in facts:
                facts.append(p_text.strip())
    except Exception as e:
        logger.debug(f"Could not retrieve user preferences from BigQuery: {e}")

    # 2. Vertex AI Memory Bank
    bank_name = get_memory_bank_name(client)
    if client is not None:
        if hasattr(client, "memory_banks"):
            try:
                response = client.memory_banks.memories.retrieve(
                    name=bank_name,
                    scope={"user_id": target_user},
                )
                for item in getattr(response, "page", []):
                    mem = getattr(item, "memory", None)
                    fact = getattr(mem, "fact", None) if mem else None
                    if fact and fact.strip() and fact.strip() not in facts:
                        facts.append(fact.strip())
                logger.info(f"Retrieved memories for user '{target_user}' from mock Memory Bank.")
            except Exception as e:
                logger.debug(f"Mock Memory Bank retrieve failed: {e}")
    elif bank_name and "PROJECT_ID" not in bank_name:
        resp_data = _call_memory_bank_api(":retrieve", {"scope": {"user_id": target_user}})
        if resp_data:
            for item in resp_data.get("retrievedMemories", []):
                mem = item.get("memory", {})
                fact = mem.get("fact")
                if fact and fact.strip() and fact.strip() not in facts:
                    facts.append(fact.strip())
            logger.info(f"Retrieved {len(facts)} memories for user '{target_user}' from Vertex Memory Bank REST API.")

    return facts


def format_memories_for_prompt(memories: list[str]) -> str:
    """
    Formats a list of memory facts into a clean system instruction block for Gemini.
    """
    if not memories:
        return ""

    facts_list = "\n".join(f"- {fact}" for fact in memories)
    return (
        "\n\nUSER FINANCIAL PROFILE & LONG-TERM MEMORY (VERTEX AI MEMORY BANK):\n"
        "<USER_PREFERENCES_AND_MEMORY>\n"
        "The following persistent preferences, financial goals, discretionary ceilings, "
        "and debt paydown strategies are stored in Memory Bank for this user:\n"
        f"{facts_list}\n"
        "Guidance: Always adhere to these active user constraints and goals when providing "
        "budget analyses, evaluating transaction efficiency, or recommending debt acceleration strategies.\n"
        "</USER_PREFERENCES_AND_MEMORY>"
    )


def save_user_preference(
    preference_or_rule: str,
    user_email: str | None = None,
    client=None,
) -> bool:
    """
    Consolidates a new user preference or financial rule into the user's Memory Bank scope.
    Memory Bank handles deduplication, semantic updates, and revision tracking automatically.
    """
    target_user = _resolve_user_email(user_email)
    clean_fact = preference_or_rule.strip() if preference_or_rule else ""
    pref_id = hashlib.sha256(clean_fact.encode()).hexdigest()[:16] if clean_fact else "empty"

    is_valid, validation_msg = validate_user_preference(preference_or_rule)
    if not is_valid:
        logger.warning(f"User preference validation failed for '{target_user}': {validation_msg}")
        try:
            from app.monarch_service import log_mutation_audit

            log_mutation_audit(
                action_type="STORE_PREFERENCE",
                target_id=pref_id,
                user_email=target_user,
                status="REJECTED",
                new_value=preference_or_rule[:100] if preference_or_rule else None,
                details=validation_msg,
            )
        except Exception as audit_err:
            logger.debug(f"Audit log non-fatal error: {audit_err}")
        return False

    bq_saved = False
    try:
        from datetime import datetime

        from app.bq_service import get_bq_client, get_target_dataset, get_target_project

        target_project = get_target_project()
        target_dataset = get_target_dataset()
        bq = get_bq_client(target_project)
        table_ref = f"{target_project}.{target_dataset}.user_preferences"
        errors = bq.insert_rows_json(
            table_ref,
            [
                {
                    "preference_id": pref_id,
                    "user_email": target_user,
                    "preference_text": clean_fact,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ],
        )
        if not errors:
            bq_saved = True
        else:
            logger.warning(f"BigQuery user preferences insert error: {errors}")
    except Exception as bq_err:
        logger.debug(f"Could not persist user preference to BigQuery: {bq_err}")

    # Also persist to Vertex AI Memory Bank if available
    mb_saved = False
    bank_name = get_memory_bank_name(client)
    if client is not None:
        if hasattr(client, "memory_banks"):
            try:
                client.memory_banks.memories.generate(
                    name=bank_name,
                    direct_memories_source={"direct_memories": [{"fact": clean_fact}]},
                    scope={"user_id": target_user},
                )
                mb_saved = True
                logger.info(f"Successfully consolidated preference into Memory Bank for '{target_user}': {clean_fact}")
            except Exception as e:
                logger.error(f"Failed to generate memory in Memory Bank for '{target_user}': {e}")
                if client is not None:
                    try:
                        from app.monarch_service import log_mutation_audit

                        log_mutation_audit(
                            action_type="STORE_PREFERENCE",
                            target_id=pref_id,
                            user_email=target_user,
                            status="FAILED",
                            new_value=clean_fact,
                            details=str(e),
                        )
                    except Exception as audit_err:
                        logger.debug(f"Audit log non-fatal error: {audit_err}")
                    return False
    elif bank_name and "PROJECT_ID" not in bank_name:
        try:
            gen_payload = {
                "direct_memories_source": {"direct_memories": [{"fact": clean_fact}]},
                "scope": {"user_id": target_user},
            }
            resp_data = _call_memory_bank_api(":generate", gen_payload)
            if resp_data is not None:
                mb_saved = True
                logger.info(f"Successfully consolidated preference into Vertex Memory Bank REST API for '{target_user}': {clean_fact}")
        except Exception as e:
            logger.error(f"Failed to generate memory in Memory Bank REST API for '{target_user}': {e}")

    success = bq_saved or mb_saved
    if client is not None and not mb_saved:
        success = False

    status = "SUCCESS" if success else "FAILED"
    try:
        from app.monarch_service import log_mutation_audit

        log_mutation_audit(
            action_type="STORE_PREFERENCE",
            target_id=pref_id,
            user_email=target_user,
            status=status,
            new_value=clean_fact,
            details="Consolidated into Memory Bank and BigQuery" if success else "Failed to persist",
        )
    except Exception as audit_err:
        logger.debug(f"Audit log non-fatal error: {audit_err}")

    return success


def store_user_preference(preference_or_rule: str) -> str:
    """
    Persists an explicit financial preference, budget ceiling, debt acceleration target,
    or standing rule into the user's long-term Memory Bank for cross-session continuity.

    Call this tool whenever the user instructs Sage to remember a goal, sets a budget cap,
    establishes a payoff deadline, or defines a spending rule (e.g. 'Remember that we want to cap
    dining at $400/month', 'Our target is to pay off our loan early', 'Ignore coffee transactions <$10').

    Args:
        preference_or_rule: The concise rule, goal, or preference statement to store.
    """
    is_valid, validation_msg = validate_user_preference(preference_or_rule)
    if not is_valid:
        return f"Refused: {validation_msg}"

    user_email = _resolve_user_email()
    success = save_user_preference(preference_or_rule, user_email=user_email)
    if success:
        return f"Successfully saved to your long-term Memory Bank for {user_email}: '{preference_or_rule.strip()}'."
    else:
        return f"Could not record preference into Memory Bank due to a transient service error. The rule was: '{preference_or_rule.strip()}'."
