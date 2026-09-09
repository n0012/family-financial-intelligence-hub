#!/usr/bin/env bash
# ==============================================================================
# Sync Local Secrets (.env.local) to Google Cloud Secret Manager
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env.local"

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: $ENV_FILE not found. Copy .env.example to .env.local and populate it."
  exit 1
fi

# Load variables
set -a
source "$ENV_FILE"
set +a

PROJECT="${PROJECT_ID:-your-gcp-project-id}"

echo "================================================================="
echo "Syncing Secrets to Google Cloud Secret Manager"
echo "Project: $PROJECT"
echo "================================================================="

# 1. Monarch Email
if [ -n "$MONARCH_EMAIL" ]; then
  echo -n "Updating secret [monarch-email] -> $MONARCH_EMAIL... "
  echo -n "$MONARCH_EMAIL" | gcloud secrets versions add monarch-email \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

# 2. Monarch Password
if [ -n "$MONARCH_PASSWORD" ]; then
  echo -n "Updating secret [monarch-password]... "
  echo -n "$MONARCH_PASSWORD" | gcloud secrets versions add monarch-password \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
else
  echo "Skipping [monarch-password]: Value is empty in .env.local"
fi

# 3. Monarch MFA Secret
if [ -n "$MONARCH_MFA_SECRET" ]; then
  echo -n "Updating secret [monarch-mfa-secret]... "
  echo -n "$MONARCH_MFA_SECRET" | gcloud secrets versions add monarch-mfa-secret \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

# 4. Alert Webhook URL (Google Chat / Slack)
if [ -n "$ALERT_WEBHOOK_URL" ]; then
  echo -n "Updating secret [alert-webhook-url]... "
  echo -n "$ALERT_WEBHOOK_URL" | gcloud secrets versions add alert-webhook-url \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
else
  echo "Skipping [alert-webhook-url]: Value is empty in .env.local"
fi

# 5. Gemini API Key (Optional, falls back to Vertex AI if empty)
if [ -n "$GEMINI_API_KEY" ]; then
  echo -n "Updating secret [gemini-api-key]... "
  gcloud secrets describe gemini-api-key --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create gemini-api-key --replication-policy="automatic" --project="$PROJECT" --quiet >/dev/null
  echo -n "$GEMINI_API_KEY" | gcloud secrets versions add gemini-api-key \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

# 6. Account Attribute Overrides (Optional JSON)
if [ -n "$ACCOUNT_OVERRIDES_JSON" ]; then
  echo -n "Updating secret [account-overrides]... "
  gcloud secrets describe account-overrides --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create account-overrides --replication-policy="automatic" --project="$PROJECT" --quiet >/dev/null
  echo -n "$ACCOUNT_OVERRIDES_JSON" | gcloud secrets versions add account-overrides \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

# 7. Decommissioned Account IDs (Optional CSV or JSON list)
if [ -n "$DECOMMISSIONED_ACCOUNT_IDS" ]; then
  echo -n "Updating secret [decommissioned-account-ids]... "
  gcloud secrets describe decommissioned-account-ids --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create decommissioned-account-ids --replication-policy="automatic" --project="$PROJECT" --quiet >/dev/null
  echo -n "$DECOMMISSIONED_ACCOUNT_IDS" | gcloud secrets versions add decommissioned-account-ids \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

# 8. Excluded Institutions (Optional CSV list)
if [ -n "$EXCLUDED_INSTITUTIONS" ]; then
  echo -n "Updating secret [excluded-institutions]... "
  gcloud secrets describe excluded-institutions --project="$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create excluded-institutions --replication-policy="automatic" --project="$PROJECT" --quiet >/dev/null
  echo -n "$EXCLUDED_INSTITUTIONS" | gcloud secrets versions add excluded-institutions \
    --data-file=- --project="$PROJECT" --quiet > /dev/null
  echo "Done."
fi

echo ""
echo "================================================================="
echo "Secrets sync completed successfully!"
echo "Cloud Run service [${SERVICE_NAME:-monarch-gemini-wrapper}] automatically"
echo "picks up the latest secret versions on its next request/execution."
echo "================================================================="
