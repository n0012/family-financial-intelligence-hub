# FinSage: Family Financial Intelligence Hub
**Automated Personal Finance & Spend Optimization via Monarch Money, Google BigQuery & Gemini 3.8 Flash**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Google%20Cloud%20Platform-4285F4.svg)](https://cloud.google.com/)
[![Google BigQuery](https://img.shields.io/badge/Warehouse-Google%20BigQuery-669DF6.svg)](https://cloud.google.com/bigquery)
[![Gemini 3.8 Flash](https://img.shields.io/badge/AI%20Model-Gemini%203.8%20Flash-8E24AA.svg)](https://deepmind.google/technologies/gemini/)
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC.svg)](https://www.terraform.io/)
[![Tests: 76 Passing](https://img.shields.io/badge/Tests-76%20Passing-brightgreen.svg)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<p align="center">
  <img src="static/workflow.png" alt="Automated Family Financial AI Workflow" width="100%">
</p>

An enterprise-grade, serverless family financial advisor and spend optimization hub deployed to **Google Cloud Platform**. It bridges **Monarch Money**'s GraphQL API directly into **Google BigQuery** data warehouse models, powered by a bidirectional **Google Chat Advisor (FinSage)** running **Gemini 3.8 Flash** with **Automatic Function Calling (AFC)** and **Multimodal Vision**.

---

## Executive Summary & Engineering Highlights

Traditional personal finance tools (Monarch, Mint, YNAB) excel at aggregating transactions and basic budgeting, but lack proactive quantitative reasoning, debt paydown acceleration mathematics, and conversational interfaces. 

This project transforms raw personal finance data into a continuous, intelligent financial advisor:

* **Deterministic Arithmetic over Hallucination**: AI models are notoriously prone to arithmetic mistakes when performing math on financial figures. Here, all financial logic—daily compounding interest, subscription price creep, grocery-to-dining ratios, and micro-transaction leakage—is modeled directly in BigQuery GoogleSQL analytical views. Gemini queries these views via live read-only tools to ground every recommendation in deterministic arithmetic.
* **Multimodal Vision in Google Chat**: Users can paste screenshots of spreadsheets, brokerage accounts, paystubs, or compensation outlook plans directly into Google Chat. Gemini 3.8 Flash parses the visual layout, extracts line items and dates, and correlates them against live bank balances in BigQuery.
* **Lifecycle & Migration Intelligence**: Automatically detects active vs legacy/superseded accounts across banking mergers, account upgrades, and credit card replacements without hardcoded account IDs.
* **Idempotent Ingestion & Deduplication**: High-performance pagination pulls transactions from Monarch Money into staging tables and executes atomic SQL `MERGE` operations, preventing duplicate entries across repeated syncs.
* **Serverless Cost Efficiency**: Operates entirely within Google Cloud's **Always Free Tier** (~**$0.37 / month** total infrastructure cost).

---

## Architectural Workflow (Zero-Ingress Posture)

```mermaid
flowchart TD
    subgraph Scheduling ["Automated Schedules (Cloud Scheduler)"]
        CronSync["Daily Ingestion (04:00 AM)<br/>monarch-daily-sync"]
        CronAlert["Daily Proactive Scan (08:00 AM)<br/>monarch-daily-advisor-alerts"]
    end

    subgraph BatchLayer ["Serverless Batch Layer (Cloud Run Jobs - Ephemeral Execution)"]
        JobSync["monarch-sync-job<br/>(python -m app.job sync)"]
        JobAlert["monarch-alerts-job<br/>(python -m app.job alerts)"]
        MMClient["MonarchMoney GraphQL Client<br/>+ Automated Base32 TOTP (pyotp)"]
        AdvisorEngine["Proactive Spend Alert Engine"]
    end

    subgraph DataWarehouse ["Google BigQuery Data Warehouse"]
        RawTables["Raw Tables:<br/>• raw_accounts<br/>• raw_transactions<br/>• raw_categories<br/>• staging_transactions"]
        Views["Analytical Optimization Views:<br/>• v_account_lifecycle (Active vs Superseded)<br/>• v_heloc_daily_cost (Daily Compounding Debt)<br/>• v_merchant_domain (Functional Domain & Disposition)<br/>• v_subscription_charges (Recurring Tier, POS Removed)<br/>• v_active_subscriptions (Cadence Run-Rates)<br/>• v_subscription_price_creep (Sequential LAG Hikes)<br/>• v_subscription_overlap (Domain Redundancies)<br/>• v_utility_seasonal_baseline (Same-Month Prior Years)<br/>• v_food_efficiency (Groceries vs Dining/Delivery)<br/>• v_micro_transaction_leakage (Sub-$35 Habit Leaks)<br/>• v_spend_classification (Fixed vs Discretionary)"]
    end

    subgraph ProactiveOutbound ["Proactive Outbound Alerts (Request/Response HTTPS)"]
        Webhook["Google Chat Incoming Webhook<br/>chat.googleapis.com/v1/spaces/...<br/>(Pub/Sub has no Chat sink -- a space is<br/>only reachable by an authenticated HTTPS call)"]
    end

    subgraph PrivateIngestion ["Private Inbound Chat Integration (Zero Inbound Ports)"]
        Topic["Pub/Sub Topic<br/>monarch-chat-incoming"]
        Worker["Chat Pull Worker<br/>(python -m app.chat_worker)<br/>Outbound Streaming Pull"]
    end

    subgraph MemoryLayer ["Long-Term Memory Bank (Vertex AI Agent Platform)"]
        MemoryBank["Reasoning Engine Memory Bank<br/>(FinSage Memory Bank)<br/>• User-Scoped Preferences<br/>• Fact Consolidation & Conflict Resolution"]
    end

    subgraph Intelligence ["Gemini 3.8 Flash Brain & Chat Interface (FinSage)"]
        GeminiFlash["Gemini 3.8 Flash<br/>• MEDIUM Thinking Budget<br/>• Automatic Function Calling (AFC)<br/>• Read-only BigQuery Tool<br/>• Live Monarch Confirmation Tools<br/>• Persistent Memory Bank AFC Tool"]
        MultimodalVision["Multimodal Vision Ingestion<br/>(Pasted PNG/JPG Screenshots & Plans)"]
    end

    subgraph ChatSpace ["User Interface (Google Chat Room)"]
        GoogleChat["Google Chat Space & 1:1 DMs<br/>• Native Cards v2 Actionable Alerts<br/>• Conversational Financial Co-Pilot"]
    end

    CronSync -->|"IAM OAuth (Cloud Run API)"| JobSync
    JobSync --> MMClient
    MMClient -->|"GraphQL Extraction"| RawTables
    RawTables --> Views

    CronAlert -->|"IAM OAuth (Cloud Run API)"| JobAlert
    Views --> JobAlert
    JobAlert --> AdvisorEngine
    AdvisorEngine -->|"Direct HTTPS Card v2 POST"| Webhook
    Webhook --> GoogleChat

    GoogleChat -->|"Inbound User Message Event"| Topic
    Topic -->|"Outbound Streaming Pull (No Ingress Ports)"| Worker
    Worker --> MultimodalVision
    MultimodalVision --> GeminiFlash
    MemoryBank -->|"Active Preferences & Targets"| GeminiFlash
    GeminiFlash -->|"Consolidate: store_user_preference"| MemoryBank
    GeminiFlash -->|"Analytical SQL: run_readonly_sql_tool"| Views
    Views -->|"Deterministic Query Results"| GeminiFlash
    GeminiFlash -->|"Live Read: get_live_account_balance / txn"| MMClient
    MMClient -->|"Live Data Confirmation"| GeminiFlash
    GeminiFlash -->|"Async REST Reply (chat.googleapis.com)"| GoogleChat
```

### Why inbound uses Pub/Sub and outbound does not

The two Chat paths are deliberately asymmetric, and the asymmetry is a property of the Google Chat API rather than a design choice:

* **Inbound (Chat → FinSage) is event-driven.** Google Chat is the *publisher*; it writes user message events into `monarch-chat-incoming`. FinSage subscribes with an outbound streaming pull, which is what buys the zero-ingress posture — the app opens a connection outward and never listens on a port.
* **Outbound (FinSage → Chat) is request/response.** Pub/Sub has no Google Chat sink. A subscription can only deliver to a pull client or push to an HTTPS endpoint you own — it cannot deposit a message into a space, and Chat is not subscribed to that topic. The only way a card reaches a space is an authenticated HTTPS call to `chat.googleapis.com`, either an incoming-webhook URL or `spaces.messages.create` with a service-account token.

So publishing alerts to Pub/Sub would not deliver anything on its own: it would still require a permanently running subscriber whose sole job is to make the same HTTPS call, adding a hop and an always-on component. It would also buy nothing in security terms, because the Cloud Run Job already makes that call **outbound** and exposes no ingress. Pub/Sub earns its place on the inbound path (where it removes a public listener) and would only add latency and a failure mode on the outbound one.

Pub/Sub would become the right answer outbound if the alert fan-out grew several independent consumers (Chat plus email plus a mobile push), or if alert delivery needed durable retry and replay independent of the job's lifetime. At one destination, once a day, it does not.

---

## BigQuery Data Model & Analytical Views

The data warehouse decouples storage from analytical modeling, allowing queries to run instantaneously across years of transaction history:

| View / Table | Description & Optimization Logic |
| :--- | :--- |
| **`raw_accounts`** | Active balances, credit limits, reported interest rates, and institution metadata. |
| **`raw_transactions`** | Deduplicated, sanitized transaction stream with merchant categorization. |
| **`raw_categories`** | Budget envelopes grouped into Fixed Overhead, Discretionary, Debt, and Income. |
| **`v_account_lifecycle`** | Dynamically classifies accounts as `PRIMARY` vs `SUPERSEDED` based on activity recency, non-zero balance, and transaction count. Resolves duplicate accounts during bank mergers. |
| **`v_heloc_daily_cost`** | Computes the exact daily compounding cost (`(balance * apr) / 365`) and monthly carrying cost of variable-rate debt, alongside payoff acceleration impacts. |
| **`v_merchant_domain`** | Maps each merchant to the functional domain it competes in and a `disposition` that constrains the advice: `CANCELLABLE`, `RESHOPPABLE` (insurance, telecom — re-quote, never cancel), `ESSENTIAL_METERED` (regulated utilities — no cancel action exists), `NOT_A_SUBSCRIPTION`. |
| **`v_subscription_charges`** | The cleaned recurring-charge ledger. Trusts the aggregator's recurrence flag where present, otherwise keeps only charges within 60–200% of the merchant's median, so an incidental cafe purchase at a gym never gets compared against the membership fee. |
| **`v_active_subscriptions`** | One row per merchant (not per merchant/category, which fragmented a single service whenever the aggregator re-categorised it). Detects billing cadence and derives a cadence-normalised `estimated_annual_cost` and `monthly_run_rate`. |
| **`v_subscription_price_creep`** | Compares the latest bill against the mean of the preceding three cycles via `LAG()`/window framing. Requires recency within 45 days, a +3% to +40% move, and >$0.50, so a change from a year ago stops firing and a single anomalous cycle cannot fabricate one. Reports `annual_impact` (the annualised increase), not the whole plan cost. |
| **`v_subscription_overlap`** | Groups only *concurrently active*, functionally substitutable services by domain, with a per-domain threshold (3 for video streaming, 2 elsewhere). Reports `consolidation_savings_monthly` — the total minus the largest plan — rather than implying every service can be cancelled. |
| **`v_utility_seasonal_baseline`** | Compares each metered utility month against the **same calendar month in prior years**, so heating and cooling swings are measured against their own season instead of against the previous month. |
| **`v_food_efficiency`** | Calculates the monthly ratio between grocery purchases and dining out / food delivery markups (DoorDash, UberEats, Grubhub). |
| **`v_micro_transaction_leakage`** | Flags frequent sub-$35 convenience transactions (coffee shops, convenience stores, app purchases) and calculates their annualized drain. |
| **`v_spend_classification`** | Classifies all monthly outflows into Fixed Overhead vs Discretionary spend to evaluate baseline burn rate. |

---

## Interactive Google Chat Advisor (FinSage)

The microservice functions as a registered **Google Chat Bot** supporting both 1:1 direct messages and collaborative family spaces:

### 1. Conversational Queries & Multi-Turn Reasoning
Ask complex financial questions in natural language. Gemini selects the appropriate analytical view, runs the query, and synthesizes actionable recommendations:
* *"What is our daily interest cost on the HELOC right now?"*
* *"Which subscriptions increased in price over the last year?"*
* *"How much did we spend on dining out vs groceries last month?"*
* *"Where are our top micro-transaction leaks under $35?"*
* *"If we redirect $500/month from discretionary spending to debt paydown, how much interest do we eliminate?"*

### 2. Multimodal Vision Analysis (Screenshots & Projections)
Paste images directly into Google Chat:
* **Outlook & Compensation Tables**: Paste a screenshot of an annual compensation breakdown or bonus projection. The bot extracts base salary, bonuses, and equity vesting dates, then models optimal tax withholding and debt-payoff allocation.
* **External Brokerage / Loan Statements**: Paste a PDF or PNG statement from an unlinked institution. Gemini extracts balances, interest rates, and minimum payments to incorporate into your debt snow-ball calculations.

### 3. Persistent User Preferences & Long-Term Memory
Powered by Google Cloud's **Vertex AI Agent Platform Reasoning Engine Memory Bank**, FinSage remembers your family's financial targets, payoff milestones, and budget ceilings across conversation threads. It automatically consolidates preferences and resolves conflicting goals without database schema bloat.

### 5. Daily Morning Brief & Executive Financial Synopsis
Each morning, Cloud Run Job scans account posture and spend pacing, posting a comprehensive two-tier Card v2 message to Google Chat:
* **🌅 Morning Financial Synopsis**: Executive snapshot detailing current liquid checking reserves (with monthly fixed burn coverage buffer), active HELOC balance with exact daily interest carry ($/day) and monthly carry, plus month-to-date spending pacing vs days elapsed.
* **🎯 What to Pay Attention to Today**: High-priority focus bullets synthesized directly from posture and active alerts (e.g. price hikes, dining out ratio, debt sweep opportunities, micro-spend habits, or an "all systems normal" confirmation).
* **Daily Optimization Opportunities**: Proactive spend advisory cards featuring interactive 7-day snooze buttons.

You can also request this on demand at any time via natural language (*"What's today's morning brief?"*, *"Give me our daily financial synopsis"*) via the `get_daily_morning_brief()` Gemini tool, or query the `/advisor/morning-brief` API endpoint.

### 6. Chat Commands & Shortcuts
* `/sync` — Pulls latest transactions from Monarch Money into BigQuery immediately.
* `/brief` / `/alerts` — Triggers an on-demand scan across all BigQuery optimization views and posts the morning synopsis and alert summary with snooze actions.
* `/help` — Displays quick reference guides and sample prompts.

---

## Repository Structure
 
```
family-financial-intelligence-hub/
├── app/
│   ├── __init__.py
│   ├── alerts.py                # Proactive spend anomaly alert engine & Google Chat Card v2 builder
│   ├── bq_service.py            # BigQuery SQL query tool, analytical views & schema migration
│   ├── chat_worker.py           # Zero-ingress Google Chat Pub/Sub pull subscriber
│   ├── config.py                # Centralized configuration, local overrides, & secret caching
│   ├── job.py                   # Cloud Run Job CLI entrypoint (sync & alerts batch runner)
│   ├── main.py                  # FastAPI application & Google Chat webhook router
│   ├── memory_service.py        # Vertex AI Agent Platform Memory Bank client & user preference tools
│   └── monarch_service.py       # MonarchMoney client auth, sync pipelines & live confirmation tools
├── docs/
│   ├── ALERTS_STRATEGY.md       # Proactive anomaly alert & debt acceleration strategy
│   └── PLAN.md                  # Project master plan, PR roadmap & execution history
├── scripts/
│   ├── bootstrap_gcp_project.sh # Initial GCP project bootstrapping & IAM automation
│   ├── create_ca_agent.py       # Google Cloud Conversational Analytics Agent deployment script
│   ├── deploy.sh                # Hardened zero-ingress deployment script
│   └── sync_secrets_to_gcp.sh   # Automated secret synchronization from .env.local to Secret Manager
├── static/
│   ├── avatar.png               # Google Chat bot avatar image
│   └── workflow.png             # Architecture and workflow diagram
├── terraform/                   # Infrastructure as Code (Terraform / OpenTofu)
│   ├── main.tf                  # BigQuery, Artifact Registry, Pub/Sub, Cloud Scheduler, IAM
│   ├── variables.tf             # Configurable deployment variables
│   ├── outputs.tf               # Pub/Sub topics, subscriptions, and dataset outputs
│   └── terraform.tfvars.example # Example variable values
├── tests/                       # Pytest automated test suite (75 passing unit tests)
│   ├── test_alerts_and_config.py# Config caching, token auth, Card v2 builders, job CLI
│   ├── test_bq_service.py       # Read-only SQL safety guards, CA fallback, in-memory session history
│   ├── test_memory_service.py   # Vertex AI Memory Bank retrieval, prompt formatting, fact consolidation
│   ├── test_monarch_mutations.py# HMAC signatures, guarded mutations, Card v2 interactive actions
│   └── test_monarch_service.py  # Monarch auth, sync pipelines, live read tools, rate limits
├── .env.example                 # Template for environment configuration
├── Dockerfile                   # Production container definition (Python 3.11-slim)
├── cloudbuild.yaml              # Google Cloud Build CI/CD pipeline definition
├── config.example.json          # JSON configuration template
├── config.example.yaml          # YAML configuration template (rates, account overrides, exclusions)
├── pyproject.toml               # Python project configuration (Ruff, Pytest, packaging)
├── requirements.txt             # Python dependencies (includes google-cloud-aiplatform)
├── requirements-dev.txt         # Development & test dependencies
├── schema.sql                   # BigQuery schema definitions & analytical optimization views
└── LICENSE                      # MIT Open Source License
```

---

## Configuration & Custom Rates

You can configure custom interest rates (e.g. variable-rate HELOCs), manual account overrides, and bank exclusions using a local configuration file (`config.yaml` or `config.json`), or through Google Cloud Secret Manager / environment variables:

```bash
cp config.example.yaml config.yaml
```

```yaml
# config.yaml (gitignored - safe for private local use)
rates:
  default_heloc_apr: 0.0700          # Default APR for HELOCs (7.00%)
  default_mortgage_apr: 0.0400       # Default APR for Mortgages (4.00%)
  default_debt_apr: 0.0750           # Baseline fallback for loans/debt

account_overrides:
  "123456789012345678":
    name: "Sample Variable Rate Credit Line"
    interest_rate: 0.0700            # Explicit rate for a specific account

decommissioned_account_ids:
  - "987654321098765432"             # Account IDs to omit from sync

excluded_institutions:
  - "defunct_bank"                   # Institution keywords to filter out
```

During ingestion, the microservice automatically applies these rates and synchronizes them directly into BigQuery `raw_accounts.interest_rate`, powering the daily compounding cost calculations in `v_heloc_daily_cost`.

---

## Configuration & Environment Variables

The application resolves configuration seamlessly with the following precedence:
1. **Google Cloud Secret Manager** (production)
2. **Environment Variables** (local development & container runtime)
3. **Internal Defaults** (fail-safe fallbacks)

| Variable / Secret Name | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `MONARCH_EMAIL` (`monarch-email`) | Monarch Money account email | Yes | — |
| `MONARCH_PASSWORD` (`monarch-password`) | Monarch Money account password | Yes | — |
| `MONARCH_MFA_SECRET` (`monarch-mfa-secret`) | Base32 TOTP secret string (for automated 2FA) | Optional | `NONE` |
| `GEMINI_API_KEY` (`gemini-api-key`) | Google AI Studio Gemini API Key (or uses Vertex AI ADC) | Optional | ADC / Vertex AI |
| `ALERT_WEBHOOK_URL` (`alert-webhook-url`) | Google Chat incoming webhook URL for scheduled alerts | Optional | — |
| `GEMINI_WRAPPER_KEY` (`gemini-wrapper-key`) | Shared API secret for securing `/sync` and `/advisor` endpoints | Yes | Provisioned |
| `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` | Google Cloud Project ID | Yes | Auto-detected |
| `BQ_DATASET_ID` | BigQuery dataset name | No | `family_finance` |
| `REGION` | GCP deployment region | No | `us-central1` |
| `ACCOUNT_OVERRIDES_JSON` (`account-overrides`) | JSON map of manual account overrides (e.g. unlisted APRs) | Optional | `{}` |
| `DECOMMISSIONED_ACCOUNT_IDS` (`decommissioned-account-ids`) | CSV or JSON array of closed account IDs to ignore | Optional | `[]` |
| `EXCLUDED_INSTITUTIONS` (`excluded-institutions`) | CSV of institution keywords to exclude (e.g. merged banks) | Optional | `[]` |

---

## Deployment & Setup Guide

### 1. Prerequisites
* A [Google Cloud Platform](https://cloud.google.com/) account with billing enabled.
* Google Cloud SDK (`gcloud`) installed and authenticated:
  ```bash
  gcloud auth login
  gcloud auth application-default login
  ```
* [Terraform](https://www.terraform.io/) or [OpenTofu](https://opentofu.org/) installed.
* An active [Monarch Money](https://www.monarchmoney.com/) account.

---

### 2. Deploy Infrastructure via Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Update project_id and region in terraform.tfvars
terraform init
terraform apply
cd ..
```

Terraform provisions:
* **Artifact Registry** repository (`monarch-repo`)
* **BigQuery Dataset** (`family_finance`)
* **Cloud Pub/Sub** topic (`monarch-chat-incoming`) & subscription (`monarch-chat-sub`) for zero-ingress chat
* **Cloud Scheduler Jobs** (`monarch-daily-sync` and `monarch-daily-advisor-alerts`) authenticated via GCP OAuth IAM
* **Secret Manager** containers for all credentials
* **IAM Service Accounts**: Runtime identity (`monarch-gemini-run`) and scheduler identity (`monarch-scheduler-sa`)

---

### 3. Populate Secrets

Copy the example environment template and enter your credentials:
```bash
cp .env.example .env.local
# Fill in MONARCH_EMAIL, MONARCH_PASSWORD, MONARCH_MFA_SECRET, and ALERT_WEBHOOK_URL in .env.local
```

Synchronize the secrets to Google Cloud Secret Manager:
```bash
./scripts/sync_secrets_to_gcp.sh
```

---

### 4. Build & Deploy via Cloud Build

Trigger the automated build, container packaging, and zero-ingress deployment:
```bash
gcloud builds submit --config=cloudbuild.yaml --project=YOUR_PROJECT_ID
```

Cloud Build automatically:
1. Builds and tags the container image in Artifact Registry.
2. Deploys the hardened Cloud Run service with `--no-allow-unauthenticated` and `--ingress internal`.
3. Deploys the Cloud Run batch Jobs (`monarch-sync-job` and `monarch-alerts-job`).
4. Applies all analytical views and tables in `schema.sql` to BigQuery.

---

### 5. Configure the Google Chat App (Zero Ingress via Pub/Sub)

1. Go to the [Google Cloud Console → Google Chat API](https://console.cloud.google.com/apis/api/chat.googleapis.com).
2. Click **Configuration** and fill in:
   * **App name**: `FinSage`
   * **Avatar URL**: (Optional) URL to your bot avatar image.
   * **Description**: `Interactive family financial advisor powered by Monarch Money, BigQuery, and Gemini 3.8 Flash.`
   * **Functionality**:
     - [x] *Join spaces and group conversations*
     - [x] *Receive 1:1 messages*
   * **Connection settings**: Select **Cloud Pub/Sub** and enter:
     ```
     projects/<YOUR_PROJECT_ID>/topics/monarch-chat-incoming
     ```
   * **Visibility**: Set to your Google Workspace domain or personal Google account.
3. Click **Save**.
4. Run the zero-ingress Chat worker (connects outbound via gRPC pull with zero listening ports):
   ```bash
   python -m app.chat_worker --project YOUR_PROJECT_ID --subscription monarch-chat-sub
   ```
5. In Google Chat, search for `FinSage` and add it to your space or direct message thread.

---

## Security & Privacy Architecture

* **Zero-Ingress Posture**: No public listening HTTP endpoints are exposed. Daily ingestion syncs and anomaly scans execute via ephemeral **Cloud Run Jobs** invoked by Cloud Scheduler over Google's internal APIs using short-lived OAuth 2.0 tokens (`monarch-scheduler-sa`).
* **Private Pub/Sub Chat Integration**: Google Chat events are routed through Cloud Pub/Sub topic `monarch-chat-incoming` and pulled outbound by `app/chat_worker.py`. Unauthenticated public internet traffic is dropped at Google's edge.
* **Local-First & Private**: Financial data is synced directly between Monarch Money and your private BigQuery dataset within your own GCP project boundary. No data is shared with third-party aggregators.
* **Deterministic Guardrails**: Gemini operates with Automatic Function Calling over a single read-only SQL tool. Destructive operations (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`) are strictly forbidden by IAM and query syntax checks.
* **Secret Isolation**: Passwords, MFA tokens, and webhook URLs are stored exclusively in **Google Cloud Secret Manager** and accessed dynamically at runtime using short-lived tokens.
* **Principle of Least Privilege**: The Cloud Run runtime identity holds `roles/bigquery.dataEditor` (strictly scoped for idempotent dataset table syncs/merges), `roles/bigquery.jobUser`, and `roles/secretmanager.secretAccessor`. Gemini's tool calls are restricted to read-only `SELECT` queries across pre-aggregated analytical views with regex keyword enforcement.

---

## Operating Cost Profile

The architecture runs comfortably within Google Cloud's **Always Free Tier**:

| Resource | Configuration | Estimated Cost / Month |
| :--- | :--- | :--- |
| **Cloud Run** | 1 GiB RAM, 1 vCPU, scale-to-zero when idle | **$0.00** (Within 2M free requests & 360k vCPU-sec) |
| **BigQuery Ingestion & Storage** | < 100 MB transaction history | **$0.00** (Free Tier covers 10 GB active storage) |
| **BigQuery SQL Analytics** | Partitioned views, ~1.5 GB scanned / month | **$0.00** (Free Tier covers 1 TB query analysis / month) |
| **Cloud Scheduler** | 2 scheduled cron jobs | **$0.00** (Free Tier covers 3 free jobs / month) |
| **Artifact Registry** | Container image storage (~150 MB) | **~$0.15** |
| **Secret Manager** | Active secret versions | **~$0.22** (Free Tier covers 6 active secret versions) |
| **Total Operating Cost** | | **~$0.37 / month** |

---

## Strategic Roadmap & Planned Expansion

FinSage continues to evolve from daily transaction aggregation into an autonomous family chief financial officer. The roadmap integrates mathematical modeling and policy structures:

* **PR 7: Autonomous Anomaly Detection & Cash Sweeps**
  * Immediate cash recovery via duplicate transaction detection (`v_duplicate_charges`).
  * Free trial conversion monitoring (`v_new_subscriptions`) within the first 35 days.
  * Adaptive category outlier alerting (`v_category_spend_baseline`) using 6-month statistical rolling standard deviations.
  * Paycheck surplus detection (`v_paycheck_surplus_sweep`) to calculate instant debt sweeps above safe operating buffers.
  * Sinking fund radar (`v_annual_bill_radar`) pre-warning 30 days before major annual/quarterly bills.
* **PR 8: Macro-Economic Context & Rate Benchmarking (FRED Integration)**
  * Automated Federal Reserve Economic Data ingestion (`FRED` series: `DFF` Fed Funds, `MORTGAGE30US`) into BigQuery.
  * Real-time benchmark synchronization for variable-rate credit facilities (e.g. Prime rate shifts on active HELOC balances).
* **PR 9: Multimodal Vision IRS Tax Document Parsing**
  * Automated extraction and staging of IRS tax documents (Form 1040, W-2, 1099-INT, 1099-DIV, Schedule A) pasted directly into Google Chat.
  * Pre-fills annual tax projections and monitors itemized deduction pacing (SALT & mortgage interest caps).
* **PR 10: 3-Tier Policy Guardrail System**
  * Formalized action approval taxonomy across all advisor mutations:
    * `none`: Passive anomaly scans, read-only analytical queries, and background syncs.
    * `user`: Side-effecting operations requiring interactive Google Chat Card v2 confirmation (Snooze alerts, budget category modifications).
    * `advisor`: Hard safety blocks on destructive actions (account unlinking, bulk transaction deletions).
* **PR 11: Periodic Executive Family CFO Briefing**
  * Scheduled weekly (Sunday) and monthly macro rollups in Google Chat: net worth delta ($\Delta$), fixed overhead vs discretionary burn rate, and debt acceleration milestone progress.
* **PR 12: Investment Allocation & Portfolio Drift Monitoring**
  * BigQuery analytical view tracking asset allocation across Equities, Fixed Income, Cash, and Real Estate to detect cash drag and target portfolio drift.

---

## Acknowledgments

The idea, architecture, and design patterns for this project took inspiration from:
* [Concept and Architecture Reference](https://chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339)
* [`hammem/monarchmoney`](https://github.com/hammem/monarchmoney) — Python client library and authentication flow for Monarch Money.
* [`6missedcalls/personal-finance-skill`](https://github.com/6missedcalls/personal-finance-skill) — Inspiration for multi-tier policy guardrails (`none`, `user`, `advisor`), macro-economic benchmark grounding (FRED API series), deterministic financial brief formatting, and structured IRS tax form parsing schemas.

---

## License

This project is licensed under the [MIT License](LICENSE).

