-- ==============================================================================
-- Monarch Money -> BigQuery Schema & Financial Analytics Views
-- Dataset: family_finance
-- ==============================================================================

-- 1. Raw Accounts Table
CREATE TABLE IF NOT EXISTS `family_finance.raw_accounts` (
    account_id STRING NOT NULL,
    account_name STRING,
    display_name STRING,
    type_name STRING,         -- depository, credit, loan, investment, etc.
    subtype_name STRING,      -- checking, credit_card, heloc, mortgage, etc.
    current_balance NUMERIC,
    available_balance NUMERIC,
    credit_limit NUMERIC,
    interest_rate NUMERIC,    -- e.g. 0.0825 for 8.25% APR on HELOC
    institution_name STRING,
    is_asset BOOL,
    updated_at TIMESTAMP
);

-- 2. Raw Transactions Table
CREATE TABLE IF NOT EXISTS `family_finance.raw_transactions` (
    transaction_id STRING NOT NULL,
    account_id STRING NOT NULL,
    transaction_date DATE NOT NULL,
    amount NUMERIC NOT NULL,   -- Positive = expense / debit, Negative = income / credit
    merchant_name STRING,
    clean_merchant_name STRING,
    category_id STRING,
    category_name STRING,
    notes STRING,
    is_recurring BOOL,
    pending BOOL,
    updated_at TIMESTAMP
);

-- Staging Transactions Table (for idempotent MERGE / dedup)
CREATE TABLE IF NOT EXISTS `family_finance.staging_transactions` (
    transaction_id STRING NOT NULL,
    account_id STRING NOT NULL,
    transaction_date DATE NOT NULL,
    amount NUMERIC NOT NULL,
    merchant_name STRING,
    clean_merchant_name STRING,
    category_id STRING,
    category_name STRING,
    notes STRING,
    is_recurring BOOL,
    pending BOOL,
    updated_at TIMESTAMP
);

-- 3. Raw Categories Table
CREATE TABLE IF NOT EXISTS `family_finance.raw_categories` (
    category_id STRING NOT NULL,
    category_name STRING NOT NULL,
    group_name STRING,        -- Fixed, Discretionary, Debt, Income, Transfer
    is_income BOOL,
    monthly_budget NUMERIC,
    updated_at TIMESTAMP
);

-- 4. Alert Suppression & Snooze Table (PR 6)
CREATE TABLE IF NOT EXISTS `family_finance.alert_suppression` (
    alert_key STRING NOT NULL,
    alert_type STRING NOT NULL,
    suppressed_until TIMESTAMP NOT NULL,
    created_at TIMESTAMP,
    reason STRING
);

-- ==============================================================================
-- Analytical & Optimization Views for Conversational Analytics Agent
-- ==============================================================================

