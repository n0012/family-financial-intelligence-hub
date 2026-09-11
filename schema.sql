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
    amount NUMERIC NOT NULL,   -- Negative = expense / debit, Positive = income / credit (Monarch standard)
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

-- 5. Mutation & Guardrail Audit Log Table (PR 10)
CREATE TABLE IF NOT EXISTS `family_finance.mutation_audit_log` (
    mutation_id STRING NOT NULL,
    timestamp TIMESTAMP NOT NULL,
    user_email STRING NOT NULL,
    action_type STRING NOT NULL,     -- 'RECATEGORIZE_TRANSACTION', 'SNOOZE_ALERT', 'STORE_PREFERENCE'
    target_id STRING NOT NULL,       -- transaction_id, alert_key, preference_key
    previous_value STRING,           -- JSON/string of prior state
    new_value STRING,                -- JSON/string of proposed/applied state
    status STRING NOT NULL,          -- 'SUCCESS', 'REJECTED', 'FAILED', 'RATE_LIMITED', 'NOOP', 'CANCELLED'
    signature_valid BOOL,            -- TRUE if HMAC verified, FALSE if signature failed/missing, NULL if N/A
    details STRING,                  -- notes, reason for rejection or failure
    created_at TIMESTAMP
);

-- ==============================================================================
-- Analytical & Optimization Views for Conversational Analytics Agent
-- ==============================================================================

-- VIEW A0: Merchant Functional Domain Classifier
-- Maps each merchant to the functional service domain it actually competes in. The
-- aggregator's flat "Subscriptions" category lumps a password manager, an AI assistant
-- and a magazine into one bucket; two services are only redundant if they substitute
-- for each other, so overlap detection must key off this domain, never off the category.
--
-- `disposition` constrains what advice is even valid for a merchant:
--   CANCELLABLE        discretionary service; "cancel or rotate" is a real action.
--   RESHOPPABLE        underwritten or contractual (insurance, telecom, broadband);
--                      "re-shop at renewal" is valid, "cancel to save" is not.
--   ESSENTIAL_METERED  regulated consumption billing (electric, gas, water). No
--                      substitute and no cancel action; routed to the seasonal baseline.
--   NOT_A_SUBSCRIPTION episodic services (vet, clinic) that repeat without being a plan.
CREATE OR REPLACE VIEW `family_finance.v_merchant_domain` AS
WITH merchant_profile AS (
    -- Exactly one row per merchant. Monarch re-categorises merchants over time (the same
    -- streaming service can sit in "Subscriptions" for a year then "Entertainment"), so
    -- anything keyed on (merchant, category) fragments one service into several.
    SELECT
        COALESCE(clean_merchant_name, merchant_name) AS merchant,
        category_name,
        COUNT(*) AS observations
    FROM `family_finance.raw_transactions`
    WHERE amount < 0
      AND pending = FALSE
      AND COALESCE(clean_merchant_name, merchant_name) IS NOT NULL
    GROUP BY merchant, category_name
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY merchant
        ORDER BY observations DESC, category_name
    ) = 1
),
classified AS (
    SELECT
        merchant,
        category_name,
        CASE
            -- Regulated / metered essentials. Category wins over merchant text here.
            WHEN LOWER(category_name) IN ('utilities', 'electric', 'gas & electric', 'gas', 'water', 'sewage', 'trash', 'natural gas')
              OR REGEXP_CONTAINS(LOWER(merchant), r'(energy|electric|utilit|water district|water dept|sewer|natural gas|waste management|sanitation)')
                THEN 'UTILITY_METERED'

            -- Underwritten contracts: re-shoppable at renewal, never "cancellable".
            WHEN LOWER(category_name) LIKE '%insurance%'
              OR REGEXP_CONTAINS(LOWER(merchant), r'(insurance|assurance|underwrit|casualty|state farm|geico|allstate|progressive|nationwide)')
                THEN 'INSURANCE'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(veterin|animal hospital|animal clinic|companion animal)')
              OR LOWER(category_name) IN ('veterinary', 'vet')
                THEN 'VET_CARE'
            WHEN LOWER(category_name) IN ('phone', 'mobile phone', 'cell phone')
              OR REGEXP_CONTAINS(LOWER(merchant), r'(verizon|at&t|t-mobile|mint mobile|visible|google fi|cricket wireless)')
                THEN 'TELECOM_MOBILE'
            WHEN LOWER(category_name) IN ('internet & cable', 'internet', 'cable')
                THEN 'INTERNET_BROADBAND'
            WHEN LOWER(category_name) IN ('home security', 'security')
              OR REGEXP_CONTAINS(LOWER(merchant), r'(simplisafe|adt\b|ring protect|vivint|frontpoint)')
                THEN 'HOME_SECURITY'

            -- Discretionary digital services, grouped by what they substitute for.
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(netflix|hulu|disney|max\b|hbo|peacock|paramount|prime video|apple tv|starz|showtime|philo|fubo|sling|youtube tv)')
                THEN 'VIDEO_STREAMING'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(spotify|pandora|tidal|sirius|apple music|youtube music|deezer)')
                THEN 'AUDIO_AND_MEDIA'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(audible|kindle|scribd|libro\.fm|everand|kobo)')
                THEN 'AUDIOBOOKS_READING'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(magazine|the week|nytimes|new york times|wsj|washington post|the athletic|economist|substack)')
                THEN 'NEWS_AND_READING'
            -- Hosted consumer storage only. A self-hosted NAS OS licence is not a
            -- substitute for cloud storage and must not pair with it as "redundant".
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(google one|icloud|dropbox|onedrive|backblaze|carbonite|sync\.com)')
                THEN 'CLOUD_STORAGE'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(openai|chatgpt|anthropic|claude|perplexity|midjourney|copilot)')
                THEN 'AI_PRODUCTIVITY'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(1password|lastpass|dashlane|bitwarden|nordpass|keeper security)')
                THEN 'PASSWORD_MANAGER'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(nordvpn|expressvpn|mullvad|proton vpn|surfshark|private internet access)')
                THEN 'VPN_PRIVACY'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(canva|adobe|figma|lightroom|affinity)')
                THEN 'CREATIVE_SOFTWARE'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(plex|channels|emby|jellyfin|unraid|tablo)')
                THEN 'HOME_MEDIA_SERVER'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(xbox game pass|playstation plus|nintendo switch online|steam\b)')
                THEN 'GAMING'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(hellofresh|blue apron|home chef|green chef|factor|every ?plate|sunbasket)')
                THEN 'MEAL_KIT_DELIVERY'
            WHEN LOWER(category_name) IN ('fitness', 'gym', 'gyms & fitness')
              OR REGEXP_CONTAINS(LOWER(merchant), r'(gym|fitness|athletic club|peloton|crossfit|yoga|pilates|health club)')
                THEN 'FITNESS'
            WHEN REGEXP_CONTAINS(LOWER(merchant), r'(amazon prime|prime membership|costco membership|walmart\+|instacart\+)')
                THEN 'ECOMMERCE_MEMBERSHIP'

            ELSE 'UNCLASSIFIED'
        END AS domain
    FROM merchant_profile
)
SELECT
    merchant,
    category_name,
    domain,
    CASE domain
        WHEN 'UTILITY_METERED' THEN 'ESSENTIAL_METERED'
        WHEN 'VET_CARE' THEN 'NOT_A_SUBSCRIPTION'
        WHEN 'INSURANCE' THEN 'RESHOPPABLE'
        WHEN 'TELECOM_MOBILE' THEN 'RESHOPPABLE'
        WHEN 'INTERNET_BROADBAND' THEN 'RESHOPPABLE'
        WHEN 'HOME_SECURITY' THEN 'RESHOPPABLE'
        WHEN 'UNCLASSIFIED' THEN 'UNKNOWN'
        ELSE 'CANCELLABLE'
    END AS disposition,
    -- Only domains whose members genuinely substitute for one another. Telecom and
    -- broadband are excluded: two carriers in a household are two lines, not a
    -- duplicate service. UNCLASSIFIED is excluded because overlap requires positive
    -- evidence of duplication, never the mere absence of a classification.
    domain IN (
        'VIDEO_STREAMING', 'AUDIO_AND_MEDIA', 'AUDIOBOOKS_READING', 'NEWS_AND_READING',
        'CLOUD_STORAGE', 'AI_PRODUCTIVITY', 'PASSWORD_MANAGER', 'VPN_PRIVACY',
        'CREATIVE_SOFTWARE', 'HOME_MEDIA_SERVER', 'GAMING', 'MEAL_KIT_DELIVERY', 'FITNESS'
    ) AS is_overlap_eligible,
    -- Owning two video streamers is normal; three is a rotation opportunity.
    CASE WHEN domain = 'VIDEO_STREAMING' THEN 3 ELSE 2 END AS overlap_min_count
