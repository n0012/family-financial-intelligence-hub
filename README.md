# Family Financial Intelligence Hub
**Automated Personal Finance & Spend Optimization via Monarch Money, Google BigQuery & Gemini 3.8 Flash**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/Framework-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Google Cloud](https://img.shields.io/badge/Cloud-Google%20Cloud%20Platform-4285F4.svg)](https://cloud.google.com/)
[![Google BigQuery](https://img.shields.io/badge/Warehouse-Google%20BigQuery-669DF6.svg)](https://cloud.google.com/bigquery)
[![Gemini 3.8 Flash](https://img.shields.io/badge/AI%20Model-Gemini%203.8%20Flash-8E24AA.svg)](https://deepmind.google/technologies/gemini/)
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC.svg)](https://www.terraform.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<p align="center">
  <img src="static/workflow.png" alt="Automated Family Financial AI Workflow" width="100%">
</p>

An enterprise-grade, serverless family financial advisor and spend optimization hub deployed to **Google Cloud Platform**. It bridges **Monarch Money**'s GraphQL API directly into **Google BigQuery** data warehouse models, powered by a bidirectional **Google Chat Copilot** running **Gemini 3.8 Flash** with **Automatic Function Calling (AFC)** and **Multimodal Vision**.

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

## Architectural Workflow

```mermaid
flowchart TD
    subgraph Scheduling ["Automated Schedules (Cloud Scheduler)"]
        CronSync["Daily Ingestion (04:00 AM)<br/>monarch-daily-sync"]
        CronAlert["Daily Proactive Scan (08:00 AM)<br/>monarch-daily-advisor-alerts"]
    end

    subgraph IngestionLayer ["Serverless Microservice (Cloud Run: FastAPI)"]
        FastAPI["FastAPI Orchestrator<br/>(Python 3.11)"]
        MMClient["MonarchMoney GraphQL Client<br/>+ Automated Base32 TOTP (pyotp)"]
        AdvisorEngine["Proactive Spend Alert Engine"]
        MediaDownloader["Google Chat Media Downloader<br/>(OAuth2 Bot Token)"]
    end

    subgraph DataWarehouse ["Google BigQuery Data Warehouse"]
        RawTables["Raw Tables:<br/>• raw_accounts<br/>• raw_transactions<br/>• raw_categories<br/>• staging_transactions"]
        Views["Analytical Optimization Views:<br/>• v_account_lifecycle (Active vs Superseded)<br/>• v_heloc_daily_cost (Carrying Cost & Debt Sweeps)<br/>• v_active_subscriptions (Cadence & Price Creep)<br/>• v_subscription_overlap (Redundant Services)<br/>• v_food_efficiency (Groceries vs Dining/Delivery)<br/>• v_micro_transaction_leakage (Sub-$35 Convenience Leaks)<br/>• v_spend_classification (Fixed vs Discretionary)"]
    end

    subgraph Intelligence ["Gemini 3.8 Flash Brain & Chat Interface"]
        GeminiFlash["Gemini 3.8 Flash<br/>• MEDIUM Thinking Budget<br/>• Automatic Function Calling (AFC)<br/>• Read-only BigQuery Tool"]
        MultimodalVision["Multimodal Ingestion<br/>(Pasted PNG/JPG Screenshots &amp; Plans)"]
        GoogleChat["Google Chat Space &amp; 1:1 DMs<br/>• Native Cards v2 Alerts<br/>• Bidirectional Thread Replies"]
    end

    CronSync -->|"POST /sync/bigquery"| FastAPI
    FastAPI --> MMClient
    MMClient -->|"GraphQL Extraction"| RawTables
    RawTables --> Views

    CronAlert -->|"POST /advisor/scan-alerts"| AdvisorEngine
    Views --> AdvisorEngine
    AdvisorEngine -->|"Card v2 Notification"| GoogleChat

    GoogleChat -->|"Webhook Event: Text & Images"| FastAPI
    FastAPI --> MediaDownloader
    MediaDownloader --> MultimodalVision
    MultimodalVision --> GeminiFlash
    GeminiFlash -->|"Tool Call: run_readonly_sql"| Views
    Views -->|"Query Results"| GeminiFlash
    GeminiFlash -->|"Synthesized Advisory Response"| GoogleChat
```

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
| **`v_active_subscriptions`** | Autodetects recurring billing cadences (monthly, quarterly, annual), projects annual run-rates, and flags **price creep** by comparing average vs maximum historical charges. |
| **`v_subscription_overlap`** | Aggregates concurrent active subscriptions within the same category (e.g. streaming, cloud storage, fitness) to highlight redundancy. |
| **`v_food_efficiency`** | Calculates the monthly ratio between grocery purchases and dining out / food delivery markups (DoorDash, UberEats, Grubhub). |
| **`v_micro_transaction_leakage`** | Flags frequent sub-$35 convenience transactions (coffee shops, convenience stores, app purchases) and calculates their annualized drain. |
| **`v_spend_classification`** | Classifies all monthly outflows into Fixed Overhead vs Discretionary spend to evaluate baseline burn rate. |

---

## Interactive Google Chat Copilot

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

### 3. Chat Commands & Shortcuts
* `/sync` — Pulls latest transactions from Monarch Money into BigQuery immediately.
* `/alerts` — Triggers an on-demand scan across all BigQuery optimization views and posts the alert summary.
* `/help` — Displays quick reference guides and sample prompts.

---

## Repository Structure

```
monarch-gemini/
├── main.py                     # FastAPI application, Monarch client, Gemini Brain, & Google Chat webhook
├── schema.sql                  # BigQuery schema definitions & analytical optimization views
├── Dockerfile                  # Production container definition (Python 3.11-slim)
├── cloudbuild.yaml             # Google Cloud Build CI/CD pipeline definition
├── config.example.yaml         # YAML configuration template (rates, account overrides, exclusions)
├── config.example.json         # JSON configuration template
├── sync_secrets_to_gcp.sh      # Automated secret synchronization from .env.local to Secret Manager
├── create_ca_agent.py          # Google Cloud Conversational Analytics Agent deployment script
├── app_streamlit.py            # Optional Streamlit visual dashboard
├── requirements.txt            # Python dependencies
├── .env.example                # Template for environment configuration
└── terraform/                  # Infrastructure as Code (Terraform / OpenTofu)
    ├── main.tf                 # Cloud Run, BigQuery, Artifact Registry, Cloud Scheduler, IAM
    ├── variables.tf            # Configurable deployment variables
    ├── outputs.tf              # Service URLs, service accounts, and dataset outputs
    └── terraform.tfvars.example # Example variable values
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
  default_heloc_apr: 0.0675          # Default APR for HELOCs (6.75%)
  default_mortgage_apr: 0.0350       # Default APR for Mortgages (3.50%)
  default_debt_apr: 0.0750           # Baseline fallback for loans/debt

account_overrides:
  "123456789012345678":
    name: "Primary Home Equity Line of Credit"
    interest_rate: 0.0675            # Explicit rate for a specific account

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
* **Cloud Run Service** with least-privilege IAM service account (`monarch-gemini-run`)
* **Cloud Scheduler Jobs** for daily sync (4:00 AM) and proactive advisory alerts (8:00 AM)
* **Secret Manager** containers for all credentials

---

### 3. Populate Secrets

Copy the example environment template and enter your credentials:
```bash
cp .env.example .env.local
# Fill in MONARCH_EMAIL, MONARCH_PASSWORD, MONARCH_MFA_SECRET, and ALERT_WEBHOOK_URL in .env.local
```

Synchronize the secrets to Google Cloud Secret Manager:
```bash
./sync_secrets_to_gcp.sh
```

---

### 4. Build & Deploy Microservice via Cloud Build

Trigger the automated build, container packaging, and zero-downtime deployment:
```bash
gcloud builds submit --config=cloudbuild.yaml --project=YOUR_PROJECT_ID
```

Cloud Build automatically:
1. Builds and tags the container image in Artifact Registry.
2. Deploys the service to Cloud Run.
3. Applies all analytical views and tables in `schema.sql` to BigQuery.
4. Performs an automated health check smoke test on the live endpoint.

---

### 5. Configure the Google Chat App

1. Go to the [Google Cloud Console → Google Chat API](https://console.cloud.google.com/apis/api/chat.googleapis.com).
2. Click **Configuration** and fill in:
   * **App name**: `Family Finance Copilot`
   * **Avatar URL**: `https://<YOUR-CLOUD-RUN-URL>/avatar.png`
   * **Description**: `Interactive family financial advisor powered by Monarch Money, BigQuery, and Gemini 3.8 Flash.`
   * **Functionality**:
     - [x] *Join spaces and group conversations*
     - [x] *Receive 1:1 messages*
   * **Connection settings**: Select **HTTP endpoint** and enter:
     ```
     https://<YOUR-CLOUD-RUN-URL>/chat/event
     ```
   * **Visibility**: Set to your Google Workspace domain or personal Google account.
3. Click **Save**.
4. In Google Chat, search for `Family Finance Copilot` and add it to your space or direct message thread.

---

## Security & Privacy Architecture

* **Local-First & Private**: Financial data is synced directly between Monarch Money and your private BigQuery dataset within your own GCP project boundary. No data is shared with third-party aggregators.
* **Deterministic Guardrails**: Gemini operates with Automatic Function Calling over a single read-only SQL tool. Destructive operations (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`) are strictly forbidden by IAM and query syntax checks.
* **Secret Isolation**: Passwords, MFA tokens, and webhook URLs are stored exclusively in **Google Cloud Secret Manager** and accessed dynamically at runtime using short-lived OAuth tokens.
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

## Acknowledgments

The idea and approach for this project originated and took inspiration from:
* [Concept and Architecture Reference](https://chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339)
* [`hammem/monarchmoney`](https://github.com/hammem/monarchmoney)


---

## License

This project is licensed under the [MIT License](LICENSE).