-- VIEW A: Active Subscriptions & Recurring Debits
-- Automatically detects monthly, quarterly, and annual recurring charges,
-- calculates run rates, and flags price increases.
CREATE OR REPLACE VIEW `family_finance.v_active_subscriptions` AS
WITH recurring_stats AS (
    SELECT
        COALESCE(clean_merchant_name, merchant_name) AS merchant,
        category_name,
        COUNT(*) AS charge_count,
        ROUND(AVG(ABS(amount)), 2) AS avg_charge,
        ROUND(MIN(ABS(amount)), 2) AS min_charge,
        ROUND(MAX(ABS(amount)), 2) AS max_charge,
        MIN(transaction_date) AS first_seen,
        MAX(transaction_date) AS last_seen,
        DATE_DIFF(MAX(transaction_date), MIN(transaction_date), DAY) AS span_days,
        ROUND(DATE_DIFF(MAX(transaction_date), MIN(transaction_date), DAY) / NULLIF(COUNT(*) - 1, 0), 1) AS avg_cadence_days
    FROM `family_finance.raw_transactions`
    WHERE amount < 0
      AND pending = FALSE
      AND LOWER(category_name) NOT IN ('transfer', 'transfers', 'credit card payment', 'credit card payments', 'loan payment', 'balance transfers')
      AND LOWER(COALESCE(clean_merchant_name, merchant_name)) NOT LIKE '%transfer%'
      AND (
          is_recurring = TRUE
          OR LOWER(category_name) IN ('subscriptions', 'phone', 'internet & cable', 'fitness', 'home security', 'utilities')
      )
      AND transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 18 MONTH)
    GROUP BY 1, 2
    HAVING charge_count >= 2
)
SELECT
    merchant,
    category_name,
    charge_count,
    avg_charge,
    min_charge,
    max_charge,
    CASE 
        WHEN max_charge > min_charge THEN TRUE 
        ELSE FALSE 
    END AS has_price_increased,
    CASE
        WHEN avg_cadence_days BETWEEN 25 AND 35 THEN 'MONTHLY'
        WHEN avg_cadence_days BETWEEN 80 AND 100 THEN 'QUARTERLY'
        WHEN avg_cadence_days BETWEEN 340 AND 390 THEN 'ANNUAL'
        ELSE 'OTHER'
    END AS billing_cadence,
    CASE
        WHEN avg_cadence_days BETWEEN 25 AND 35 THEN ROUND(avg_charge * 12, 2)
        WHEN avg_cadence_days BETWEEN 80 AND 100 THEN ROUND(avg_charge * 4, 2)
        WHEN avg_cadence_days BETWEEN 340 AND 390 THEN avg_charge
        ELSE ROUND(avg_charge * charge_count, 2)
    END AS estimated_annual_cost,
    first_seen,
    last_seen,
    avg_cadence_days
FROM recurring_stats
WHERE avg_cadence_days BETWEEN 25 AND 390
ORDER BY estimated_annual_cost DESC;

-- VIEW B: Discretionary Spend vs Fixed Family Overhead
CREATE OR REPLACE VIEW `family_finance.v_spend_classification` AS
SELECT
    FORMAT_DATE('%Y-%m', transaction_date) AS month,
    category_name,
    CASE
        WHEN LOWER(category_name) IN ('mortgage', 'rent', 'daycare', 'childcare', 'groceries', 'utilities', 'insurance', 'medical', 'heloc payment', 'dentist', 'auto maintenance')
            THEN 'FIXED_OVERHEAD'
        WHEN LOWER(category_name) IN ('income', 'paycheck', 'bonus', 'interest', 'dividend')
            THEN 'INCOME'
        WHEN LOWER(category_name) IN ('transfer', 'credit card payment')
            THEN 'TRANSFER'
        ELSE 'DISCRETIONARY_VARIABLE'
    END AS spend_type,
    ROUND(SUM(ABS(amount)), 2) AS total_amount,
    COUNT(*) AS transaction_count
FROM `family_finance.raw_transactions`
WHERE pending = FALSE
GROUP BY 1, 2, 3;

