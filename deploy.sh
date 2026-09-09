#!/usr/bin/env bash
set -euo pipefail

# Configuration Defaults (override by setting env vars before running)
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-monarch-gemini-wrapper}"
RUN_SA="${RUN_SA:-monarch-gemini-run@$PROJECT_ID.iam.gserviceaccount.com}"
DATASET_ID="${DATASET_ID:-family_finance}"

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID is not set. Run 'gcloud config set project <your-project-id>' or export PROJECT_ID." >&2
  exit 1
fi

echo "=========================================="
echo "Deploying Monarch Gemini & BigQuery Hub"
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"
echo "Service: $SERVICE"
echo "Dataset: $DATASET_ID"
echo "=========================================="

# 1. Enable Required GCP APIs
echo "Enabling Google Cloud APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  geminidataanalytics.googleapis.com \
  iam.googleapis.com \
  --project="$PROJECT_ID"

# 2. Create Service Account if not exists
if ! gcloud iam service-accounts describe "$RUN_SA" --project="$PROJECT_ID" &>/dev/null; then
  echo "Creating service account $RUN_SA..."
  gcloud iam service-accounts create monarch-gemini-run \
    --display-name="Monarch Gemini Cloud Run Service Account" \
    --project="$PROJECT_ID"
fi

# 3. Grant BigQuery Permissions to Cloud Run SA
echo "Granting BigQuery roles to service account..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUN_SA" \
  --role="roles/bigquery.dataEditor" \
  --quiet

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUN_SA" \
  --role="roles/bigquery.jobUser" \
  --quiet

# 4. Ensure Secrets Exist in Secret Manager
create_secret_if_missing() {
  local secret_name="$1"
  local prompt_msg="$2"
  if ! gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
    echo "Creating secret: $secret_name"
    read -rsp "$prompt_msg: " secret_val
    echo ""
    printf '%s' "$secret_val" | gcloud secrets create "$secret_name" --data-file=- --project="$PROJECT_ID"
  else
    echo "Secret $secret_name already exists."
  fi
}

create_secret_if_missing "monarch-email" "Enter Monarch login email"
create_secret_if_missing "monarch-password" "Enter Monarch password"
create_secret_if_missing "monarch-mfa-secret" "Enter Monarch TOTP 2FA Secret Key (leave empty if not enabled)"

# Auto-generate GEMINI_WRAPPER_KEY if missing
if ! gcloud secrets describe "gemini-wrapper-key" --project="$PROJECT_ID" &>/dev/null; then
  echo "Generating random API key for gemini-wrapper-key..."
  python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" | \
    gcloud secrets create "gemini-wrapper-key" --data-file=- --project="$PROJECT_ID"
fi

# 5. Grant Service Account Access to Secrets
echo "Granting Secret Accessor permissions..."
for SECRET in monarch-email monarch-password monarch-mfa-secret gemini-wrapper-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$RUN_SA" \
    --role="roles/secretmanager.secretAccessor" \
    --project="$PROJECT_ID" \
    --quiet
done

# 6. Initialize BigQuery Dataset and Tables/Views
echo "Initializing BigQuery dataset '$DATASET_ID'..."
if ! bq show --project_id="$PROJECT_ID" "$DATASET_ID" &>/dev/null; then
  bq --location="$REGION" mk --dataset "${PROJECT_ID}:${DATASET_ID}"
fi

echo "Applying BigQuery schema and analytical views from schema.sql..."
bq query --use_legacy_sql=false --project_id="$PROJECT_ID" < schema.sql

# 7. Deploy to Cloud Run from Source
echo "Deploying Cloud Run service..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --set-env-vars PROJECT_ID="$PROJECT_ID",BQ_DATASET_ID="$DATASET_ID" \
  --set-secrets MONARCH_EMAIL=monarch-email:latest,MONARCH_PASSWORD=monarch-password:latest,MONARCH_MFA_SECRET=monarch-mfa-secret:latest,GEMINI_WRAPPER_KEY=gemini-wrapper-key:latest \
  --project="$PROJECT_ID"

SERVICE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project="$PROJECT_ID" --format='value(status.url)')"
WRAPPER_KEY="$(gcloud secrets versions access latest --secret=gemini-wrapper-key --project="$PROJECT_ID")"

echo "=========================================="
echo "Deployment Complete!"
echo "Service URL: $SERVICE_URL"
echo ""
echo "Next steps:"
echo "1. Run initial BigQuery sync:"
echo "   curl -X POST -H \"X-API-Key: $WRAPPER_KEY\" \"$SERVICE_URL/sync/bigquery?days_back=180\""
echo ""
echo "2. Provision Conversational Analytics Agent:"
echo "   python3 create_ca_agent.py"
echo "=========================================="
