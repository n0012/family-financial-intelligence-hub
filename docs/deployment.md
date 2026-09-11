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
* **Cloud Pub/Sub**: Inbound chat event topic (`monarch-chat-incoming`) and pull subscription (`monarch-chat-sub`).
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

Trigger the automated build, container packaging, and zero-ingress deployment:

```bash
gcloud builds submit --config=cloudbuild.yaml --project=YOUR_PROJECT_ID
```

### Cloud Build Pipeline:
1. Compiles and tags the production container image in Artifact Registry.
2. Deploys the hardened Cloud Run service (`--ingress internal`, `--no-allow-unauthenticated`).
3. Deploys the batch Cloud Run Jobs (`monarch-sync-job` and `monarch-alerts-job`).
4. Executes and updates BigQuery analytical views and DDLs from `schema.sql`.

---

## 5. Google Chat App Configuration (Zero Ingress)

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
4. Start the zero-ingress Chat worker:
   ```bash
   python -m app.chat_worker --project YOUR_PROJECT_ID --subscription monarch-chat-sub
   ```
5. In Google Chat, search for `FinSage` and add it to your space or direct message thread.

---

## 6. Scheduled Batch Jobs (Cloud Scheduler)

FinSage runs two scheduled background jobs via Cloud Scheduler:
* **Daily Ingestion (`monarch-daily-sync`)**: Runs daily at 04:00 AM to synchronize accounts, balances, and transactions into BigQuery.
* **Daily Advisor Alerts (`monarch-daily-advisor-alerts`)**: Runs daily at 08:00 AM to generate the morning brief card and evaluate proactive spend alerts.

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
| **Cloud Run** | 1 GiB RAM, 1 vCPU, scales to zero | **$0.00** (Free Tier covers 2M requests & 360k vCPU-s) |
| **BigQuery Storage** | Active financial ledger (< 100 MB) | **$0.00** (Free Tier covers 10 GB storage) |
| **BigQuery Analysis** | Partitioned views (~1.5 GB query scan/mo) | **$0.00** (Free Tier covers 1 TB queries/mo) |
| **Cloud Scheduler** | 2 scheduled cron jobs | **$0.00** (Free Tier covers 3 jobs/mo) |
| **Cloud Pub/Sub** | Inbound chat messages (< 10,000/mo) | **$0.00** (Free Tier covers 10 GB messages/mo) |
| **Artifact Registry** | Container image storage (~150 MB) | **~$0.15** |
| **Secret Manager** | Active secret versions | **~$0.22** (Free Tier covers 6 active secret versions) |
| **Total Operating Cost** | | **~$0.37 / month** |

---

## 8. Local Development & Testing

Run tests and style checks locally:

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run lint checks
./.venv/bin/ruff check .

# Run pytest test suite (148 tests)
PYTHONPATH=. ./.venv/bin/pytest -v
```