FROM classified;

-- VIEW A1: Recurring Charge Ledger (incidental point-of-sale purchases removed)
-- A merchant with a membership also takes one-off POS money: a monthly gym plan and an
-- $8 cafe drink post under the same merchant. Grouping them and comparing MIN to MAX
-- manufactures a 1,600% "price hike".
--
-- Two-stage isolation:
--   1. Flag priority. If the aggregator marks >= 2 charges at a merchant as recurring,
--      trust ONLY those rows, so incidental spend never enters scope at all.
--   2. Median anchor band. Otherwise fall back to subscription-shaped categories and keep
--      only charges within 60%-200% of that merchant's median. An $8 drink is 2% of a
--      $347 anchor and drops out; a genuine 3-10% price step survives.
--
-- Note the band is applied AFTER stage 1 rather than as an OR alongside it: a charge the
-- aggregator mislabels as recurring still has to look like the recurring tier to count.
-- Failure is biased toward false negatives -- if incidental charges outnumber the
-- membership the anchor lands on noise and the merchant drops out of the corpus entirely,
-- which is strictly better than emitting a fabricated alert.
CREATE OR REPLACE VIEW `family_finance.v_subscription_charges` AS
WITH candidate_txns AS (
    SELECT
        COALESCE(clean_merchant_name, merchant_name) AS merchant,
        category_name,
        transaction_date,
        ROUND(CAST(ABS(amount) AS FLOAT64), 2) AS amount,
        COALESCE(is_recurring, FALSE) AS is_recurring
    FROM `family_finance.raw_transactions`
    WHERE amount < 0
      AND pending = FALSE
      AND COALESCE(clean_merchant_name, merchant_name) IS NOT NULL
      AND ABS(amount) > 0
      -- Money movement, debt service and metered utilities are not subscriptions. Without
      -- the mortgage/ATM/fee exclusions a recurring mortgage debit lands in this corpus
      -- and gets advertised as a five-figure annual "subscription" the family can cancel.
      AND LOWER(category_name) NOT IN (
          'transfer', 'transfers', 'credit card payment', 'credit card payments',
          'loan payment', 'loan repayment', 'balance transfers', 'mortgage', 'rent',
          'cash & atm', 'atm', 'financial fees', 'bank fees', 'taxes', 'buy', 'sell',
          'utilities', 'gas & electric', 'electric', 'gas', 'water'
      )
      AND LOWER(COALESCE(clean_merchant_name, merchant_name)) NOT LIKE '%transfer%'
      AND transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 24 MONTH)
),
flag_profile AS (
    SELECT merchant, COUNTIF(is_recurring) AS recurring_rows
    FROM candidate_txns
    GROUP BY merchant
),
scoped AS (
    SELECT c.*
    FROM candidate_txns c
    JOIN flag_profile f USING (merchant)
    WHERE
        -- Stage 1: the aggregator's recurrence flag is authoritative when it exists.
        (f.recurring_rows >= 2 AND c.is_recurring)
        -- Stage 2: otherwise admit only subscription-shaped categories.
        OR (
            f.recurring_rows < 2
            AND LOWER(c.category_name) IN (
                'subscriptions', 'phone', 'mobile phone', 'internet & cable', 'internet',
                'fitness', 'gym', 'gyms & fitness', 'home security', 'streaming', 'software'
            )
        )
),
merchant_recurring_baseline AS (
    -- Keyed on merchant alone. Keying on (merchant, category) splits a single service in
    -- two whenever the aggregator re-categorises it mid-history, which double-counts the
    -- service in overlap and halves each fragment's charge history for creep detection.
    SELECT
        merchant,
        APPROX_QUANTILES(amount, 100)[SAFE_OFFSET(50)] AS baseline_recurring_amount,
        COUNT(*) AS scoped_rows
    FROM scoped
    GROUP BY merchant
    HAVING scoped_rows >= 3
)
SELECT
    s.merchant,
    s.category_name,
    s.transaction_date,
    s.amount,
    s.is_recurring,
    ROUND(b.baseline_recurring_amount, 2) AS recurring_anchor
