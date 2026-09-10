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

# 7. Provision Pub/Sub for Zero-Ingress Google Chat
echo "Configuring Cloud Pub/Sub for Google Chat..."
if ! gcloud pubsub topics describe monarch-chat-incoming --project="$PROJECT_ID" &>/dev/null; then
  echo "Creating topic monarch-chat-incoming..."
  gcloud pubsub topics create monarch-chat-incoming --project="$PROJECT_ID"
fi

# Allow Google Chat API to publish to the topic
gcloud pubsub topics add-iam-policy-binding monarch-chat-incoming \
  --member="serviceAccount:chat-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --project="$PROJECT_ID" \
  --quiet

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud pubsub topics add-iam-policy-binding monarch-chat-incoming \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --project="$PROJECT_ID" \
  --quiet

if ! gcloud pubsub subscriptions describe monarch-chat-sub --project="$PROJECT_ID" &>/dev/null; then
  echo "Creating pull subscription monarch-chat-sub..."
  gcloud pubsub subscriptions create monarch-chat-sub \
    --topic=monarch-chat-incoming \
    --ack-deadline=60 \
    --project="$PROJECT_ID"
fi

# 8. Deploy Cloud Run Service (Private & Locked Down: Internal Ingress & No Unauthenticated Access)
echo "Deploying Cloud Run service with internal ingress and strict authentication..."
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --no-allow-unauthenticated \
  --ingress internal \
  --service-account "$RUN_SA" \
  --set-env-vars PROJECT_ID="$PROJECT_ID",BQ_DATASET_ID="$DATASET_ID",ENABLE_CHAT_PULL_WORKER=true,CHAT_SUBSCRIPTION=monarch-chat-sub \
  --set-secrets MONARCH_EMAIL=monarch-email:latest,MONARCH_PASSWORD=monarch-password:latest,MONARCH_MFA_SECRET=monarch-mfa-secret:latest,GEMINI_WRAPPER_KEY=gemini-wrapper-key:latest \
  --project="$PROJECT_ID"

# 9. Deploy Cloud Run Jobs for Batch Operations (Zero Ingress, Zero HTTP Ports)
echo "Deploying Cloud Run Jobs for batch ingestion and alerts..."
IMAGE="$(gcloud run services describe "$SERVICE" --region "$REGION" --project="$PROJECT_ID" --format='value(spec.template.spec.containers[0].image)')"

# A. Sync Job
gcloud run jobs deploy monarch-sync-job \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$RUN_SA" \
  --set-env-vars PROJECT_ID="$PROJECT_ID",BQ_DATASET_ID="$DATASET_ID" \
  --set-secrets MONARCH_EMAIL=monarch-email:latest,MONARCH_PASSWORD=monarch-password:latest,MONARCH_MFA_SECRET=monarch-mfa-secret:latest \
  --command "python" \
  --args "job.py,sync,--days-back,30" \
  --max-retries 1 \
  --task-timeout 600s \
  --project="$PROJECT_ID"

# B. Alerts Job
gcloud run jobs deploy monarch-alerts-job \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$RUN_SA" \
  --set-env-vars PROJECT_ID="$PROJECT_ID",BQ_DATASET_ID="$DATASET_ID" \
  --set-secrets MONARCH_EMAIL=monarch-email:latest,MONARCH_PASSWORD=monarch-password:latest,MONARCH_MFA_SECRET=monarch-mfa-secret:latest \
  --command "python" \
  --args "job.py,alerts" \
  --max-retries 1 \
  --task-timeout 300s \
  --project="$PROJECT_ID"

# 10. Grant Cloud Scheduler SA permissions to execute Jobs
SCHEDULER_SA="monarch-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud run jobs add-iam-policy-binding monarch-sync-job \
  --region="$REGION" \
  --member="serviceAccount:$SCHEDULER_SA" \
  --role="roles/run.developer" \
  --project="$PROJECT_ID" \
  --quiet

gcloud run jobs add-iam-policy-binding monarch-alerts-job \
  --region="$REGION" \
  --member="serviceAccount:$SCHEDULER_SA" \
  --role="roles/run.developer" \
  --project="$PROJECT_ID" \
  --quiet

echo "=========================================="
echo "Deployment Complete! (Zero Public Ingress Posture)"
echo "Cloud Run Service: Locked down (--ingress internal, --no-allow-unauthenticated)"
echo "Cloud Run Jobs:    monarch-sync-job, monarch-alerts-job"
echo "Pub/Sub Topic:     projects/$PROJECT_ID/topics/monarch-chat-incoming"
echo "Pub/Sub Sub:       projects/$PROJECT_ID/subscriptions/monarch-chat-sub"
echo ""
echo "Next steps:"
echo "1. Run initial BigQuery sync via private Cloud Run Job:"
echo "   gcloud run jobs execute monarch-sync-job --args=\"job.py,sync,--days-back,180\" --region=\"$REGION\" --project=\"$PROJECT_ID\""
echo ""
echo "2. Configure Google Chat API connection:"
echo "   In Google Cloud Console -> Google Chat API -> Configuration:"
echo "   Select 'Cloud Pub/Sub' and set Topic name: projects/$PROJECT_ID/topics/monarch-chat-incoming"
echo "=========================================="
