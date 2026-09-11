# Configuration & Custom Rates

FinSage provides a flexible, secure configuration engine supporting local files, environment variables, and Google Cloud Secret Manager.

---

## 1. Resolution Precedence

FinSage evaluates configuration using a tiered resolution order tailored for local agility and production security:

### Application Settings (`rates`, `account_overrides`, `decommissioned_account_ids`, `excluded_institutions`)
1. **Local Configuration File**: `config.yaml` or `config.json` in repository root (evaluated first for local development and rapid tuning).
2. **Google Cloud Secret Manager**: Production JSON secrets (e.g., `account-overrides`, `rates-config`).
3. **Environment Variables**: Container and CI/CD runtime variables (e.g., `ACCOUNT_OVERRIDES_JSON`, `RATES_CONFIG_JSON`).
4. **Internal Defaults**: Safe zero/fallback defaults.

### Runtime Secrets (`MONARCH_EMAIL`, `MONARCH_PASSWORD`, `ALERT_WEBHOOK_URL`, `GEMINI_WRAPPER_KEY`)
1. **Google Cloud Secret Manager**: Fetched securely via ADC if running inside GCP (`PROJECT_ID` set).
2. **Environment Variables**: Checked if Secret Manager secret is unset (used for local `.env.local` execution).

---

## 2. Debt APR Resolution & Provenance

To guarantee mathematical consistency across BigQuery views, ingestion strictly resolves APRs using the following hierarchy:
1. **Explicit Account Override**: Matching `account_id` in `config.yaml` or `ACCOUNT_OVERRIDES_JSON`.
2. **Institution Feed APR**: Real-time `interestRate` reported directly by Monarch Money / Plaid.
3. **Configured Subtype Defaults**:
   - `default_heloc_apr` (fallback: 6.75%): Applied only if account name or subtype indicates a HELOC.
   - `default_mortgage_apr` (fallback: 3.50%): Applied only if account name or subtype indicates a Mortgage.
   - `default_debt_apr`: Applied **strictly to term loans** (`type_str == "loan"`).
4. **Credit Cards**: Credit cards (`type_str == "credit"`) are **never** assigned an automatic default APR. Credit cards only enter interest-bearing debt calculations if Monarch explicitly reports a positive APR or if explicitly specified in `account_overrides`.

In BigQuery, `v_debt_daily_cost` tracks `is_apr_estimated`, which evaluates to `FALSE` whenever a rate is verified or explicit, and `TRUE` only if unrated.

---

## 3. Local Configuration (`config.yaml`)

Copy the example template to get started:
```bash
cp config.example.yaml config.yaml
```

The `config.yaml` file is gitignored to protect sensitive account IDs and private notes.

### Example Configuration:
```yaml
# config.yaml

# 1. Baseline Interest Rates (APRs)
# Applied when Monarch Money does not report an interest rate.
rates:
  default_heloc_apr: 0.0675          # Default APR for HELOCs (6.75%)
  default_mortgage_apr: 0.0350       # Default APR for Mortgages (3.50%)
  default_debt_apr: 0.0750           # Baseline fallback for other debt/loans

# 2. Specific Account Overrides
# Map Monarch account IDs to explicit rates, custom names, or credit limits.
account_overrides:
  "123456789012345678":
    name: "Primary Home Equity Line of Credit"
    interest_rate: 0.0675            # 6.75% APR

# 3. Decommissioned / Closed Accounts
# Monarch account IDs to omit from BigQuery historical sync.
decommissioned_account_ids:
  - "987654321098765432"
  - "876543210987654321"

# 4. Excluded Institutions
# Keywords of defunct or merged institutions to filter out.
excluded_institutions:
  - "defunct_bank_name"
  - "legacy_credit_union"

# 5. Household Partner Aliases
# Used in chat synopsis cards and weekly briefings.
partners:
  partner_a: "Partner A"
  partner_b: "Partner B"
```

During ingestion, the synchronization pipeline automatically injects these APRs into BigQuery `raw_accounts.interest_rate`, powering the deterministic calculations in `v_debt_daily_cost` and `v_debt_summary`.

---

## 3. Environment Variables & Secret Manager Reference

In Google Cloud production, credentials and configuration are stored in **Secret Manager** (secret names in parentheses):

| Variable / Secret Name | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `MONARCH_EMAIL` (`monarch-email`) | Monarch Money account email | Yes | — |
| `MONARCH_PASSWORD` (`monarch-password`) | Monarch Money account password | Yes | — |
| `MONARCH_MFA_SECRET` (`monarch-mfa-secret`) | Base32 TOTP secret string (for automated 2FA) | Optional | `NONE` |
| `GEMINI_API_KEY` (`gemini-api-key`) | Google AI Studio Gemini API Key (or uses Vertex AI ADC) | Optional | ADC / Vertex AI |
| `ALERT_WEBHOOK_URL` (`alert-webhook-url`) | Google Chat incoming webhook URL for scheduled alerts | Optional | — |
| `GEMINI_WRAPPER_KEY` (`gemini-wrapper-key`) | Shared API secret for securing `/sync` and `/advisor` endpoints | Yes | Auto-provisioned |
| `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` | Google Cloud Project ID | Yes | Auto-detected |
| `BQ_DATASET_ID` | BigQuery dataset name | No | `family_finance` |
| `REGION` | GCP deployment region | No | `us-central1` |
| `ACCOUNT_OVERRIDES_JSON` (`account-overrides`) | JSON string of account overrides (e.g. custom APRs) | Optional | `{}` |
| `DECOMMISSIONED_ACCOUNT_IDS` (`decommissioned-account-ids`) | CSV or JSON array of closed account IDs to ignore | Optional | `[]` |
| `EXCLUDED_INSTITUTIONS` (`excluded-institutions`) | CSV of institution keywords to exclude (e.g. merged banks) | Optional | `[]` |

---

## 4. Synchronizing Secrets to Google Cloud

To push your `.env.local` variables directly into Google Cloud Secret Manager:

```bash
cp .env.example .env.local
# Edit .env.local with your credentials
./scripts/sync_secrets_to_gcp.sh
```