FROM scoped s
JOIN merchant_recurring_baseline b USING (merchant)
WHERE s.amount BETWEEN b.baseline_recurring_amount * 0.60
                   AND b.baseline_recurring_amount * 2.00;

-- VIEW A2: Active Subscriptions & Recurring Debits
-- Built on the cleaned ledger, one row per merchant. `has_price_increased` is deliberately
-- absent: a lifetime MIN vs MAX comparison re-fires forever on a change that happened a
-- year ago. Price movement now lives in v_subscription_price_creep.
CREATE OR REPLACE VIEW `family_finance.v_active_subscriptions` AS
WITH recurring_stats AS (
    SELECT
        merchant,
        ANY_VALUE(category_name) AS category_name,
        COUNT(*) AS charge_count,
        ROUND(ANY_VALUE(recurring_anchor), 2) AS typical_charge,
        ROUND(AVG(amount), 2) AS avg_charge,
        ROUND(MIN(amount), 2) AS min_charge,
        ROUND(MAX(amount), 2) AS max_charge,
        -- Coefficient of variation. A real subscription bills a near-constant amount;
        -- anything genuinely variable is metered consumption, not a plan.
        ROUND(COALESCE(SAFE_DIVIDE(STDDEV_SAMP(amount), AVG(amount)), 0), 3) AS charge_variability,
        MIN(transaction_date) AS first_seen,
        MAX(transaction_date) AS last_seen,
        ROUND(DATE_DIFF(MAX(transaction_date), MIN(transaction_date), DAY) / NULLIF(COUNT(*) - 1, 0), 1) AS avg_cadence_days
    FROM `family_finance.v_subscription_charges`
    GROUP BY merchant
    HAVING charge_count >= 3
)
SELECT
    s.merchant,
    s.category_name,
    COALESCE(d.domain, 'UNCLASSIFIED') AS functional_domain,
    COALESCE(d.disposition, 'UNKNOWN') AS disposition,
    COALESCE(d.is_overlap_eligible, FALSE) AS is_overlap_eligible,
    COALESCE(d.overlap_min_count, 2) AS overlap_min_count,
    s.charge_count,
    s.typical_charge,
    s.avg_charge,
    s.min_charge,
    s.max_charge,
    s.charge_variability,
    -- Bands are deliberately wide: avg_cadence_days is span/(n-1), so a single skipped or
    -- doubled cycle in two years of history drags a plainly monthly service off a narrow
    -- 25-35 band. The label is descriptive only -- estimated_annual_cost below is derived
    -- from the raw cadence, so a mislabelled band never distorts the run rate.
    CASE
        WHEN s.avg_cadence_days BETWEEN 25 AND 40 THEN 'MONTHLY'
        WHEN s.avg_cadence_days BETWEEN 80 AND 110 THEN 'QUARTERLY'
        WHEN s.avg_cadence_days BETWEEN 160 AND 200 THEN 'SEMI_ANNUAL'
        WHEN s.avg_cadence_days BETWEEN 330 AND 400 THEN 'ANNUAL'
        ELSE 'IRREGULAR'
    END AS billing_cadence,
    -- Cadence-normalised. The previous CASE fell through to avg_charge * charge_count for
    -- anything off-cadence, which made a subscription's "annual cost" a function of how
    -- much history happened to be loaded rather than of its price.
    ROUND(s.typical_charge * 365.0 / s.avg_cadence_days, 2) AS estimated_annual_cost,
    -- True monthly run rate. Summing avg_charge across mixed cadences counts an annual
    -- bill as if it were charged every month.
    ROUND(s.typical_charge * 365.0 / s.avg_cadence_days / 12, 2) AS monthly_run_rate,
    s.first_seen,
    s.last_seen,
    s.avg_cadence_days,
    DATE_DIFF(CURRENT_DATE(), s.last_seen, DAY) AS days_since_last_charge,
    -- "Concurrently active" has to be cadence-aware: an annual plan billed 8 months ago is
    -- still live, a monthly plan last charged 8 months ago was cancelled.
    (DATE_DIFF(CURRENT_DATE(), s.last_seen, DAY) <= CAST(s.avg_cadence_days * 1.5 + 15 AS INT64)) AS is_currently_active
FROM recurring_stats s
LEFT JOIN `family_finance.v_merchant_domain` d USING (merchant)
WHERE s.avg_cadence_days BETWEEN 25 AND 400
  AND s.charge_variability <= 0.35
ORDER BY estimated_annual_cost DESC;

-- VIEW A3: Subscription Price Creep Detection
-- Compares the most recent bill against the average of the three bills before it, over
-- consecutive billing cycles on the cleaned ledger.
--
-- Why a trailing baseline rather than LAG(1) alone: a single anomalous cycle (a prorated
-- month, a policy endorsement, a one-off credit) becomes the comparison point and either
-- fabricates a hike or masks a real one. Averaging the prior three cycles survives one
-- bad reading in either direction. LAG(1) is still required to step up as well, so a
-- merchant cannot trip the alert purely by reverting from a promo.
CREATE OR REPLACE VIEW `family_finance.v_subscription_price_creep` AS
WITH sequenced AS (
    SELECT
        merchant,
        transaction_date,
        amount,
        LAG(amount, 1) OVER cycle AS prev_amount,
        LAG(transaction_date, 1) OVER cycle AS prev_date,
        AVG(amount) OVER (
            PARTITION BY merchant ORDER BY transaction_date
            ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING
        ) AS baseline_amount,
        COUNT(*) OVER (
            PARTITION BY merchant ORDER BY transaction_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_charge_count,
        ROW_NUMBER() OVER (PARTITION BY merchant ORDER BY transaction_date DESC) AS recency_rank
    FROM `family_finance.v_subscription_charges`
    WINDOW cycle AS (PARTITION BY merchant ORDER BY transaction_date)
),
latest_cycle AS (
    SELECT * FROM sequenced WHERE recency_rank = 1
)
SELECT
    l.merchant,
    s.category_name,
    s.functional_domain,
    s.disposition,
    s.billing_cadence,
    l.amount AS latest_charge,
    ROUND(l.baseline_amount, 2) AS prior_charge,
    ROUND(l.amount - l.baseline_amount, 2) AS price_increase_amount,
    ROUND(100 * SAFE_DIVIDE(l.amount - l.baseline_amount, l.baseline_amount), 1) AS pct_increase,
    -- The annualised cost of the INCREASE, not of the whole subscription. Quoting the full
    -- run rate as the saving is what turned a $2.89/mo step into "save up to $727/year".
    ROUND((l.amount - l.baseline_amount) * 365.0 / s.avg_cadence_days, 2) AS annual_impact,
    s.estimated_annual_cost,
    l.transaction_date AS effective_date,
    DATE_DIFF(l.transaction_date, l.prev_date, DAY) AS days_since_prior_charge
FROM latest_cycle l
JOIN `family_finance.v_active_subscriptions` s USING (merchant)
WHERE l.prior_charge_count >= 3                                                 -- need a real baseline
  AND l.transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 45 DAY)           -- recent: stale hikes stop firing
  AND l.amount > l.prev_amount                                                  -- the latest cycle genuinely stepped up
  AND l.amount - l.baseline_amount > 0.50                                       -- ignore rounding and tax drift
  AND SAFE_DIVIDE(l.amount - l.baseline_amount, l.baseline_amount) BETWEEN 0.03 AND 0.40
  AND DATE_DIFF(l.transaction_date, l.prev_date, DAY) BETWEEN 20 AND 400        -- consecutive cycles, not a gap
  AND s.disposition IN ('CANCELLABLE', 'RESHOPPABLE')                           -- metered utilities are not price creep
