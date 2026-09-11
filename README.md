# FinSage: Family Financial Intelligence Hub
**Automated Personal Finance, Cash Flow & Debt Acceleration Hub via Monarch Money, Google BigQuery & Gemini Flash**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Google%20Cloud%20Platform-4285F4.svg)](https://cloud.google.com/)
[![Google BigQuery](https://img.shields.io/badge/Warehouse-Google%20BigQuery-669DF6.svg)](https://cloud.google.com/bigquery)
[![Gemini Flash](https://img.shields.io/badge/AI%20Model-Gemini%20Flash-8E24AA.svg)](https://deepmind.google/technologies/gemini/)
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC.svg)](https://www.terraform.io/)
[![Tests: 148 Passing](https://img.shields.io/badge/Tests-148%20Passing-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<p align="center">
  <img src="static/workflow.png" alt="Automated Family Financial AI Workflow" width="100%">
</p>

An enterprise-grade, serverless family financial advisor, cash flow coordinator, and debt acceleration engine deployed to **Google Cloud Platform**. It bridges **Monarch Money** directly into **Google BigQuery** data warehouse models, powered by an autonomous, bidirectional **Google Chat Co-Pilot (FinSage)** running **Gemini Flash** with **Automatic Function Calling (AFC)**, **Multimodal Vision**, and **Long-Term Memory**.

---

## 📚 Documentation Index

FinSage documentation is broken out into dedicated guides:

| Guide | Description |
| :--- | :--- |
| **[BigQuery Analytical Views](docs/analytical-views.md)** | Deep dive into the 18+ BigQuery models: daily debt carry math, subscription price creep, grocery-to-dining ratios, and paycheck sweep algorithms. |
| **[Google Chat Financial Advisor](docs/chat-advisor.md)** | FinSage bot architecture, slash command reference (`/brief`, `/sweep`, `/tax`, `/digest`), multimodal receipt ingestion, and guarded mutations. |
| **[Configuration & Custom Rates](docs/configuration.md)** | Setting baseline APRs (Mortgage, HELOC, Loans), account overrides, decommissioned accounts, and Secret Manager resolution. |
| **[Deployment & Operations](docs/deployment.md)** | Step-by-step setup with Terraform, Google Cloud Build, Cloud Run Jobs, Cloud Scheduler, and zero-ingress Pub/Sub configuration. |

---

## Core Engineering Highlights

* **Deterministic Arithmetic over Hallucination**: AI models are prone to arithmetic errors. All financial calculations—daily compounding debt carry across mortgages and HELOCs, subscription price creep, dining efficiency ratios, and paycheck surplus sweeps—are executed deterministically in BigQuery SQL views. Gemini queries these views via read-only tools to ground recommendations in exact arithmetic.
* **Paycheck Surplus Sweep & Debt Acceleration**: Automatically models 30-day fixed overhead burn baselines plus upcoming lump-sum bills. When payroll deposits land, FinSage calculates safe checking reserves and computes the exact sweep amount to pay down high-carry debt, reporting daily, monthly, and annual compound interest saved.
* **Multimodal Vision & Tax Ingestion**: Paste receipts or invoices directly into Google Chat. Gemini Vision extracts itemized lines, scrubs sensitive PII (SSN, EIN, card numbers), categorizes tax deductibility (Schedule C, HSA/FSA, Charities), and asymmetrically matches against posted bank debits with tip authorization handling.
* **Guarded Mutations & Cryptographic Confirmation**: When modifying transaction categories or updating records, FinSage requires physical confirmation via interactive Google Chat Cards v2 with HMAC-SHA256 tokens and an append-only BigQuery audit trail.
* **Zero-Ingress Perimeter Security**: Cloud Run runs with `--ingress internal` and receives chat events exclusively through an asynchronous **Google Cloud Pub/Sub** streaming pull worker (`app/chat_worker.py`). The microservice opens connections outward and exposes zero listening ports to the public internet.
* **Always-Free Tier Efficiency**: Operates entirely within Google Cloud's **Always Free Tier** (~**$0.37 / month** total infrastructure cost).

---

## Architectural Workflow (Zero-Ingress Posture)

```mermaid
flowchart TD
    subgraph Scheduling ["Automated Schedules (Cloud Scheduler)"]
        CronSync["Daily Ingestion (04:00 AM)<br/>monarch-daily-sync"]
        CronAlert["Daily Proactive Scan (08:00 AM)<br/>monarch-daily-advisor-alerts"]
    end

    subgraph BatchLayer ["Serverless Batch Layer (Cloud Run Jobs)"]
        JobSync["monarch-sync-job<br/>(python -m app.job sync)"]
        JobAlert["monarch-alerts-job<br/>(python -m app.job alerts)"]
        MMClient["MonarchMoney GraphQL Client<br/>+ Automated Base32 TOTP (pyotp)"]
        AdvisorEngine["Proactive Spend Alert Engine"]
    end

    subgraph DataWarehouse ["Google BigQuery Data Warehouse"]
        RawTables["Tables:<br/>• raw_accounts<br/>• raw_transactions<br/>• raw_categories<br/>• receipt_records<br/>• alert_suppression<br/>• mutation_audit_log"]
        Views["Analytical Optimization Views:<br/>• v_debt_daily_cost (Daily Compounding Debt)<br/>• v_debt_summary (Aggregated Liabilities)<br/>• v_subscription_price_creep (Sequential LAG Hikes)<br/>• v_subscription_overlap (Domain Redundancies)<br/>• v_food_efficiency (Groceries vs Dining)<br/>• v_micro_transaction_leakage (Habit Leaks)<br/>• v_spend_classification (Fixed vs Discretionary)<br/>• v_paycheck_surplus_sweep (Multi-Debt Sweep Engine)<br/>• v_tax_deductible_summary (Schedule C / HSA)"]
    end

    subgraph PrivateIngestion ["Private Inbound Chat (Zero Ingress Ports)"]
        Topic["Pub/Sub Topic<br/>monarch-chat-incoming"]
        Worker["Chat Pull Worker (chat_worker.py)<br/>Outbound Streaming Pull"]
    end

    subgraph Intelligence ["Gemini Brain & Chat Interface (FinSage)"]
        GeminiFlash["Gemini Flash Brain<br/>• Automatic Function Calling (AFC)<br/>• Read-only BigQuery Tool<br/>• Live Monarch Confirmation Tools<br/>• Paycheck Sweep & Tax Analysis Tools"]
        MultimodalVision["Multimodal Vision Ingestion<br/>(Pasted PNG/JPG/PDF Receipts)"]
    end

    subgraph ChatSpace ["User Interface (Google Chat)"]
        GoogleChat["Google Chat Space & 1:1 DMs<br/>• Native Cards v2 Actionable Alerts<br/>• Conversational Financial Co-Pilot"]
    end

    CronSync -->|"IAM OAuth"| JobSync
    JobSync --> MMClient
    MMClient -->|"GraphQL Extraction"| RawTables
    RawTables --> Views

    CronAlert -->|"IAM OAuth"| JobAlert
    Views --> JobAlert
    JobAlert --> AdvisorEngine
    AdvisorEngine -->|"HTTPS Card v2 POST"| GoogleChat

    GoogleChat -->|"Inbound Event"| Topic
    Topic -->|"Streaming Pull"| Worker
    Worker --> MultimodalVision
    MultimodalVision --> GeminiFlash
    GeminiFlash -->|"Analytical SQL"| Views
    Views -->|"Exact Results"| GeminiFlash
    GeminiFlash -->|"Live Read"| MMClient
    MMClient -->|"Live Data"| GeminiFlash
    GeminiFlash -->|"Async REST Reply"| GoogleChat
```

---

## Google Chat Commands Cheatsheet

| Command | Action | Primary Model / Tool |
| :--- | :--- | :--- |
| **`/brief`** or **`/alerts`** | Renders morning financial synopsis and active spend alerts. | `v_debt_summary`, `v_spend_classification` |
| **`/sweep`** | Computes safe paycheck surplus to sweep to high-rate variable debt. | `v_paycheck_surplus_allocation` |
| **`/tax [YYYY]`** | Displays annual tax deductibility summary (Schedule C, HSA, Charities). | `v_tax_deductible_summary` |
| **`/digest [weekly\|monthly]`** | Generates an executive CFO performance briefing. | `v_debt_summary`, `v_spend_classification` |
| **`/sync`** | Triggers immediate Monarch Money ingestion into BigQuery. | `monarch_service.sync_accounts_to_bq()` |
| **`/receipt`** *(with attachment)* | Extracts and logs receipt with IRS tax classification and bank match. | Gemini Vision, `raw_transactions` |
| **`/help`** | Displays quick command reference and usage examples. | Built-in |

---

## Quickstart (2-Minute Local Setup)

```bash
# 1. Clone repository and install dependencies
git clone https://github.com/n0012/family-financial-intelligence-hub.git
cd family-financial-intelligence-hub
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Configure credentials & custom interest rates
cp .env.example .env.local
cp config.example.yaml config.yaml

# 3. Run automated test suite
PYTHONPATH=. ./.venv/bin/pytest -v
```

For complete deployment instructions, see the **[Deployment & Operations Guide](docs/deployment.md)**.

---

## License & Acknowledgments

This project is licensed under the [MIT License](LICENSE).

Inspirations & libraries:
* [Concept and Architecture Reference](https://chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339)
* [`hammem/monarchmoney`](https://github.com/hammem/monarchmoney) — Python client library for Monarch Money.
* [`6missedcalls/personal-finance-skill`](https://github.com/6missedcalls/personal-finance-skill) — Multi-tier policy guardrails, FRED macro-economic grounding, and IRS tax classification schemas.
