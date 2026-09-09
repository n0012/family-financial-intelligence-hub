import os
import requests
import streamlit as st
from google import genai
from google.genai import types

st.set_page_config(
    page_title="Monarch Money Copilot & Advisor (Gemini)",
    page_icon="💰",
    layout="wide",
)

st.title("💰 Monarch Money Copilot & Financial Advisor")
st.caption("Powered by Google Gemini 2.5, Google Cloud Run & BigQuery")

# Configuration from environment or Streamlit inputs
WRAPPER_URL = os.getenv("SERVICE_URL", "http://localhost:8000")
WRAPPER_KEY = os.getenv("GEMINI_WRAPPER_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

with st.sidebar:
    st.header("⚙️ Configuration")
    wrapper_url = st.text_input("Cloud Run / Wrapper URL", value=WRAPPER_URL)
    wrapper_key = st.text_input("Wrapper API Key (X-API-Key)", value=WRAPPER_KEY, type="password")
    gemini_key = st.text_input("Gemini API Key", value=GEMINI_API_KEY or "", type="password")
    
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Test Health"):
            try:
                r = requests.get(f"{wrapper_url}/health", headers={"X-API-Key": wrapper_key}, timeout=5)
                if r.status_code == 200:
                    st.success("Connected!")
                else:
                    st.error(f"HTTP {r.status_code}")
            except Exception as e:
                st.error(f"Error: {e}")
    with col2:
        if st.button("Sync BigQuery"):
            try:
                with st.spinner("Syncing to BigQuery..."):
                    r = requests.post(
                        f"{wrapper_url}/sync/bigquery?days_back=180",
                        headers={"X-API-Key": wrapper_key},
                        timeout=60,
                    )
                    if r.status_code == 200:
                        st.success("Synced to BQ!")
                        st.json(r.json().get("synced_counts", {}))
                    else:
                        st.error(f"Sync failed: {r.text}")
            except Exception as e:
                st.error(f"Error: {e}")

# Tool functions for Gemini function calling
def get_accounts() -> dict:
    """Retrieve all connected financial accounts, types, and current balances from Monarch Money."""
    resp = requests.get(
        f"{wrapper_url}/accounts",
        headers={"X-API-Key": wrapper_key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()

def get_transactions(start_date: str = None, end_date: str = None, limit: int = 50) -> dict:
    """
    Fetch transactions filtered by date range from Monarch Money.
    
    Args:
        start_date: Optional start date in YYYY-MM-DD format.
        end_date: Optional end date in YYYY-MM-DD format.
        limit: Max transactions to return (max 200).
    """
    params = {"limit": limit}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    resp = requests.get(
        f"{wrapper_url}/transactions",
        headers={"X-API-Key": wrapper_key},
        params=params,
        timeout=25,
    )
    resp.raise_for_status()
    return resp.json()

def get_categories() -> dict:
    """Retrieve spending and income category hierarchy from Monarch Money."""
    resp = requests.get(
        f"{wrapper_url}/categories",
        headers={"X-API-Key": wrapper_key},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def get_cashflow(start_date: str = None, end_date: str = None) -> dict:
    """Retrieve cashflow summary (income, expenses, savings) from Monarch Money."""
    params = {}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    resp = requests.get(
        f"{wrapper_url}/cashflow",
        headers={"X-API-Key": wrapper_key},
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()

# Chat history initialization
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "Hi! I'm your Gemini Family Financial Advisor. Ask me to:\n"
                "- **Audit subscriptions** and find hidden price increases.\n"
                "- **Analyze discretionary leakage** (dining out, delivery, micro-transactions).\n"
                "- **Simulate HELOC paydown** and calculate daily interest savings."
            ),
        }
    ]

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("E.g., Analyze our spending and suggest 3 ways to reduce outflows and pay down our HELOC faster."):
    st.chat_message("user").markdown(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})

    if not wrapper_key:
        st.error("Please provide the Wrapper API Key in the sidebar.")
    else:
        try:
            client = genai.Client(api_key=gemini_key) if gemini_key else genai.Client()
            with st.chat_message("assistant"):
                with st.spinner("Analyzing financial data and crafting optimization recommendations..."):
                    response = client.models.generate_content(
                        model="gemini-3.8-flash",
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
                            system_instruction=(
                                "You are an expert personal financial advisor and spend optimization strategist for a family. "
                                "Use the provided Monarch Money tools to fetch real data. "
                                "Proactively identify spend optimizations, recurring subscription waste, and high-variance discretionary leaks. "
                                "Always quantify savings in terms of debt acceleration: every dollar saved reduces daily compounding interest on the HELOC. "
                                "Summarize figures clearly in tables or bullet points."
                            ),
                            tools=[get_accounts, get_transactions, get_categories, get_cashflow],
                        ),
                    )
                    st.markdown(response.text)
                    st.session_state.messages.append({"role": "assistant", "content": response.text})
        except Exception as e:
            st.error(f"Error querying Gemini: {e}")