-- VIEW C1: Account Classification & Lifecycle Intelligence (Dynamic Resolution)
CREATE OR REPLACE VIEW `family_finance.v_account_lifecycle` AS
WITH institution_activity AS (
    SELECT
        a.institution_name,
        MAX(t.transaction_date) AS institution_latest_tx_date,
        COUNT(t.transaction_id) AS institution_tx_count
    FROM `family_finance.raw_accounts` a
    LEFT JOIN `family_finance.raw_transactions` t ON a.account_id = t.account_id
    GROUP BY 1
),
account_tx AS (
    SELECT
        account_id,
        MAX(transaction_date) AS last_tx_date,
        COUNT(*) AS tx_total,
        COUNTIF(transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 45 DAY)) AS tx_45d,
        COUNTIF(transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)) AS tx_90d
    FROM `family_finance.raw_transactions`
    WHERE pending = FALSE
    GROUP BY account_id
),
scored AS (
    SELECT
        a.account_id,
        a.display_name,
        a.institution_name,
        a.type_name,
        a.subtype_name,
        CASE
            WHEN LOWER(a.subtype_name) IN ('heloc', 'home_equity', 'home_equity_line_of_credit') 
                 OR LOWER(a.display_name) LIKE '%heloc%' OR LOWER(a.display_name) LIKE '%home equity%' THEN 'HOME_EQUITY_LINE'
            WHEN LOWER(a.subtype_name) IN ('credit_card', 'credit') OR LOWER(a.type_name) = 'credit' THEN 'CREDIT_CARD'
            WHEN LOWER(a.subtype_name) IN ('checking', 'savings', 'money_market') OR LOWER(a.type_name) = 'depository' THEN 'DEPOSITORY'
            WHEN LOWER(a.subtype_name) IN ('mortgage', 'loan') OR LOWER(a.type_name) = 'loan' THEN 'TERM_LOAN'
            WHEN LOWER(a.subtype_name) IN ('brokerage', 'ira', 'roth', '401k', 'st_401k') OR LOWER(a.type_name) = 'investment' THEN 'INVESTMENT'
            ELSE 'OTHER'
        END AS account_class,
        ROUND(ABS(a.current_balance), 2) AS current_balance,
        ROUND(a.credit_limit, 2) AS credit_limit,
        a.interest_rate AS apr,
        tx.last_tx_date,
        COALESCE(tx.tx_total, 0) AS tx_total,
        COALESCE(tx.tx_45d, 0) AS tx_45d,
        COALESCE(tx.tx_90d, 0) AS tx_90d,
        ia.institution_latest_tx_date,
        COALESCE(ia.institution_tx_count, 0) AS institution_tx_count,
        a.updated_at,
        CAST(a.account_id AS INT64) AS account_numeric_id
    FROM `family_finance.raw_accounts` a
    LEFT JOIN account_tx tx ON a.account_id = tx.account_id
    LEFT JOIN institution_activity ia ON a.institution_name = ia.institution_name
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY account_class
            ORDER BY
                (ABS(current_balance) > 0) DESC,
                COALESCE(last_tx_date, DATE '1970-01-01') DESC,
                COALESCE(institution_latest_tx_date, DATE '1970-01-01') DESC,
                institution_tx_count DESC,
                account_numeric_id DESC
        ) AS rank_in_class,
        COUNT(*) OVER (PARTITION BY account_class) AS peers_in_class
    FROM scored
)
SELECT
    account_id,
    display_name,
    institution_name,
    account_class,
    type_name,
    subtype_name,
    current_balance,
    credit_limit,
    apr,
    CASE WHEN rank_in_class = 1 THEN 'PRIMARY' ELSE 'SUPERSEDED' END AS lifecycle_status,
    (rank_in_class = 1) AS is_primary_active,
    last_tx_date,
    tx_total,
    tx_45d,
    tx_90d,
    institution_latest_tx_date,
    institution_tx_count,
    rank_in_class,
    peers_in_class,
    updated_at
FROM ranked;

-- VIEW C2: HELOC Debt Balance & Daily Interest Burden (Joined with Lifecycle Intelligence)
CREATE OR REPLACE VIEW `family_finance.v_heloc_daily_cost` AS
SELECT
    account_id,
    display_name,
    institution_name,
    current_balance,
    credit_limit,
    ROUND(credit_limit - current_balance, 2) AS available_credit,
    -- apr is synced from config file (rates block or account_overrides) or institution data
    apr,
    ROUND((current_balance * COALESCE(apr, 0.0)) / 365, 2) AS daily_interest_cost,
    ROUND((current_balance * COALESCE(apr, 0.0)) / 12, 2) AS monthly_interest_cost,
    ROUND(500 * COALESCE(apr, 0.0), 2) AS annual_interest_saved_per_500_monthly_reduction,
    apr IS NULL AS is_apr_estimated,
    lifecycle_status,
    is_primary_active,
    last_tx_date,
    institution_latest_tx_date,
    institution_tx_count,
    updated_at
FROM `family_finance.v_account_lifecycle`
WHERE account_class = 'HOME_EQUITY_LINE'
ORDER BY is_primary_active DESC;