ORDER BY annual_impact DESC;

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
WHERE NOT COALESCE(pending, FALSE)
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
            WHEN LOWER(a.subtype_name) IN ('mortgage') OR LOWER(a.display_name) LIKE '%mortgage%' THEN 'MORTGAGE'
            WHEN LOWER(a.subtype_name) IN ('credit_card', 'credit') OR LOWER(a.type_name) = 'credit' THEN 'CREDIT_CARD'
            WHEN LOWER(a.subtype_name) IN ('checking', 'savings', 'money_market') OR LOWER(a.type_name) = 'depository' THEN 'DEPOSITORY'
            WHEN LOWER(a.subtype_name) IN ('loan', 'student_loan', 'auto_loan', 'personal_loan') OR LOWER(a.type_name) = 'loan' THEN 'OTHER_LOAN'
            WHEN LOWER(a.subtype_name) IN ('brokerage', 'ira', 'roth', '401k', 'st_401k') OR LOWER(a.type_name) = 'investment' THEN 'INVESTMENT'
            ELSE 'OTHER'
        END AS account_class,
        ROUND(ABS(a.current_balance), 2) AS current_balance,
        ROUND(a.credit_limit, 2) AS credit_limit,
        COALESCE(
            a.interest_rate,
            CASE
                WHEN LOWER(a.subtype_name) IN ('heloc', 'home_equity', 'home_equity_line_of_credit') 
                     OR LOWER(a.display_name) LIKE '%heloc%' OR LOWER(a.display_name) LIKE '%home equity%' THEN 0.0675
                WHEN LOWER(a.subtype_name) IN ('mortgage') OR LOWER(a.display_name) LIKE '%mortgage%' THEN 0.0350
                WHEN LOWER(a.subtype_name) IN ('loan', 'student_loan', 'auto_loan', 'personal_loan') THEN 0.0750
                ELSE NULL
            END
        ) AS apr,
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

-- VIEW C2: Total Debt Balances & Daily Interest Burden (Mortgage + HELOC + Other Liabilities)
CREATE OR REPLACE VIEW `family_finance.v_debt_daily_cost` AS
SELECT
    account_id,
    display_name,
    institution_name,
    CASE
        WHEN account_class = 'MORTGAGE' THEN 'MORTGAGE'
        WHEN account_class = 'HOME_EQUITY_LINE' THEN 'HELOC'
        WHEN account_class = 'CREDIT_CARD' THEN 'CREDIT_CARD'
        ELSE 'OTHER_LOAN'
    END AS debt_type,
    account_class,
    current_balance,
    credit_limit,
    ROUND(credit_limit - current_balance, 2) AS available_credit,
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
WHERE account_class IN ('HOME_EQUITY_LINE', 'MORTGAGE', 'OTHER_LOAN', 'TERM_LOAN')
   OR (account_class = 'CREDIT_CARD' AND current_balance > 0 AND apr > 0)
ORDER BY 
    CASE 
        WHEN account_class = 'HOME_EQUITY_LINE' THEN 1 
        WHEN account_class = 'MORTGAGE' THEN 2 
        ELSE 3 
    END,
    current_balance DESC;

-- VIEW C3: Executive Debt Summary & Daily Carrying Cost Breakdown
CREATE OR REPLACE VIEW `family_finance.v_debt_summary` AS
SELECT
    ROUND(COALESCE(SUM(current_balance), 0.0), 2) AS total_debt_balance,
    ROUND(COALESCE(SUM(daily_interest_cost), 0.0), 2) AS total_daily_interest_cost,
    ROUND(COALESCE(SUM(monthly_interest_cost), 0.0), 2) AS total_monthly_interest_cost,
    -- Mortgage breakdown
    ROUND(COALESCE(SUM(CASE WHEN debt_type = 'MORTGAGE' THEN current_balance ELSE 0 END), 0.0), 2) AS mortgage_balance,
    ROUND(COALESCE(SUM(CASE WHEN debt_type = 'MORTGAGE' THEN daily_interest_cost ELSE 0 END), 0.0), 2) AS mortgage_daily_interest_cost,
    -- HELOC breakdown
    ROUND(COALESCE(SUM(CASE WHEN debt_type = 'HELOC' THEN current_balance ELSE 0 END), 0.0), 2) AS heloc_balance,
    ROUND(COALESCE(SUM(CASE WHEN debt_type = 'HELOC' THEN daily_interest_cost ELSE 0 END), 0.0), 2) AS heloc_daily_interest_cost,
    -- Other debt breakdown (auto, student, other loans)
    ROUND(COALESCE(SUM(CASE WHEN debt_type NOT IN ('MORTGAGE', 'HELOC') THEN current_balance ELSE 0 END), 0.0), 2) AS other_debt_balance,
    ROUND(COALESCE(SUM(CASE WHEN debt_type NOT IN ('MORTGAGE', 'HELOC') THEN daily_interest_cost ELSE 0 END), 0.0), 2) AS other_debt_daily_interest_cost,
    COUNT(DISTINCT account_id) AS total_debt_accounts
