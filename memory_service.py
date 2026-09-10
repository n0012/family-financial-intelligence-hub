"""
Memory Service for Sage Financial Intelligence Hub.
Provides persistent, user-scoped memory integration using Google Cloud Vertex AI
Agent Platform Memory Bank (Reasoning Engine memory service).

Replaces fragile BigQuery chat_history OLAP logging with semantic memory consolidation.
"""

import contextvars
import logging
import os
from typing import List, Optional

import google.auth
from google.auth.transport.requests import Request

from monarch_service import CURRENT_USER_EMAIL

logger = logging.getLogger("memory_service")

DEFAULT_MEMORY_BANK_NAME = os.environ.get(
    "VERTEX_MEMORY_BANK_NAME",
    "projects/475933066321/locations/us-central1/reasoningEngines/3539210831822585856",
)
DEFAULT_USER_EMAIL = os.environ.get("DEFAULT_USER_EMAIL", "nick@sagelycreations.com")
GCP_REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", os.environ.get("GCP_REGION", "us-central1"))
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", os.environ.get("BQ_PROJECT_ID", "sagely-family-finance"))


_cached_client = None


def _resolve_user_email(user_email: Optional[str] = None) -> str:
    """Resolves target user email with fallback to context variable and default."""
    if user_email and user_email.strip() and user_email.strip() != "unknown":
        return user_email.strip()
    ctx_user = CURRENT_USER_EMAIL.get()
    if ctx_user and ctx_user.strip() and ctx_user.strip() != "unknown":
        return ctx_user.strip()
    return DEFAULT_USER_EMAIL


def get_memory_client(
    project_id: Optional[str] = None,
    location: Optional[str] = None,
):

    """
    Returns an authenticated agentplatform.Client configured with Application Default
    Credentials (ADC) to prevent API key conflicts with Vertex AI IAM endpoints.
    """
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    target_project = project_id or GCP_PROJECT
    target_location = location or GCP_REGION

    try:
        import agentplatform

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not creds.valid:
            creds.refresh(Request())

        _cached_client = agentplatform.Client(
            project=target_project,
            location=target_location,
            credentials=creds,
        )
        return _cached_client
    except Exception as e:
        logger.warning(f"Could not initialize Agent Platform client: {e}")
        return None


def get_memory_bank_name(client=None) -> Optional[str]:
    """
    Resolves the Memory Bank resource name from environment variable,
    or queries available memory banks in the project.
    """
    if DEFAULT_MEMORY_BANK_NAME:
        return DEFAULT_MEMORY_BANK_NAME

    cli = client or get_memory_client()
    if not cli:
        return None

    try:
        banks = list(cli.memory_banks.list())
        for bank in banks:
            if getattr(bank, "display_name", "") == "Sage Memory Bank":
                return bank.name
        if banks:
            return banks[0].name
    except Exception as e:
        logger.warning(f"Could not list memory banks: {e}")

    return None


def retrieve_user_memories(user_email: Optional[str] = None, client=None) -> List[str]:
    """
    Retrieves consolidated long-term memories and preferences scoped to the user's email.
    Returns a list of extracted fact strings.
    """
    target_user = _resolve_user_email(user_email)
    bank_name = get_memory_bank_name(client)
    if not bank_name:
        return []

    cli = client or get_memory_client()
    if not cli:
        return []

    try:
        response = cli.memory_banks.memories.retrieve(
            name=bank_name,
            scope={"user_id": target_user},
        )
        facts = []
        for item in getattr(response, "page", []):
            mem = getattr(item, "memory", None)
            fact = getattr(mem, "fact", None) if mem else None
            if fact:
                facts.append(fact.strip())
        logger.info(f"Retrieved {len(facts)} memories for user '{target_user}' from Memory Bank.")
        return facts
    except Exception as e:
        logger.warning(f"Failed to retrieve memories for user '{target_user}': {e}")
        return []


def format_memories_for_prompt(memories: List[str]) -> str:
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
    user_email: Optional[str] = None,
    client=None,
) -> bool:
    """
    Consolidates a new user preference or financial rule into the user's Memory Bank scope.
    Memory Bank handles deduplication, semantic updates, and revision tracking automatically.
    """
    if not preference_or_rule or not preference_or_rule.strip():
        return False

    target_user = _resolve_user_email(user_email)
    bank_name = get_memory_bank_name(client)
    if not bank_name:
        logger.warning("No Memory Bank resource name configured; cannot persist preference.")
        return False

    cli = client or get_memory_client()
    if not cli:
        logger.warning("Agent Platform client unavailable; cannot persist preference.")
        return False

    try:
        clean_fact = preference_or_rule.strip()
        cli.memory_banks.memories.generate(
            name=bank_name,
            direct_memories_source={"direct_memories": [{"fact": clean_fact}]},
            scope={"user_id": target_user},
        )
        logger.info(f"Successfully consolidated preference into Memory Bank for '{target_user}': {clean_fact}")
        return True
    except Exception as e:
        logger.error(f"Failed to generate memory in Memory Bank for '{target_user}': {e}")
        return False


def store_user_preference(preference_or_rule: str) -> str:
    """
    Persists an explicit financial preference, budget ceiling, debt acceleration target,
    or standing rule into the user's long-term Memory Bank for cross-session continuity.

    Call this tool whenever the user instructs Sage to remember a goal, sets a budget cap,
    establishes a payoff deadline, or defines a spending rule (e.g. 'Remember that we want to cap
    dining at $400/month', 'Our target is to pay off the HELOC by December 2026', 'Ignore coffee transactions <$10').

    Args:
        preference_or_rule: The concise rule, goal, or preference statement to store.
    """
    user_email = _resolve_user_email()
    success = save_user_preference(preference_or_rule, user_email=user_email)
    if success:
        return f"Successfully saved to your long-term Memory Bank for {user_email}: '{preference_or_rule.strip()}'."
    else:
        return f"Could not record preference into Memory Bank due to a transient service error. The rule was: '{preference_or_rule.strip()}'."

