#!/usr/bin/env python3
"""
Provision a BigQuery Conversational Analytics Agent for Family Finance & Spend Optimization.
Uses the Gemini Data Analytics API (geminidataanalytics.googleapis.com)
matching the pattern established in bigquery-graph-demo.
Completely dependency-free (uses Python standard library and gcloud).
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
DATASET_ID = os.getenv("BQ_DATASET_ID", "family_finance")
AGENT_ID = os.getenv("CA_AGENT_ID", "family-finance-advisor")

if not PROJECT_ID:
    print("Error: PROJECT_ID environment variable is required.")
    sys.exit(1)

CA_BASE = "https://geminidataanalytics.googleapis.com/v1beta"
CA_PARENT = f"projects/{PROJECT_ID}/locations/global"


def get_access_token():
    try:
        token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
        return token
    except Exception as e:
        print(f"Error obtaining gcloud token: {e}")
        sys.exit(1)


def ca_request(url, method="GET", json_body=None):
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "x-goog-user-project": PROJECT_ID,
        "Content-Type": "application/json",
    }
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"error": content}
        return e.code, parsed


# Grounded tables and analytical optimization views
TABLES = [
    "raw_accounts",
    "raw_transactions",
    "raw_categories",
    "v_active_subscriptions",
    "v_subscription_price_creep",
    "v_spend_classification",
    "v_heloc_daily_cost",
    "v_food_efficiency",
    "v_micro_transaction_leakage",
    "v_subscription_overlap",
    "v_utility_seasonal_baseline",
]

SYSTEM_INSTRUCTION = (
    "You are an expert personal financial advisor and spend optimization strategist for a family. "
    "Your source of truth is Monarch Money synchronized into Google BigQuery. "
    "CORE MISSION: Help the family optimize spending, eliminate waste, establish budget discipline, and aggressively pay down HELOC debt. "
    "\n"
    "WHEN PRODUCING RECOMMENDATIONS & OPTIMIZATIONS: "
    "1. TIER 1 - PAINLESS CUTS (Zero Lifestyle Impact): "
    "   - Query v_subscription_overlap and v_active_subscriptions. "
    "   - Flag duplicate streaming/cloud services (e.g., multiple media apps active simultaneously, redundant cloud storage). "
    "   - Query v_subscription_price_creep for recent price rises; quote annual_impact (the annualised increase) as the recoverable amount, never estimated_annual_cost (the whole plan). "
    "   - Suggest rotation strategies (e.g. keep 1 streaming service active per season instead of 4 at once). "
    "   - Honour the `disposition` column before advising: CANCELLABLE can be cancelled or rotated; RESHOPPABLE (insurance, telecom, broadband, security monitoring) must be re-quoted or renegotiated at renewal, never cancelled; ESSENTIAL_METERED (electric, gas, water) is a regulated monopoly with no cancel action, so compare it against v_utility_seasonal_baseline (same calendar month, prior years) and discuss consumption or rate schedules only. "
    "2. TIER 2 - HIGH-LEVERAGE HABIT OPTIMIZATION: "
    "   - Query v_food_efficiency. If dining_percentage_of_food_budget > 30%, identify delivery markups (DoorDash, UberEats). "
    "   - Propose concrete shifts (e.g., 'Moving 2 delivery meals/month to home cooking frees $220/month'). "
    "   - Query v_micro_transaction_leakage to identify frequent sub-$35 habit leaks (convenience stores, daily coffee, impulse app buys). "
    "3. TIER 3 - HELOC ACCELERATION & DEBT VELOCITY: "
    "   - Query v_heloc_daily_cost to get the current HELOC balance, APR, and daily interest burden. "
    "   - For every dollar saved from Tiers 1 and 2, ALWAYS calculate the exact debt paydown impact: "
    "     * Daily and annual compound interest eliminated. "
    "     * Number of months shaved off the debt payoff timeline. "
    "\n"
    "CALCULATION STANDARDS: "
    "Always generate BigQuery GoogleSQL queries against the dataset views to obtain 100% exact math. "
    "Never guess, estimate, or hallucinate numbers. Format currency as $X,XXX.XX. "
    "Keep answers empathetic, encouraging, and structured with clear actionable next steps."
)


def create_agent():
    print(f"Creating / Updating Conversational Analytics Agent: {AGENT_ID}...")

    agent_body = {
        "display_name": "Family Financial & Spend Optimization Advisor",
        "description": "Proactively identifies spend optimizations, cuts subscription bloat, and optimizes HELOC paydown.",
        "data_analytics_agent": {
            "published_context": {
                "datasource_references": {
                    "bq": {
                        "table_references": [
                            {"projectId": PROJECT_ID, "datasetId": DATASET_ID, "tableId": t} for t in TABLES
                        ]
                    }
                },
                "system_instruction": SYSTEM_INSTRUCTION,
            }
        },
    }

    url = f"{CA_BASE}/{CA_PARENT}/dataAgents?data_agent_id={AGENT_ID}"
    status, resp_data = ca_request(url, method="POST", json_body=agent_body)

    if status in (200, 201):
        print(f"Agent successfully created: {resp_data.get('name')}")
    elif status == 409:
        print(f"Agent {AGENT_ID} already exists. Updating existing agent context...")
        update_url = f"{CA_BASE}/{CA_PARENT}/dataAgents/{AGENT_ID}?update_mask=data_analytics_agent.published_context"
        update_status, update_data = ca_request(update_url, method="PATCH", json_body=agent_body)
        print(f"Update status: {update_status}")
    else:
        print(f"Failed to create agent: HTTP {status}")
        print(json.dumps(resp_data, indent=2))
        sys.exit(1)


def ask(question: str):
    """Sends an advisory question to the Conversational Analytics Agent and displays SQL + Answer."""
    body = {
        "parent": CA_PARENT,
        "messages": [{"userMessage": {"text": question}}],
        "data_agent_context": {"data_agent": f"{CA_PARENT}/dataAgents/{AGENT_ID}"},
    }
    url = f"{CA_BASE}/{CA_PARENT}:chat"
    status, resp_data = ca_request(url, method="POST", json_body=body)

    print("\n" + "=" * 78)
    print("QUESTION:", question)
    print("=" * 78)

    if status != 200:
        print(f"Error ({status}):", json.dumps(resp_data, indent=2))
        return

    messages = resp_data if isinstance(resp_data, list) else resp_data.get("messages", [])
    for m in messages:
        sm = m.get("systemMessage", {})
        if "data" in sm and sm["data"].get("generatedSql"):
            print("\n--- GENERATED SQL ---")
            print(sm["data"]["generatedSql"].strip())
        if "text" in sm and sm["text"].get("textType") != "THOUGHT":
            parts = sm["text"].get("parts", [])
            print("\n--- ADVISOR RECOMMENDATION ---")
            print(" ".join(parts).strip())


if __name__ == "__main__":
    create_agent()
    print("\nConversational Analytics Agent initialized successfully!")