FROM `family_finance.v_debt_daily_cost`
WHERE is_primary_active = TRUE OR lifecycle_status = 'PRIMARY';

-- Backward-compatibility view for legacy HELOC queries
CREATE OR REPLACE VIEW `family_finance.v_heloc_daily_cost` AS
SELECT * 
FROM `family_finance.v_debt_daily_cost`
WHERE debt_type = 'HELOC';

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

-- VIEW F: Subscription Functional Overlap & Redundancy
-- Fires only on genuine functional duplication: several services that substitute for one
-- another, all currently live. Grouping by the aggregator's category instead produced a
-- 15-service "Subscriptions" soup advising the family to rotate a password manager
-- against a magazine, and paired a pet insurer with a veterinary clinic under "Pets".
CREATE OR REPLACE VIEW `family_finance.v_subscription_overlap` AS
SELECT
    functional_domain,
    COUNT(*) AS active_service_count,
    -- Cadence-normalised, so an annually billed service no longer contributes its full
    -- price to a "monthly" total.
    ROUND(SUM(monthly_run_rate), 2) AS combined_monthly_cost,
    ROUND(SUM(estimated_annual_cost), 2) AS combined_annual_cost,
    -- Defensible saving: consolidate onto the single largest plan and drop the rest.
    -- The old view reported the entire domain total as recoverable, which implies
    -- cancelling every service including the one being kept.
    ROUND(SUM(monthly_run_rate) - MAX(monthly_run_rate), 2) AS consolidation_savings_monthly,
    ROUND((SUM(monthly_run_rate) - MAX(monthly_run_rate)) * 12, 2) AS consolidation_savings_annual,
    STRING_AGG(merchant, ', ' ORDER BY monthly_run_rate DESC) AS active_services
FROM `family_finance.v_active_subscriptions`
WHERE is_overlap_eligible
  -- Concurrency. Without this a service cancelled a year ago still counts toward the
  -- duplicate tally, which is the same defect as comparing a lifetime MIN to a MAX.
  AND is_currently_active
GROUP BY functional_domain
-- Per-domain threshold: two video streamers is normal household behaviour, three is a
-- rotation opportunity. Everything else is redundant at two.
HAVING active_service_count >= MAX(overlap_min_count)
   AND combined_monthly_cost >= 15.00
ORDER BY consolidation_savings_monthly DESC;

-- VIEW G: Utility Seasonal Baseline
-- Utilities are excluded from the subscription corpus because they are regulated, metered
-- and have no cancel action. That does not mean they should go unwatched: it means the
-- right comparison is seasonal, not sequential. A December heating bill is not a "price
-- hike" over November, and a July cooling bill is not a hike over June -- both are the
-- same consumption curve every year. Comparing a month against the SAME calendar month in
-- prior years isolates genuine rate changes and consumption regressions from weather.
CREATE OR REPLACE VIEW `family_finance.v_utility_seasonal_baseline` AS
WITH utility_months AS (
    SELECT
        d.merchant,
        DATE_TRUNC(t.transaction_date, MONTH) AS spend_month,
        EXTRACT(MONTH FROM t.transaction_date) AS calendar_month,
        ROUND(SUM(CAST(ABS(t.amount) AS FLOAT64)), 2) AS month_total
    FROM `family_finance.raw_transactions` t
    JOIN `family_finance.v_merchant_domain` d
      ON COALESCE(t.clean_merchant_name, t.merchant_name) = d.merchant
    WHERE t.amount < 0
      AND t.pending = FALSE
      AND d.domain = 'UTILITY_METERED'
      AND t.transaction_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 48 MONTH)
    GROUP BY 1, 2, 3
),
seasonal_norm AS (
    SELECT
        merchant,
        calendar_month,
        ROUND(AVG(month_total), 2) AS seasonal_avg,
        ROUND(STDDEV_SAMP(month_total), 2) AS seasonal_stddev,
        COUNT(*) AS years_observed
    FROM utility_months
    -- Strictly before the month under test (the last completed month). Cutting at the
    -- current month instead would leave the tested month inside its own baseline, pulling
    -- the average toward it and blunting exactly the overage we are trying to detect.
    WHERE spend_month < DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH)
    GROUP BY 1, 2
)
SELECT
    m.merchant,
    m.spend_month,
    m.month_total,
    n.seasonal_avg,
    n.seasonal_stddev,
    n.years_observed,
    ROUND(m.month_total - n.seasonal_avg, 2) AS variance_vs_season,
    ROUND(100 * SAFE_DIVIDE(m.month_total - n.seasonal_avg, n.seasonal_avg), 1) AS variance_pct
FROM utility_months m
JOIN seasonal_norm n USING (merchant, calendar_month)
-- Last completed month. A month-to-date total compared against a full-month norm always
-- reads low and would never surface a genuine overage.
WHERE m.spend_month = DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH)
  AND n.years_observed >= 1
ORDER BY variance_vs_season DESC;

-- VIEW H: Duplicate Charge Radar (PR 7a)
-- Detects identical debit charges on the same account from the same merchant within a 72-hour window.
-- Excludes transfers, credit card payments, loan payments, ATM withdrawals, pending transactions, and micro-transit/parking taps (< $3.00).
CREATE OR REPLACE VIEW `family_finance.v_duplicate_charges` AS
SELECT
    t1.transaction_id AS t1_id,
    t2.transaction_id AS t2_id,
    t1.account_id,
    COALESCE(a.display_name, 'Account') AS account_name,
    COALESCE(t1.clean_merchant_name, t1.merchant_name) AS merchant,
    t1.category_name,
    ROUND(CAST(ABS(t1.amount) AS FLOAT64), 2) AS amount,
    t1.transaction_date AS t1_date,
    t2.transaction_date AS t2_date,
    DATE_DIFF(t2.transaction_date, t1.transaction_date, DAY) AS days_apart,
    CONCAT('duplicate:', t1.transaction_id, ':', t2.transaction_id) AS alert_key
