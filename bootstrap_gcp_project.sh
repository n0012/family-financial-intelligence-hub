#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Bootstrap Script: GCP Project & Infrastructure Foundation
# ==============================================================================

echo "============================================================"
echo "Family Financial Intelligence Hub Bootstrap"
echo "============================================================"

# Ensure gcloud is authenticated
CURRENT_ACCOUNT="$(gcloud config get-value account 2>/dev/null || echo '')"
if [ -z "$CURRENT_ACCOUNT" ]; then
  echo "Error: No active gcloud account found. Please run 'gcloud auth login' first."
  exit 1
fi
echo "Active gcloud account: $CURRENT_ACCOUNT"

# Project ID Configuration
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"
REGION="${REGION:-us-central1}"

echo "Target Project ID: $PROJECT_ID"
echo "Target Region:     $REGION"

# 1. Create project if it doesn't already exist
if ! gcloud projects describe "$PROJECT_ID" &>/dev/null; then
  echo "Project $PROJECT_ID does not exist. Creating..."
  ORG_ID="$(gcloud organizations list --format="value(ID)" --limit=1 2>/dev/null || echo '')"
  if [ -n "$ORG_ID" ]; then
    gcloud projects create "$PROJECT_ID" --name="Family Financial Hub" --organization="$ORG_ID"
  else
    gcloud projects create "$PROJECT_ID" --name="Family Financial Hub"
  fi
  
  # Detect billing account and link
  BILLING_ACCOUNT="$(gcloud billing accounts list --filter=open=true --format="value(name)" --limit=1 2>/dev/null || echo '')"
  if [ -n "$BILLING_ACCOUNT" ]; then
    echo "Linking billing account: $BILLING_ACCOUNT..."
    gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
  else
    echo "⚠️ Warning: No open billing account detected automatically. Please link billing in GCP Console."
  fi
else
  echo "Project $PROJECT_ID already exists."
fi

gcloud config set project "$PROJECT_ID"

# 2. Check/Install Terraform locally or prepare Cloud Build
if ! command -v terraform &>/dev/null; then
  echo "Terraform is not installed locally. Installing via brew..."
  if command -v brew &>/dev/null; then
    brew install terraform
  else
    echo "Error: Homebrew not found. Please install terraform manually or use Cloud Build."
    exit 1
  fi
fi

# 3. Initialize & Apply Terraform Foundation
echo "Initializing Terraform in ./terraform..."
cd terraform
cat > terraform.tfvars <<EOF
project_id          = "$PROJECT_ID"
region              = "$REGION"
service_name        = "monarch-gemini-wrapper"
artifact_repo_name  = "monarch-repo"
bigquery_dataset_id = "family_finance"
sync_schedule       = "0 4 * * *"
alert_schedule      = "0 8 * * *"
time_zone           = "America/New_York"
EOF

export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)"
terraform init
echo "Applying Terraform infrastructure..."
terraform apply -auto-approve

cd ..

# 4. Populate Monarch Secrets in Secret Manager
echo "============================================================"
echo "Configuring Monarch Money Secrets in Secret Manager"
echo "============================================================"
set_secret_if_provided() {
  local secret_name="$1"
  local env_val="${2:-}"
  local prompt_label="$3"

  if [ -n "$env_val" ]; then
    printf '%s' "$env_val" | gcloud secrets versions add "$secret_name" --data-file=- --project="$PROJECT_ID"
    echo "Configured $secret_name from environment."
  elif [ -t 0 ]; then
    current_val="$(gcloud secrets versions access latest --secret="$secret_name" --project="$PROJECT_ID" 2>/dev/null || echo '')"
    if [ "$current_val" = "placeholder" ] || [ "$current_val" = "placeholder@example.com" ]; then
      read -rsp "$prompt_label (or press Enter to configure later): " val
      echo ""
      if [ -n "$val" ]; then
        printf '%s' "$val" | gcloud secrets versions add "$secret_name" --data-file=- --project="$PROJECT_ID"
        echo "Stored $secret_name."
      fi
    else
      echo "Secret $secret_name is already active."
    fi
  else
    echo "Secret $secret_name is initialized with placeholder (update later via gcloud secrets versions add $secret_name)."
  fi
}

set_secret_if_provided "monarch-email" "${MONARCH_EMAIL:-}" "Enter Monarch Login Email"
set_secret_if_provided "monarch-password" "${MONARCH_PASSWORD:-}" "Enter Monarch Standalone Password"
set_secret_if_provided "monarch-mfa-secret" "${MONARCH_MFA_SECRET:-}" "Enter Monarch TOTP 2FA Secret Key"
set_secret_if_provided "alert-webhook-url" "${ALERT_WEBHOOK_URL:-}" "Enter Discord/Slack Webhook URL for proactive alerts"

# 5. Execute First Cloud Build Deployment
echo "============================================================"
echo "Submitting Application Build via Cloud Build"
echo "============================================================"
gcloud builds submit --config=cloudbuild.yaml --project="$PROJECT_ID"

# 6. Retrieve Deployed Service URL & Verification
SERVICE_URL="$(gcloud run services describe monarch-gemini-wrapper --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
WRAPPER_KEY="$(gcloud secrets versions access latest --secret=gemini-wrapper-key --project="$PROJECT_ID")"

# 7. Configure Cloud Scheduler Jobs (100% Free Tier)
echo "============================================================"
echo "Configuring Scheduled Automation (Daily Sync & Weekly Alerts)"
echo "============================================================"

# A. Daily Ingestion Sync (4:00 AM)
if ! gcloud scheduler jobs describe monarch-daily-sync --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  echo "Creating daily sync job: monarch-daily-sync..."
  gcloud scheduler jobs create http monarch-daily-sync \
    --location="$REGION" \
    --schedule="0 4 * * *" \
    --uri="$SERVICE_URL/sync/bigquery?days_back=30" \
    --http-method=POST \
    --headers="X-API-Key=$WRAPPER_KEY" \
    --time-zone="America/New_York" \
    --project="$PROJECT_ID"
fi

# B. Daily Proactive Advisory & Fix Scan (Daily at 8:00 AM)
if ! gcloud scheduler jobs describe monarch-daily-alerts --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
  echo "Creating daily advisory alert job: monarch-daily-alerts..."
  gcloud scheduler jobs create http monarch-daily-alerts \
    --location="$REGION" \
    --schedule="0 8 * * *" \
    --uri="$SERVICE_URL/advisor/scan-alerts" \
    --http-method=POST \
    --headers="X-API-Key=$WRAPPER_KEY" \
    --time-zone="America/New_York" \
    --project="$PROJECT_ID"
fi

echo "============================================================"
echo "Setup Complete!"
echo "Service URL: $SERVICE_URL"
echo ""
echo "Trigger initial historical sync:"
echo "curl -X POST -H \"X-API-Key: $WRAPPER_KEY\" \"$SERVICE_URL/sync/bigquery?days_back=180\""
echo ""
echo "Run an immediate proactive alert scan:"
echo "curl -X POST -H \"X-API-Key: $WRAPPER_KEY\" \"$SERVICE_URL/advisor/scan-alerts\""
echo ""
echo "Deploy Conversational Analytics Agent:"
echo "PROJECT_ID=$PROJECT_ID python3 create_ca_agent.py"
echo "============================================================"
