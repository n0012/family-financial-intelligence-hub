# Deployment & Operations Guide

This guide covers deploying FinSage to **Google Cloud Platform (GCP)** using Terraform, Cloud Build, Cloud Run, BigQuery, and Pub/Sub.

---

## 1. Prerequisites

1. A [Google Cloud Platform](https://cloud.google.com/) account with an active billing project.
2. [Google Cloud SDK (`gcloud`)](https://cloud.google.com/sdk) installed and authenticated:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
3. [Terraform](https://www.terraform.io/) (>= 1.5) or [OpenTofu](https://opentofu.org/) installed.
4. An active [Monarch Money](https://www.monarchmoney.com/) account.

---

## 2. Infrastructure Provisioning via Terraform

Terraform provisions all core GCP infrastructure:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Update project_id and region in terraform.tfvars
terraform init
terraform apply
cd ..
```

### Provisioned Resources:
* **Artifact Registry**: Docker container repository (`monarch-repo`).
* **BigQuery Dataset**: Data warehouse dataset (`family_finance`).
* **Cloud Pub/Sub**: Inbound chat event topic (`monarch-chat-incoming`) and push subscription (`monarch-chat-push` → `/chat/pubsub`).
* **Cloud Scheduler**: Cron schedules for daily ingestion (`monarch-daily-sync`) and morning alerts (`monarch-daily-advisor-alerts`).
* **Secret Manager**: Secure containers for all credentials.
* **IAM Service Accounts**: Runtime identity (`monarch-gemini-run`) and scheduler identity (`monarch-scheduler-sa`).

---

## 3. Secret Management

Create your local credentials file and synchronize it to Secret Manager:

```bash
cp .env.example .env.local
# Fill in MONARCH_EMAIL, MONARCH_PASSWORD, MONARCH_MFA_SECRET, and ALERT_WEBHOOK_URL
./scripts/sync_secrets_to_gcp.sh
```

---

## 4. Build & Deploy via Google Cloud Build

Trigger the automated build, container packaging, and private-ingress deployment:

```bash
gcloud builds submit --config=cloudbuild.yaml --project=YOUR_PROJECT_ID
```

### Cloud Build Pipeline:
1. Compiles and tags the production container image in Artifact Registry.
2. Deploys the hardened Cloud Run service (`--ingress internal`, `--no-allow-unauthenticated`, `--min-instances 0`, request-based CPU).
3. Deploys the batch Cloud Run Jobs (`monarch-sync-job` and `monarch-alerts-job`).
4. Executes and updates BigQuery analytical views and DDLs from `schema.sql`.

> **Note:** `cloudbuild.yaml` uses `--set-env-vars`, which replaces every environment variable on the service. Pass your real `_MEMORY_BANK` and `_DEFAULT_USER_EMAIL` substitutions, or update an existing deployment image-only so its current values are kept:
> ```bash
> gcloud run deploy monarch-gemini-wrapper --region=us-central1 --image=IMAGE
> gcloud run jobs update monarch-alerts-job --region=us-central1 --image=IMAGE
> gcloud run jobs update monarch-sync-job --region=us-central1 --image=IMAGE
> ```
> Build `IMAGE` from a clean checkout (for example `git archive main`) so untracked local files never reach Cloud Build.

---

## 5. Google Chat App Configuration (Pub/Sub Push)

1. Open the [Google Cloud Console → Google Chat API](https://console.cloud.google.com/apis/api/chat.googleapis.com).
2. Navigate to **Configuration** and configure:
   * **App name**: `FinSage`
   * **Avatar URL**: (Optional) URL to your bot avatar image.
   * **Description**: `Interactive family financial advisor powered by Monarch Money, BigQuery, and Gemini Flash.`
   * **Functionality**:
     - [x] *Join spaces and group conversations*
     - [x] *Receive 1:1 messages*
   * **Connection settings**: Select **Cloud Pub/Sub** and enter:
     ```
     projects/<YOUR_PROJECT_ID>/topics/monarch-chat-incoming
     ```
   * **Visibility**: Set to your Google Workspace domain or personal Google account.
3. Click **Save**.
4. Confirm the push subscription points at the service (`scripts/deploy.sh` and Terraform create it):
   ```bash
   gcloud pubsub subscriptions describe monarch-chat-push --format='value(pushConfig.pushEndpoint)'
   ```
   The run service account needs `roles/run.invoker` on the service, and `CHAT_AUDIENCE` must match the subscription's OIDC audience (the service URL).
5. In Google Chat, search for `FinSage` and add it to your space or direct message thread.

---

## 6. Scheduled Batch Jobs (Cloud Scheduler)

FinSage runs two scheduled background jobs via Cloud Scheduler:
* **Daily Ingestion (`monarch-daily-sync`)**: Runs daily at 04:00 AM to synchronize accounts, balances, and transactions into BigQuery.
* **Daily Brief (`monarch-daily-advisor-alerts`)**: Runs daily at 08:00 AM and posts the summarized daily brief (`python -m app.job alerts`). On Mondays it also posts the weekly digest.

#### How the daily brief stays fresh
The brief computes candidate findings each morning (category shifts against the median of the prior three 4-week windows, merchants visited twice as often as usual, first-ever merchants, accounts that have stopped reporting, and one-time alerts such as duplicate charges). It shows only the top two by dollar impact and records them in `brief_history`. A trend finding is not repeated for 14 days, a stale-account warning for 7, and a one-time alert for a year. Goal pacing reads spending caps and HELOC payoff dates from long-term memory, so the job needs `VERTEX_MEMORY_BANK_NAME` and `DEFAULT_USER_EMAIL` set. The exhaustive alert list is still available: `/alerts` in Chat replies with every alert and a snooze button, and `python -m app.job full-scan` posts the alerts with the older synopsis card to the space.

You can trigger a job manually at any time:
```bash
gcloud run jobs execute monarch-sync-job --region=us-central1
gcloud run jobs execute monarch-alerts-job --region=us-central1
```

---

## 7. Operating Cost Profile

The entire system is designed to operate within Google Cloud's **Always Free Tier**:

| Service | Tier / Usage | Estimated Monthly Cost |
| :--- | :--- | :--- |
| **Cloud Run** | 1 GiB RAM, 1 vCPU, `--min-instances 0`, request-based CPU | **$0.00** (Free Tier covers 2M requests & 180k vCPU-s) |
| **BigQuery Storage** | Active financial ledger (< 100 MB) | **$0.00** (Free Tier covers 10 GB storage) |
| **BigQuery Analysis** | Partitioned views (~1.5 GB query scan/mo) | **$0.00** (Free Tier covers 1 TB queries/mo) |
| **Cloud Scheduler** | 2 scheduled cron jobs | **$0.00** (Free Tier covers 3 jobs/mo) |
| **Cloud Pub/Sub** | Inbound chat messages (< 10,000/mo) | **$0.00** (Free Tier covers 10 GB messages/mo) |
| **Artifact Registry** | Container image storage (~150 MB) | **~$0.15** |
| **Secret Manager** | Active secret versions | **~$0.22** (Free Tier covers 6 active secret versions) |
| **Total Operating Cost** | | **~$0.37 / month** plus Gemini API usage |

> **Keep `--min-instances 0`.** An always-on instance (`--min-instances 1 --no-cpu-throttling`) bills a full vCPU and its memory every second of the month, which outweighs everything else in this table.

---

## 8. Security & Privacy Architecture

FinSage is built from the ground up for strict family financial confidentiality:

* **Zero Public Ingress**: The Cloud Run webhook worker operates with `--ingress internal` and `--no-allow-unauthenticated`. Inbound Google Chat interactions arrive via an OIDC-authenticated **Cloud Pub/Sub push subscription**, so no public endpoint is exposed to the internet.
* **Confidential Secret Storage**: Monarch credentials, MFA secrets, and API keys reside in **Google Cloud Secret Manager**. Secrets are fetched via Application Default Credentials (ADC) in memory and never logged or written to disk.
* **Zero-PII Git Standard**: Real account numbers, balances, merchants, lender identities, and household names are prohibited in commits, tests, documentation, and pull requests. `scripts/check_private_data.py` enforces this in git hooks and CI; see [`AGENTS.md`](../AGENTS.md).
* **Guarded Financial Mutations**: While FinSage can recategorize transactions, split line items, and add notes, all mutations enforce an interactive **Two-Phase Confirmation** flow in Google Chat. The bot will never alter Monarch Money records without explicit user approval.
* **Deterministic Isolation**: Calculations (balances, burns, APRs, debt carry, safety buffers) are computed strictly in deterministic BigQuery SQL. LLMs (Gemini Flash) are never permitted to estimate or hallucinate financial figures.

---

## 9. Architectural Boundaries

```
[Monarch Money API]
       │
       ▼ (Automated Pull / Ingestion)
[Cloud Run Sync Job]
       │
       ▼ (Append / Upsert)
[BigQuery Raw Storage]
 ├── raw_transactions
 ├── raw_accounts
 └── raw_categories
       │
       ▼ (Deterministic SQL Transformations)
[18+ Analytical Views]
 ├── v_fixed_overhead_burn
 ├── v_debt_daily_cost & v_debt_summary
 ├── v_paycheck_surplus_allocation
 └── v_active_subscriptions
       │
       ├─────────────────────────────────┐
       ▼ (Proactive Rule Engines)         ▼ (Advisory Queries & Tools)
[Scheduled Daily Brief Job]         [Chat Advisor (Gemini Flash)]
       │                                 ▲ (Pub/Sub Push)
       ▼ (Webhook Dispatch)              │
[Google Chat Space / Direct Message / Two-Phase Mutation Approval]
```

---

## 10. Local Development & Testing

Run tests and style checks locally:

```bash
# Create the virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt -r requirements-dev.txt

# Enable the private-data git hooks (once per clone)
git config core.hooksPath .githooks

# Run lint checks
uv run ruff check .

# Run the test suite
uv run python -m pytest -v

# Scan the whole tree for personal or confidential data
python3 scripts/check_private_data.py --all
```