FROM `family_finance.raw_transactions` t1
JOIN `family_finance.raw_transactions` t2
  ON t1.account_id = t2.account_id
 AND t1.transaction_id != t2.transaction_id
 AND (
     t1.transaction_date < t2.transaction_date
     OR (t1.transaction_date = t2.transaction_date AND t1.transaction_id < t2.transaction_id)
 )
 AND COALESCE(t1.clean_merchant_name, t1.merchant_name) = COALESCE(t2.clean_merchant_name, t2.merchant_name)
 AND ROUND(CAST(ABS(t1.amount) AS FLOAT64), 2) = ROUND(CAST(ABS(t2.amount) AS FLOAT64), 2)
LEFT JOIN `family_finance.raw_accounts` a ON t1.account_id = a.account_id
WHERE t1.amount < 0
  AND t2.amount < 0
  AND NOT COALESCE(t1.pending, FALSE)
  AND NOT COALESCE(t2.pending, FALSE)
  AND COALESCE(t1.clean_merchant_name, t1.merchant_name) IS NOT NULL
  AND ABS(t1.amount) >= 3.00
  AND LOWER(t1.category_name) NOT IN (
      'transfer', 'transfers', 'credit card payment', 'credit card payments',
      'loan payment', 'loan repayment', 'balance transfers', 'cash & atm', 'atm'
  )
  AND DATE_DIFF(t2.transaction_date, t1.transaction_date, DAY) BETWEEN 0 AND 3
  AND t1.transaction_date >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 30 DAY)
ORDER BY t2.transaction_date DESC, amount DESC;

-- VIEW I: New Subscription & Free Trial Intercept (PR 7a)
-- Catches recurring subscription charges or trial conversions appearing for the very first time within the last 35 days.
CREATE OR REPLACE VIEW `family_finance.v_new_subscription_intercept` AS
WITH merchant_history AS (
    SELECT
        COALESCE(clean_merchant_name, merchant_name) AS merchant,
        ANY_VALUE(category_name) AS category_name,
        MIN(transaction_date) AS first_seen,
        MAX(transaction_date) AS latest_seen,
        COUNT(*) AS charge_count,
        ROUND(AVG(CAST(ABS(amount) AS FLOAT64)), 2) AS avg_charge,
        ROUND(SUM(CAST(ABS(amount) AS FLOAT64)), 2) AS total_spend,
        LOGICAL_OR(COALESCE(is_recurring, FALSE)) AS is_recurring_flagged
    FROM `family_finance.raw_transactions`
    WHERE amount < 0
      AND NOT COALESCE(pending, FALSE)
      AND COALESCE(clean_merchant_name, merchant_name) IS NOT NULL
      AND LOWER(category_name) NOT IN (
          'transfer', 'transfers', 'credit card payment', 'credit card payments',
          'loan payment', 'loan repayment', 'balance transfers', 'mortgage', 'rent',
          'cash & atm', 'atm', 'financial fees', 'bank fees', 'taxes'
      )
    GROUP BY 1
)
SELECT
    h.merchant,
    h.category_name,
    COALESCE(d.domain, 'UNCLASSIFIED') AS functional_domain,
    COALESCE(d.disposition, 'UNKNOWN') AS disposition,
    h.first_seen,
    h.latest_seen,
    h.charge_count,
    h.avg_charge,
    h.total_spend,
    h.is_recurring_flagged,
    DATE_DIFF(CURRENT_DATE('America/New_York'), h.first_seen, DAY) AS days_since_first_charge,
    CONCAT('new_sub:', LOWER(REGEXP_REPLACE(h.merchant, r'[^a-zA-Z0-9]+', '_'))) AS alert_key
FROM merchant_history h
LEFT JOIN `family_finance.v_merchant_domain` d ON h.merchant = d.merchant
WHERE h.first_seen >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 35 DAY)
  AND (
      h.is_recurring_flagged
      OR d.domain IS NOT NULL
      OR LOWER(h.category_name) IN ('subscriptions', 'subscription', 'software', 'entertainment', 'memberships', 'gym')
  )
ORDER BY h.latest_seen DESC, h.avg_charge DESC;

-- VIEW J: Annual & Semi-Annual Bill Radar (PR 7a)
-- Predicts upcoming annual (330-380 day cycle) and semi-annual (150-195 day cycle) lump sums
-- due within the next 30 days that have not yet posted.
CREATE OR REPLACE VIEW `family_finance.v_annual_bill_radar` AS
WITH trailing_candidates AS (
    SELECT
        COALESCE(clean_merchant_name, merchant_name) AS merchant,
        category_name,
        transaction_date,
        ROUND(CAST(ABS(amount) AS FLOAT64), 2) AS amount,
        COALESCE(is_recurring, FALSE) AS is_recurring
    FROM `family_finance.raw_transactions`
    WHERE amount < 0
      AND NOT COALESCE(pending, FALSE)
      AND COALESCE(clean_merchant_name, merchant_name) IS NOT NULL
      AND ABS(amount) >= 20.00
      AND LOWER(category_name) NOT IN (
          'transfer', 'transfers', 'credit card payment', 'credit card payments',
          'loan payment', 'loan repayment', 'balance transfers', 'mortgage', 'rent',
          'cash & atm', 'atm', 'financial fees', 'bank fees', 'taxes'
      )
),
recent_charges AS (
    SELECT DISTINCT merchant
    FROM trailing_candidates
    WHERE transaction_date >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 60 DAY)
),
prior_cycles AS (
    SELECT
        c.merchant,
        c.category_name,
        c.amount,
        c.transaction_date AS prior_charge_date,
        DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) AS days_since_charge,
        CASE
            WHEN DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 330 AND 380 THEN 'ANNUAL'
            WHEN DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 150 AND 195 THEN 'SEMI_ANNUAL'
            ELSE NULL
        END AS cadence_type,
        CASE
            WHEN DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 330 AND 380 THEN 365
            WHEN DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 150 AND 195 THEN 182
            ELSE NULL
        END AS cadence_days
    FROM trailing_candidates c
    WHERE c.merchant NOT IN (SELECT merchant FROM recent_charges)
      AND (
          DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 330 AND 380
          OR DATE_DIFF(CURRENT_DATE('America/New_York'), c.transaction_date, DAY) BETWEEN 150 AND 195
      )
)
SELECT
    p.merchant,
    p.category_name,
    COALESCE(d.domain, 'UNCLASSIFIED') AS functional_domain,
    COALESCE(d.disposition, 'UNKNOWN') AS disposition,
    p.cadence_type,
    p.amount AS prior_charge_amount,
    p.prior_charge_date,
    DATE_ADD(p.prior_charge_date, INTERVAL p.cadence_days DAY) AS predicted_renewal_date,
    DATE_DIFF(DATE_ADD(p.prior_charge_date, INTERVAL p.cadence_days DAY), CURRENT_DATE('America/New_York'), DAY) AS days_until_renewal,
    CONCAT('annual_bill:', LOWER(REGEXP_REPLACE(p.merchant, r'[^a-zA-Z0-9]+', '_')), ':', FORMAT_DATE('%Y', DATE_ADD(p.prior_charge_date, INTERVAL p.cadence_days DAY))) AS alert_key