-- ==============================================================================
-- Optimization Views (Targeted Spend Reduction & Leakage Identification)
-- ==============================================================================

-- VIEW D: Food Efficiency Ratio (Groceries vs Dining Out & Delivery)
-- Pinpoints high-cost restaurant and delivery habits vs grocery meal preparation
CREATE OR REPLACE VIEW `family_finance.v_food_efficiency` AS
WITH monthly_food AS (
    SELECT
        FORMAT_DATE('%Y-%m', transaction_date) AS month,
        CASE
            WHEN LOWER(category_name) LIKE '%grocer%' THEN 'GROCERIES'
            WHEN LOWER(category_name) IN ('restaurants', 'dining out', 'fast food', 'coffee shops', 'bars', 'restaurants & bars')
                 OR LOWER(merchant_name) LIKE '%doordash%'
                 OR LOWER(merchant_name) LIKE '%ubereats%'
                 OR LOWER(merchant_name) LIKE '%grubhub%' THEN 'DINING_AND_DELIVERY'
            ELSE 'OTHER_FOOD'
        END AS food_type,
        SUM(ABS(amount)) AS total_spend
    FROM `family_finance.raw_transactions`
    WHERE (LOWER(category_name) LIKE '%food%' 
        OR LOWER(category_name) LIKE '%dining%' 
        OR LOWER(category_name) LIKE '%grocer%'
        OR LOWER(category_name) LIKE '%restaurant%'
        OR LOWER(category_name) LIKE '%coffee%'
        OR LOWER(category_name) LIKE '%bar%')
      AND amount < 0
      AND pending = FALSE
    GROUP BY 1, 2
)
SELECT
    month,
    ROUND(SUM(CASE WHEN food_type = 'GROCERIES' THEN total_spend ELSE 0 END), 2) AS grocery_spend,
    ROUND(SUM(CASE WHEN food_type = 'DINING_AND_DELIVERY' THEN total_spend ELSE 0 END), 2) AS dining_delivery_spend,
    ROUND(SUM(total_spend), 2) AS total_food_spend,
    ROUND(
        100.0 * SAFE_DIVIDE(
            SUM(CASE WHEN food_type = 'DINING_AND_DELIVERY' THEN total_spend ELSE 0 END),
            SUM(total_spend)
        ), 1
    ) AS dining_percentage_of_food_budget
FROM monthly_food
GROUP BY 1
ORDER BY month DESC;

-- VIEW E: Micro-Transaction Leakage (< $35 frequent convenience spend)
-- Detects "death by a thousand cuts" recurring habits (coffee, apps, snacks)
CREATE OR REPLACE VIEW `family_finance.v_micro_transaction_leakage` AS
SELECT
    COALESCE(clean_merchant_name, merchant_name) AS merchant,
    category_name,
    COUNT(*) AS frequency_90d,
    ROUND(AVG(ABS(amount)), 2) AS avg_ticket,
    ROUND(SUM(ABS(amount)), 2) AS total_spend_90d,
    ROUND((SUM(ABS(amount)) / 3) * 12, 2) AS annualized_run_rate
FROM `family_finance.raw_transactions`
WHERE amount < 0
  AND ABS(amount) < 35.00
  AND pending = FALSE
  AND transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY 1, 2
HAVING frequency_90d >= 4
ORDER BY total_spend_90d DESC;

-- VIEW F: Subscription Category Overlap & Redundancy
-- Groups concurrent subscriptions by domain to flag overlapping services
CREATE OR REPLACE VIEW `family_finance.v_subscription_overlap` AS
SELECT
    category_name,
    COUNT(*) AS active_subscriptions_count,
    ROUND(SUM(estimated_annual_cost), 2) AS category_annual_run_rate,
    ROUND(SUM(avg_charge), 2) AS combined_monthly_cost,
    STRING_AGG(merchant, ', ' ORDER BY estimated_annual_cost DESC) AS active_services
FROM `family_finance.v_active_subscriptions`
GROUP BY 1
HAVING active_subscriptions_count >= 2
ORDER BY combined_monthly_cost DESC;