FROM prior_cycles p
LEFT JOIN `family_finance.v_merchant_domain` d ON p.merchant = d.merchant
WHERE p.cadence_type IS NOT NULL
  AND (
      d.domain IS NOT NULL
      OR LOWER(p.category_name) LIKE '%insurance%'
      OR LOWER(p.category_name) IN ('subscriptions', 'subscription', 'software', 'memberships', 'dues', 'licenses')
  )
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY p.merchant, p.cadence_type
    ORDER BY p.prior_charge_date DESC
) = 1
ORDER BY days_until_renewal ASC;

-- VIEW J: Paycheck Surplus & Multi-Facility Debt Sweep Allocation Engine
-- Identifies recent income deposits into checking accounts, models 30-day fixed
-- overhead commitments with safety buffers, and calculates the exact safe-to-sweep
-- surplus cash to accelerate high-interest debt paydown across liabilities while reporting total carrying costs.
CREATE OR REPLACE VIEW `family_finance.v_paycheck_surplus_allocation` AS
WITH recent_income AS (
    SELECT
        t.transaction_id,
        t.transaction_date,
        t.account_id,
        COALESCE(t.clean_merchant_name, t.merchant_name, 'Paycheck / Income') AS employer_or_source,
        t.category_name,
        ROUND(t.amount, 2) AS income_amount
    FROM `family_finance.raw_transactions` t
    WHERE t.amount > 0
      AND NOT COALESCE(t.pending, FALSE)
      AND t.transaction_date >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 14 DAY)
      AND (
          LOWER(COALESCE(t.category_name, '')) IN ('income', 'paycheck', 'bonus', 'salary', 'wages')
          OR REGEXP_CONTAINS(LOWER(COALESCE(t.clean_merchant_name, t.merchant_name, '')), r'(payroll|adp|gusto|paychex|workday|direct dep|treasury|dfas|salary)')
      )
    QUALIFY ROW_NUMBER() OVER (
        ORDER BY t.transaction_date DESC, t.amount DESC
    ) = 1
),
liquid_cash AS (
    SELECT
        COALESCE(ROUND(SUM(current_balance), 2), 0.0) AS liquid_balance,
        COUNT(*) AS checking_account_count
    FROM `family_finance.raw_accounts`
    WHERE (
        LOWER(COALESCE(type_name, '')) IN ('depository', 'checking')
        OR LOWER(COALESCE(subtype_name, '')) IN ('checking', 'savings', 'money_market')
    )
    AND is_asset = TRUE
),
fixed_overhead AS (
    SELECT COALESCE(
        (
            SELECT ROUND(AVG(total_amount), 2)
            FROM `family_finance.v_spend_classification`
            WHERE spend_type = 'FIXED_OVERHEAD'
              AND month >= FORMAT_DATE('%Y-%m', DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 3 MONTH))
              AND month < FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York'))
        ),
        (
            SELECT COALESCE(ROUND(SUM(monthly_run_rate), 2), 0.0)
            FROM `family_finance.v_active_subscriptions`
            WHERE is_currently_active = TRUE
        ),
        2500.00
    ) AS monthly_fixed_burn
),
upcoming_bills AS (
    SELECT COALESCE(ROUND(SUM(prior_charge_amount), 2), 0.0) AS upcoming_30d_lump_sums
    FROM `family_finance.v_annual_bill_radar`
    WHERE days_until_renewal BETWEEN 0 AND 30
),
target_sweep_debt AS (
    SELECT
        display_name AS target_debt_name,
        debt_type AS target_debt_type,
        current_balance AS target_debt_balance,
        apr AS target_debt_apr,
        daily_interest_cost,
        monthly_interest_cost
    FROM `family_finance.v_debt_daily_cost`
    WHERE (debt_type = 'HELOC' OR (debt_type IN ('CREDIT_CARD', 'OTHER_LOAN') AND apr > 0.05))
      AND current_balance > 0
    ORDER BY apr DESC, current_balance DESC
    LIMIT 1
),
debt_totals AS (
    SELECT
        COALESCE(total_debt_balance, 0.0) AS total_debt_balance,
        COALESCE(total_daily_interest_cost, 0.0) AS total_daily_debt_cost,
        COALESCE(mortgage_balance, 0.0) AS mortgage_balance,
        COALESCE(mortgage_daily_interest_cost, 0.0) AS mortgage_daily_cost,
        COALESCE(heloc_balance, 0.0) AS heloc_balance,
        COALESCE(heloc_daily_interest_cost, 0.0) AS heloc_daily_cost,
        COALESCE(other_debt_balance, 0.0) AS other_debt_balance,
        COALESCE(other_debt_daily_interest_cost, 0.0) AS other_debt_daily_cost
    FROM `family_finance.v_debt_summary`
)
SELECT
    CURRENT_DATE('America/New_York') AS evaluation_date,
    inc.transaction_id AS latest_income_id,
    inc.transaction_date AS latest_income_date,
    inc.employer_or_source,
    inc.income_amount AS latest_income_amount,
    lc.liquid_balance,
    fo.monthly_fixed_burn,
    ub.upcoming_30d_lump_sums,
    -- Safe reserve requirement: 1.15x of monthly fixed overhead + upcoming 30d bills, with a $2,000 floor
    ROUND(GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums), 2) AS safe_reserve_buffer,
    -- Safe surplus: checking reserves minus safe reserve buffer
    ROUND(GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)), 2) AS safe_surplus,
    -- Target debt metrics (highest APR variable debt)
    COALESCE(td.target_debt_name, 'Home Equity Line of Credit') AS target_debt_name,
    COALESCE(td.target_debt_balance, 0.0) AS target_debt_balance,
    COALESCE(td.target_debt_apr, 0.0) AS target_debt_apr,
    -- Backward-compatible HELOC aliases
    COALESCE(td.target_debt_name, 'HELOC') AS heloc_name,
    COALESCE(td.target_debt_balance, 0.0) AS heloc_balance,
    COALESCE(td.target_debt_apr, 0.0) AS heloc_apr,
    -- Total liabilities across Mortgage + HELOC + Other
    COALESCE(dt.total_debt_balance, 0.0) AS total_debt_balance,
    COALESCE(dt.total_daily_debt_cost, 0.0) AS total_daily_debt_cost,
    COALESCE(dt.mortgage_balance, 0.0) AS mortgage_balance,
    COALESCE(dt.mortgage_daily_cost, 0.0) AS mortgage_daily_cost,
    COALESCE(dt.heloc_balance, 0.0) AS heloc_total_balance,
    COALESCE(dt.heloc_daily_cost, 0.0) AS heloc_daily_cost,
    COALESCE(dt.other_debt_balance, 0.0) AS other_debt_balance,
    -- Recommended sweep amount: min(safe_surplus, target_debt_balance)
    ROUND(LEAST(
        GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)),
        COALESCE(td.target_debt_balance, 0.0)
    ), 2) AS recommended_sweep_amount,
    -- Interest savings calculations
    ROUND(LEAST(
        GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)),
        COALESCE(td.target_debt_balance, 0.0)
    ) * (COALESCE(td.target_debt_apr, 0.0) / 365), 2) AS daily_interest_saved,
    ROUND(LEAST(
        GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)),
        COALESCE(td.target_debt_balance, 0.0)
    ) * (COALESCE(td.target_debt_apr, 0.0) / 12), 2) AS monthly_interest_saved,
    ROUND(LEAST(
        GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)),
        COALESCE(td.target_debt_balance, 0.0)
    ) * COALESCE(td.target_debt_apr, 0.0), 2) AS annual_interest_saved,
    -- Alert key for deduplication and snooze
    CONCAT(
        'paycheck_sweep:',
        COALESCE(CAST(inc.transaction_date AS STRING), FORMAT_DATE('%Y-%m-%d', CURRENT_DATE('America/New_York'))),
        ':',
        CAST(CAST(ROUND(LEAST(
            GREATEST(0.0, lc.liquid_balance - GREATEST(2000.00, (fo.monthly_fixed_burn * 1.15) + ub.upcoming_30d_lump_sums)),
            COALESCE(td.target_debt_balance, 0.0)
        ), 0) AS INT64) AS STRING)
    ) AS alert_key
FROM liquid_cash lc
CROSS JOIN fixed_overhead fo
CROSS JOIN upcoming_bills ub
LEFT JOIN recent_income inc ON TRUE
LEFT JOIN target_sweep_debt td ON TRUE
LEFT JOIN debt_totals dt ON TRUE;

-- Backward-compatible view alias
CREATE OR REPLACE VIEW `family_finance.v_paycheck_surplus_sweep` AS
SELECT * FROM `family_finance.v_paycheck_surplus_allocation`;

-- ============================================================================
-- PR 9: RECEIPT & TAX DEDUCTIBILITY INGESTION
-- ============================================================================

-- 1. Receipt Records Table (Document AI & Gemini Vision Extraction)
CREATE TABLE IF NOT EXISTS `family_finance.receipt_records` (
    receipt_id STRING NOT NULL,
    uploaded_at TIMESTAMP NOT NULL,
    user_email STRING,
    merchant_name STRING NOT NULL,
    receipt_date DATE NOT NULL,
    total_amount NUMERIC NOT NULL,
    deductible_amount NUMERIC NOT NULL,     -- Isolates deductible portion from mixed receipts
    tax_amount NUMERIC,
    tip_amount NUMERIC,
    payment_method_last4 STRING,
    tax_category STRING NOT NULL,           -- SCHEDULE_C_EXPENSE, HSA_FSA_ELIGIBLE, CHARITABLE_DONATION, CHILDCARE_DEPENDENT_CARE, STANDARD_NON_DEDUCTIBLE
    is_tax_deductible BOOL NOT NULL,
    deductibility_confidence FLOAT64,
    tax_justification STRING,               -- Cites IRS IRC code or Pub (e.g. IRC Sec. 213(d) / Pub 502)
    audit_status STRING NOT NULL,           -- VERIFIED, NEEDS_REVIEW, REJECTED
    matched_transaction_id STRING,          -- Foreign key to raw_transactions.transaction_id
    line_items_json STRING,                 -- PII-sanitized JSON line items: [{"description": "...", "amount": 0.0, "is_deductible": true}]
    notes STRING
);

-- 2. Tax Deductible Summary Analytical View (Annual Schedule C / HSA / Charity Aggregations)
CREATE OR REPLACE VIEW `family_finance.v_tax_deductible_summary` AS
SELECT
    EXTRACT(YEAR FROM receipt_date) AS tax_year,
    tax_category,
    COUNT(DISTINCT receipt_id) AS receipt_count,
    ROUND(SUM(deductible_amount), 2) AS total_deductible_amount,
    ROUND(SUM(total_amount), 2) AS total_gross_receipt_amount,
    COUNTIF(matched_transaction_id IS NOT NULL) AS matched_transaction_count,
    COUNTIF(audit_status = 'NEEDS_REVIEW') AS pending_review_count,
    ARRAY_AGG(DISTINCT merchant_name IGNORE NULLS LIMIT 10) AS sample_merchants
FROM `family_finance.receipt_records`
WHERE is_tax_deductible = TRUE
  AND audit_status != 'REJECTED'
GROUP BY 1, 2;

-- 3. Chat History Table (Session memory & multi-turn thread continuity)
CREATE TABLE IF NOT EXISTS `family_finance.chat_history` (
    session_id STRING NOT NULL,
    created_at TIMESTAMP NOT NULL,
    user_email STRING,
    user_text STRING,
    model_response STRING
)
PARTITION BY DATE(created_at)
CLUSTER BY session_id, user_email;

-- 4. User Preferences & Long-Term Memory
CREATE TABLE IF NOT EXISTS `family_finance.user_preferences` (
    preference_id STRING NOT NULL,
    user_email STRING NOT NULL,
    preference_text STRING NOT NULL,
    created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY user_email;


