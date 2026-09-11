"""
Proactive spend optimization alerts, anomaly scans, and notification dispatching.
Evaluates BigQuery analytical views (food efficiency, price creep, HELOC daily cost,
subscription overlap, micro-transaction leakage, memory bank budget caps)
and formats/posts Google Chat Card V2 notifications with smart suppression.
"""

import asyncio
import logging
import re
import time
from typing import Any

try:
    import requests
except ImportError:
    requests = None

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None

from app.config import BQ_DATASET_ID, BQ_PROJECT_ID, resolve_secret

logger = logging.getLogger("monarch-gemini.alerts")


def _price_creep_action(merchant: str, disposition: Any, annual_impact: float) -> str:
    """Suggested action wording. An underwritten policy cannot be 'rotated', so the verb
    has to follow the merchant's disposition or the advice is unactionable."""
    if disposition == "RESHOPPABLE":
        return (
            f"{merchant} is a contracted service, so cancelling is not the lever. "
            f"Request the loyalty/renewal rate or obtain two competing quotes before the next term "
            f"to recover the ${annual_impact:.2f}/year increase."
        )
    return (
        f"Audit usage for {merchant}. Downgrading a tier or rotating away recovers the "
        f"${annual_impact:.2f}/year increase; cancelling outright recovers the full plan cost."
    )


def check_subscription_price_creep(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks for subscriptions where price has increased in the last 45 days."""
    alerts = []
    price_creep_sql = f"""
    SELECT merchant, disposition, latest_charge, prior_charge, price_increase_amount,
           pct_increase, annual_impact, estimated_annual_cost, effective_date
    FROM `{project_id}.{dataset_id}.v_subscription_price_creep`
    ORDER BY annual_impact DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(price_creep_sql).result())
        for r in rows:
            merch = getattr(r, "merchant", "Unknown") or "Unknown"
            key = f"price_creep:{merch.lower().strip().replace(' ', '_')}"
            pct = float(getattr(r, "pct_increase", 0) or 0)
            latest = float(getattr(r, "latest_charge", 0) or 0)
            prior = float(getattr(r, "prior_charge", 0) or 0)
            # The recoverable amount is the annualised *increase*, never the whole run
            # rate: a $2.89/mo step is $34.68/yr of new spend, not a $727/yr saving.
            impact = float(getattr(r, "annual_impact", 0) or 0)
            annual = float(getattr(r, "estimated_annual_cost", 0) or 0)
            eff_date = getattr(r, "effective_date", None)
            eff_date_str = f" on {eff_date}" if eff_date else ""
            alerts.append(
                {
                    "type": "PRICE_CREEP",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Subscription Price Hike: {merch} (+{pct:.1f}%)",
                    "detail": (
                        f"Charge rose from ${prior:.2f} to ${latest:.2f}{eff_date_str} "
                        f"— ${impact:.2f}/year of new spend on a ${annual:.2f}/year plan."
                    ),
                    "suggested_fix": _price_creep_action(merch, getattr(r, "disposition", None), impact),
                }
            )
    except Exception as e:
        logger.warning(f"Subscription price creep check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription check failed: {e}"})
    return alerts


def check_duplicate_charges(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks for identical charges from the same merchant within a 72-hour window."""
    alerts = []
    sql = f"""
    SELECT
        t1_id, t2_id, account_id, account_name, merchant, category_name,
        amount, t1_date, t2_date, days_apart, alert_key
    FROM `{project_id}.{dataset_id}.v_duplicate_charges`
    ORDER BY t2_date DESC, amount DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            merch = getattr(r, "merchant", "Merchant") or "Merchant"
            amt = float(getattr(r, "amount", 0.0) or 0.0)
            acct = getattr(r, "account_name", "Account") or "Account"
            days = int(getattr(r, "days_apart", 0) or 0)
            t1_d = str(getattr(r, "t1_date", ""))
            t2_d = str(getattr(r, "t2_date", ""))
            key = getattr(r, "alert_key", f"duplicate:{merch.lower().strip()}:{amt}")
            day_str = "same day" if days == 0 else f"{days} day(s) apart"
            alerts.append(
                {
                    "type": "DUPLICATE_CHARGE",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Potential Duplicate Charge: {merch} (${amt:.2f})",
                    "detail": (f"Two identical charges of ${amt:.2f} posted to {acct} {day_str} ({t1_d} and {t2_d})."),
                    "suggested_fix": (
                        f"Check your receipt or contact {merch} to verify if you were double-billed. "
                        "Snooze this alert if both charges were intentional."
                    ),
                }
            )
    except Exception as e:
        logger.warning(f"Duplicate charge check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Duplicate charge check failed: {e}"})
    return alerts


def check_new_subscriptions(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Intercepts newly detected recurring subscriptions or trial conversions within the last 35 days."""
    alerts = []
    sql = f"""
    SELECT
        merchant, category_name, functional_domain, disposition,
        first_seen, latest_seen, charge_count, avg_charge, total_spend,
        is_recurring_flagged, days_since_first_charge, alert_key
    FROM `{project_id}.{dataset_id}.v_new_subscription_intercept`
    ORDER BY latest_seen DESC, avg_charge DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            merch = getattr(r, "merchant", "Subscription") or "Subscription"
            avg_amt = float(getattr(r, "avg_charge", 0.0) or 0.0)
            total = float(getattr(r, "total_spend", 0.0) or 0.0)
            first_d = str(getattr(r, "first_seen", ""))
            days_ago = int(getattr(r, "days_since_first_charge", 0) or 0)
            count = int(getattr(r, "charge_count", 1) or 1)
            key = getattr(r, "alert_key", f"new_sub:{merch.lower().strip().replace(' ', '_')}")
            alerts.append(
                {
                    "type": "NEW_SUBSCRIPTION_DETECTED",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"New Subscription Detected: {merch} (${avg_amt:.2f}/mo)",
                    "detail": (
                        f"First charged on {first_d} ({days_ago} days ago). "
                        f"Total charged so far: ${total:.2f} across {count} transaction(s)."
                    ),
                    "suggested_fix": (
                        "If this was an auto-converting free trial, cancel now to halt future recurring charges. "
                        "Snooze if this is a desired long-term household service."
                    ),
                }
            )
    except Exception as e:
        logger.warning(f"New subscription intercept check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"New subscription check failed: {e}"})
    return alerts


def check_annual_bill_radar(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Predicts upcoming annual and semi-annual renewal lump sums within the next 30 days."""
    alerts = []
    sql = f"""
    SELECT
        merchant, category_name, functional_domain, disposition,
        cadence_type, prior_charge_amount, prior_charge_date,
        predicted_renewal_date, days_until_renewal, alert_key
    FROM `{project_id}.{dataset_id}.v_annual_bill_radar`
    WHERE days_until_renewal BETWEEN -7 AND 30
    ORDER BY days_until_renewal ASC, prior_charge_amount DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            merch = getattr(r, "merchant", "Merchant") or "Merchant"
            amt = float(getattr(r, "prior_charge_amount", 0.0) or 0.0)
            cadence = str(getattr(r, "cadence_type", "ANNUAL") or "ANNUAL").replace("_", " ").title()
            pred_date = str(getattr(r, "predicted_renewal_date", ""))
            prior_date = str(getattr(r, "prior_charge_date", ""))
            days_due = int(getattr(r, "days_until_renewal", 0) or 0)
            disp = getattr(r, "disposition", "UNKNOWN")
            key = getattr(r, "alert_key", f"annual_bill:{merch.lower().strip().replace(' ', '_')}")

            if days_due < 0:
                timing_str = f"due around now ({abs(days_due)} day(s) ago window)"
            elif days_due == 0:
                timing_str = "due today"
            else:
                timing_str = f"due in ~{days_due} days ({pred_date})"

            if disp == "RESHOPPABLE":
                action_text = (
                    f"Shop competing insurance/contract rates before {merch} auto-renews for ~${amt:.2f}, "
                    "and verify checking account has sufficient cash buffer."
                )
            else:
                action_text = (
                    f"Ensure checking account has sufficient liquidity to absorb this ~${amt:.2f} renewal, "
                    "or cancel/downgrade before the renewal date if no longer needed."
                )

            alerts.append(
                {
                    "type": "ANNUAL_BILL_RADAR",
                    "severity": "WARNING" if amt >= 250.0 else "INFO",
                    "alert_key": key,
                    "title": f"Upcoming {cadence} Bill: {merch} (~${amt:.2f})",
                    "detail": (f"Expected {timing_str}. Prior billing was ${amt:.2f} on {prior_date}."),
                    "suggested_fix": action_text,
                }
            )
    except Exception as e:
        logger.warning(f"Annual bill radar check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Annual bill radar check failed: {e}"})
    return alerts


def check_food_efficiency(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks dining & delivery percentage of food budget, avoiding early-month grocery timing skew."""
    alerts = []
    food_sql = f"""
    SELECT month, grocery_spend, dining_delivery_spend, total_food_spend, dining_percentage_of_food_budget
    FROM `{project_id}.{dataset_id}.v_food_efficiency`
    ORDER BY month DESC
    LIMIT 2;
    """
    try:
        rows = list(bq.query(food_sql).result())
        if rows:
            # If early in the month (day <= 15) and previous month data is available, evaluate the completed month
            import datetime

            today = datetime.date.today()
            if today.day <= 15 and len(rows) > 1 and float(rows[0].total_food_spend or 0) < 500.0:
                r = rows[1]
            else:
                r = rows[0]

            dining_pct = (
                float(r.dining_percentage_of_food_budget) if r.dining_percentage_of_food_budget is not None else 0.0
            )
            dining_spend = float(r.dining_delivery_spend) if r.dining_delivery_spend is not None else 0.0
            total_spend = float(r.total_food_spend) if r.total_food_spend is not None else 0.0
            if dining_pct > 35.0:
                potential_savings = round(dining_spend * 0.30, 2)
                alerts.append(
                    {
                        "type": "FOOD_LEAKAGE",
                        "severity": "WARNING",
                        "alert_key": f"food_leakage:{r.month}",
                        "title": f"High Dining/Delivery Ratio ({dining_pct:.1f}% of food budget)",
                        "detail": f"In {r.month}, dining & delivery accounted for ${dining_spend:.2f} out of ${total_spend:.2f} total food spend.",
                        "suggested_fix": f"Shifting 2 delivery meals/month to home cooking could liberate ~${potential_savings:.2f}/month.",
                    }
                )
    except Exception as e:
        logger.warning(f"Food efficiency check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Food check failed: {e}"})
    return alerts


def check_heloc_daily_cost(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks HELOC current balance and daily interest burden."""
    alerts = []
    heloc_sql = f"""
    SELECT display_name, current_balance, apr, daily_interest_cost, monthly_interest_cost
    FROM `{project_id}.{dataset_id}.v_heloc_daily_cost`
    LIMIT 1;
    """
    try:
        rows = list(bq.query(heloc_sql).result())
        if rows and float(rows[0].current_balance or 0) > 0:
            h = rows[0]
            bal = float(h.current_balance or 0)
            apr = float(h.apr or 0)
            daily = float(h.daily_interest_cost or 0)
            monthly = float(h.monthly_interest_cost or 0)
            annual_per_100 = round(apr * 100, 2)
            alerts.append(
                {
                    "type": "HELOC_OPPORTUNITY",
                    "severity": "INFO",
                    "alert_key": "heloc_daily_cost",
                    "title": f"HELOC Cost: ${daily:.2f}/day (${monthly:.2f}/mo)",
                    "detail": f"Current balance is ${bal:,.2f} at {apr * 100:.2f}% APR.",
                    "suggested_fix": f"Every $100 trimmed from discretionary spend and swept into this debt eliminates ${annual_per_100:.2f} in compounding annual interest.",
                }
            )
    except Exception as e:
        logger.warning(f"HELOC cost check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"HELOC check failed: {e}"})
    return alerts


def check_subscription_overlap(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks for redundant concurrent subscriptions in functional domains."""
    alerts = []
    sql = f"""
    SELECT
        COALESCE(functional_domain, 'GENERAL') AS domain_name,
        active_service_count,
        combined_monthly_cost,
        combined_annual_cost,
        consolidation_savings_monthly,
        consolidation_savings_annual,
        active_services
    FROM `{project_id}.{dataset_id}.v_subscription_overlap`
    ORDER BY consolidation_savings_monthly DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            domain_raw = str(getattr(r, "domain_name", None) or "Subscription")
            domain_title = domain_raw.replace("_", " ").title()
            key = f"overlap:{domain_raw.lower().strip().replace(' ', '_')}"
            count = int(getattr(r, "active_service_count", 0) or 0)
            annual_cost = float(getattr(r, "combined_annual_cost", 0) or 0)
            monthly_cost = float(getattr(r, "combined_monthly_cost", 0) or 0)
            # Savings from consolidating onto the largest plan. Quoting the whole domain
            # total implies cancelling every service including the one being kept.
            savings = float(getattr(r, "consolidation_savings_monthly", 0) or 0)

            alerts.append(
                {
                    "type": "SUBSCRIPTION_OVERLAP",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Subscription Overlap: {domain_title} ({count} active)",
                    "detail": f"Services: {r.active_services}. Combined cost: ${monthly_cost:.2f}/mo (${annual_cost:.2f}/yr).",
                    "suggested_fix": (
                        f"These {count} services cover the same need. Consolidating onto the one you use most "
                        f"frees ${savings:.2f}/month (${savings * 12:.2f}/year)."
                    ),
                }
            )
    except Exception as e:
        logger.warning(f"Subscription overlap check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription overlap check failed: {e}"})
    return alerts


def check_utility_seasonal_spike(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Flags metered utility months that overshoot the same calendar month in prior years.

    Utilities are excluded from the subscription corpus because they are regulated and have
    no cancel action, so they need their own detector. The comparison is seasonal rather
    than sequential: a winter heating bill is not a price hike over autumn.
    """
    alerts = []
    sql = f"""
    SELECT merchant, spend_month, month_total, seasonal_avg, seasonal_stddev,
           years_observed, variance_vs_season, variance_pct
    FROM `{project_id}.{dataset_id}.v_utility_seasonal_baseline`
    WHERE variance_vs_season > 0
      AND variance_pct >= 25.0
      -- With only one prior year there is no spread to speak of, so the percentage gate
      -- carries the decision alone; with more history require a genuine outlier too.
      AND (years_observed < 2 OR variance_vs_season > 2 * COALESCE(seasonal_stddev, 0))
    ORDER BY variance_vs_season DESC
    LIMIT 2;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            merch = getattr(r, "merchant", "Utility") or "Utility"
            month = str(getattr(r, "spend_month", ""))[:7]
            total = float(getattr(r, "month_total", 0) or 0)
            norm = float(getattr(r, "seasonal_avg", 0) or 0)
            over = float(getattr(r, "variance_vs_season", 0) or 0)
            pct = float(getattr(r, "variance_pct", 0) or 0)
            years = int(getattr(r, "years_observed", 0) or 0)
            alerts.append(
                {
                    "type": "UTILITY_SEASONAL_SPIKE",
                    "severity": "INFO",
                    "alert_key": f"utility_season:{merch.lower().strip().replace(' ', '_')}:{month}",
                    "title": f"Utility Above Seasonal Norm: {merch} (+{pct:.1f}%)",
                    "detail": (
                        f"{month} came in at ${total:.2f} against a ${norm:.2f} average for the same "
                        f"month across {years} prior year(s) — ${over:.2f} above seasonal normal."
                    ),
                    "suggested_fix": (
                        f"This is consumption or rate movement, not a cancellable plan. Compare the "
                        f"rate schedule on the latest {merch} statement against the prior year and check "
                        f"for a thermostat schedule or standing-load change."
                    ),
                }
            )
    except Exception as e:
        logger.warning(f"Utility seasonal check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Utility seasonal check failed: {e}"})
    return alerts


def check_micro_transaction_leakage(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks for high-cadence sub-$35 habit leaks draining cashflow."""
    alerts = []
    sql = f"""
    SELECT merchant, category_name, frequency_90d, avg_ticket, total_spend_90d, annualized_run_rate
    FROM `{project_id}.{dataset_id}.v_micro_transaction_leakage`
    ORDER BY total_spend_90d DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            merch = r.merchant or "Unknown Merchant"
            key = f"micro:{merch.lower().strip().replace(' ', '_')}"
            alerts.append(
                {
                    "type": "MICRO_TRANSACTION_LEAKAGE",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Micro-Spend Leakage: {merch}",
                    "detail": f"{r.frequency_90d} transactions averaging ${r.avg_ticket:.2f} in last 90 days (${r.total_spend_90d:.2f} total).",
                    "suggested_fix": f"Annual run-rate is ${r.annualized_run_rate:,.2f}/year. Setting a weekly cash allowance or batching visits curbs impulse leakage.",
                }
            )
    except Exception as e:
        logger.warning(f"Micro-transaction leakage check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Micro-transaction check failed: {e}"})
    return alerts


def extract_budget_caps(memories: list[str]) -> dict[str, float]:
    """
    Extracts category spend ceilings (dining, groceries) from memory bank facts.
    Robustly parses comma-formatted numbers ($9,000), normalizes annual budgets to monthly ($12,000/yr -> $1,000/mo),
    and strictly requires budget/cap context keywords to prevent accidental number matches.
    """
    caps: dict[str, float] = {}

    def _parse_amount(amt_str: str, text: str) -> float:
        val = float(amt_str.replace(",", ""))
        # If explicitly annual / per year, convert to monthly equivalent
        if re.search(r"\b(per year|annually|annual|/\s*yr)\b", text, re.IGNORECASE):
            return round(val / 12.0, 2)
        return round(val, 2)

    budget_kw = r"(?:budget|cap|limit|ceiling|target|allowance|max)"
    amt_pattern = r"\$([\d,]+(?:\.\d{2})?)"

    for mem in memories:
        if not re.search(budget_kw, mem, re.IGNORECASE):
            continue

        # Dining / Restaurants / Food Delivery
        if "dining" not in caps:
            dining_kw = r"(?:dining|restaurants?|takeout|food delivery|food)"
            m1 = re.search(rf"{dining_kw}[^$.\n]{{0,35}}?{budget_kw}[^$.\n]{{0,25}}?{amt_pattern}", mem, re.IGNORECASE)
            m2 = re.search(rf"{budget_kw}[^$.\n]{{0,35}}?{dining_kw}[^$.\n]{{0,25}}?{amt_pattern}", mem, re.IGNORECASE)
            m3 = re.search(rf"{amt_pattern}[^$.\n]{{0,25}}?{dining_kw}[^$.\n]{{0,25}}?{budget_kw}", mem, re.IGNORECASE)
            m4 = re.search(rf"{amt_pattern}[^$.\n]{{0,25}}?{budget_kw}[^$.\n]{{0,25}}?{dining_kw}", mem, re.IGNORECASE)
            m = m1 or m2 or m3 or m4
            if m:
                caps["dining"] = _parse_amount(m.group(1), mem)

        # Groceries
        if "groceries" not in caps:
            grocery_kw = r"(?:grocer(?:y|ies)|supermarkets?)"
            m1 = re.search(rf"{grocery_kw}[^$.\n]{{0,35}}?{budget_kw}[^$.\n]{{0,25}}?{amt_pattern}", mem, re.IGNORECASE)
            m2 = re.search(rf"{budget_kw}[^$.\n]{{0,35}}?{grocery_kw}[^$.\n]{{0,25}}?{amt_pattern}", mem, re.IGNORECASE)
            m3 = re.search(rf"{amt_pattern}[^$.\n]{{0,25}}?{grocery_kw}[^$.\n]{{0,25}}?{budget_kw}", mem, re.IGNORECASE)
            m4 = re.search(rf"{amt_pattern}[^$.\n]{{0,25}}?{budget_kw}[^$.\n]{{0,25}}?{grocery_kw}", mem, re.IGNORECASE)
            m = m1 or m2 or m3 or m4
            if m:
                caps["groceries"] = _parse_amount(m.group(1), mem)

    return caps


def check_memory_budget_limits(
    bq: Any,
    project_id: str,
    dataset_id: str,
    user_email: str | None = None,
) -> list[dict[str, Any]]:
    """Compares current month-to-date spending against spending caps stored in the Vertex AI Memory Bank."""
    alerts = []
    try:
        from app.memory_service import retrieve_user_memories

        memories = retrieve_user_memories(user_email)
    except Exception as e:
        logger.debug(f"Could not retrieve user memories for budget limit checks: {e}")
        return alerts

    caps = extract_budget_caps(memories)
    if not caps:
        return alerts

    # Check dining cap if present
    if "dining" in caps:
        cap = caps["dining"]
        dining_sql = f"""
        SELECT
            FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York')) AS current_month,
            COALESCE(ROUND(SUM(ABS(amount)), 2), 0.0) AS current_month_dining
        FROM `{project_id}.{dataset_id}.raw_transactions`
        WHERE (
            LOWER(category_name) IN ('restaurants', 'dining out', 'fast food', 'coffee shops', 'bars', 'restaurants & bars')
            OR LOWER(merchant_name) LIKE '%doordash%'
            OR LOWER(merchant_name) LIKE '%ubereats%'
            OR LOWER(merchant_name) LIKE '%grubhub%'
        )
        AND amount < 0
        AND NOT COALESCE(pending, FALSE)
        AND FORMAT_DATE('%Y-%m', transaction_date) = FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York'));
        """
        try:
            rows = list(bq.query(dining_sql).result())
            if rows:
                r = rows[0]
                spent = float(r.current_month_dining)
                month = r.current_month
                if spent > cap:
                    overage = round(spent - cap, 2)
                    alerts.append(
                        {
                            "type": "BUDGET_CAP_EXCEEDED",
                            "severity": "WARNING",
                            "alert_key": f"budget_cap:dining:{month}",
                            "title": f"Dining Cap Exceeded: ${spent:.2f} vs ${cap:.2f}/mo limit",
                            "detail": f"In {month}, dining & delivery spend (${spent:.2f}) has exceeded your personal target of ${cap:.2f} by ${overage:.2f}.",
                            "suggested_fix": f"Pause restaurant and delivery orders for the remainder of {month} to redirect cashflow back to debt paydown.",
                        }
                    )
                elif spent >= cap * 0.85:
                    pct = (spent / cap) * 100
                    alerts.append(
                        {
                            "type": "BUDGET_CAP_PACING",
                            "severity": "INFO",
                            "alert_key": f"budget_pacing:dining:{month}",
                            "title": f"Dining Cap Pacing Alert: ${spent:.2f} of ${cap:.2f} ({pct:.0f}%)",
                            "detail": f"In {month}, dining & delivery spend has reached ${spent:.2f} ({pct:.0f}% of your ${cap:.2f} monthly budget).",
                            "suggested_fix": f"Keep an eye on restaurant outings to stay under your ${cap:.2f} target.",
                        }
                    )
        except Exception as e:
            logger.warning(f"Memory budget check failed for dining: {e}")
            alerts.append({"type": "QUERY_ERROR", "detail": f"Memory budget check failed for dining: {e}"})

    # Check groceries cap if present
    if "groceries" in caps:
        cap = caps["groceries"]
        groceries_sql = f"""
        SELECT
            FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York')) AS current_month,
            COALESCE(ROUND(SUM(ABS(amount)), 2), 0.0) AS current_month_groceries
        FROM `{project_id}.{dataset_id}.raw_transactions`
        WHERE (
            LOWER(category_name) IN ('groceries', 'supermarkets', 'grocery')
            OR LOWER(merchant_name) LIKE '%whole foods%'
            OR LOWER(merchant_name) LIKE '%trader joe%'
            OR LOWER(merchant_name) LIKE '%kroger%'
            OR LOWER(merchant_name) LIKE '%safeway%'
            OR LOWER(merchant_name) LIKE '%king soopers%'
            OR LOWER(merchant_name) LIKE '%costco%'
            OR LOWER(merchant_name) LIKE '%sprouts%'
        )
        AND amount < 0
        AND NOT COALESCE(pending, FALSE)
        AND FORMAT_DATE('%Y-%m', transaction_date) = FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York'));
        """
        try:
            rows = list(bq.query(groceries_sql).result())
            if rows:
                r = rows[0]
                spent = float(r.current_month_groceries)
                month = r.current_month
                if spent > cap:
                    overage = round(spent - cap, 2)
                    alerts.append(
                        {
                            "type": "BUDGET_CAP_EXCEEDED",
                            "severity": "WARNING",
                            "alert_key": f"budget_cap:groceries:{month}",
                            "title": f"Groceries Cap Exceeded: ${spent:.2f} vs ${cap:.2f}/mo limit",
                            "detail": f"In {month}, grocery spend (${spent:.2f}) has exceeded your personal target of ${cap:.2f} by ${overage:.2f}.",
                            "suggested_fix": f"Review grocery receipts and pantry meal-plan for {month} to keep overhead lean.",
                        }
                    )
                elif spent >= cap * 0.85:
                    pct = (spent / cap) * 100
                    alerts.append(
                        {
                            "type": "BUDGET_CAP_PACING",
                            "severity": "INFO",
                            "alert_key": f"budget_pacing:groceries:{month}",
                            "title": f"Groceries Cap Pacing Alert: ${spent:.2f} of ${cap:.2f} ({pct:.0f}%)",
                            "detail": f"In {month}, grocery spend has reached ${spent:.2f} ({pct:.0f}% of your ${cap:.2f} monthly budget).",
                            "suggested_fix": f"Track grocery spending for the rest of {month} to stay under your ${cap:.2f} limit.",
                        }
                    )
        except Exception as e:
            logger.warning(f"Memory budget check failed for groceries: {e}")
            alerts.append({"type": "QUERY_ERROR", "detail": f"Memory budget check failed for groceries: {e}"})

    return alerts


def get_active_suppressions(bq: Any, project_id: str, dataset_id: str) -> set[str]:
    """Retrieves all currently active alert suppression keys."""
    sql = f"""
    SELECT alert_key
    FROM `{project_id}.{dataset_id}.alert_suppression`
    WHERE suppressed_until > CURRENT_TIMESTAMP()
    """
    try:
        rows = list(bq.query(sql).result())
        return {r.alert_key for r in rows if getattr(r, "alert_key", None)}
    except Exception as e:
        logger.warning(f"Failed to fetch alert suppressions: {e}")
        return set()


def filter_suppressed_alerts(alerts: list[dict[str, Any]], suppressed_keys: set[str]) -> list[dict[str, Any]]:
    """Filters out alerts whose alert_key is currently in suppressed_keys."""
    if not suppressed_keys:
        return alerts
    filtered = []
    for a in alerts:
        key = a.get("alert_key")
        if key and key in suppressed_keys:
            logger.info(f"Alert '{key}' is suppressed until later. Skipping.")
            continue
        filtered.append(a)
    return filtered


def suppress_alert(
    bq: Any,
    project_id: str,
    dataset_id: str,
    alert_key: str,
    alert_type: str = "GENERAL",
    days: int = 7,
    reason: str = "User dismissed in chat",
) -> bool:
    """Inserts or extends an alert suppression record for N days."""
    sql = f"""
    MERGE `{project_id}.{dataset_id}.alert_suppression` T
    USING (
        SELECT
            @alert_key AS alert_key,
            @alert_type AS alert_type,
            TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL @days DAY) AS suppressed_until,
            CURRENT_TIMESTAMP() AS created_at,
            @reason AS reason
    ) S
    ON T.alert_key = S.alert_key
    WHEN MATCHED THEN
        UPDATE SET suppressed_until = S.suppressed_until, reason = S.reason, created_at = S.created_at
    WHEN NOT MATCHED THEN
        INSERT (alert_key, alert_type, suppressed_until, created_at, reason)
        VALUES (S.alert_key, S.alert_type, S.suppressed_until, S.created_at, S.reason);
    """
    try:
        if bigquery and hasattr(bigquery, "QueryJobConfig"):
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("alert_key", "STRING", alert_key),
                    bigquery.ScalarQueryParameter("alert_type", "STRING", alert_type),
                    bigquery.ScalarQueryParameter("days", "INT64", days),
                    bigquery.ScalarQueryParameter("reason", "STRING", reason),
                ]
            )
            bq.query(sql, job_config=job_config).result()
        else:
            bq.query(sql).result()
        logger.info(f"Alert '{alert_key}' suppressed for {days} days.")
        return True
    except Exception as e:
        logger.error(f"Failed to suppress alert '{alert_key}': {e}")
        return False


def generate_daily_brief_synopsis(
    bq: Any,
    project_id: str,
    dataset_id: str,
    alerts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Generates a daily FinSage morning brief and executive synopsis:
    - Liquid checking reserves and coverage of fixed monthly obligations
    - Active HELOC balance and daily interest overhead ($/day)
    - Month-to-date spending total and daily burn rate pacing
    - High-priority focus items on what to pay attention to today (synthesized from active alerts)
    """
    alerts = alerts or []
    from datetime import date

    today = date.today()
    brief_date_str = today.strftime("%A, %b %-d, %Y")
    day_of_month = today.day

    liquid_balance = 0.0
    fixed_burn = 0.0
    heloc_name = "HELOC"
    heloc_balance = 0.0
    heloc_apr = 0.0
    daily_interest_cost = 0.0
    monthly_interest_cost = 0.0
    mtd_spend = 0.0
    mtd_count = 0

    sql = f"""
    WITH liquid AS (
        SELECT COALESCE(ROUND(SUM(current_balance), 2), 0.0) AS liquid_balance
        FROM `{project_id}.{dataset_id}.raw_accounts`
        WHERE (
            LOWER(COALESCE(type_name, '')) IN ('depository', 'checking')
            OR LOWER(COALESCE(subtype_name, '')) IN ('checking', 'savings', 'money_market')
        )
        AND is_asset = TRUE
    ),
    fixed AS (
        SELECT COALESCE(
            (
                SELECT ROUND(AVG(total_amount), 2)
                FROM `{project_id}.{dataset_id}.v_spend_classification`
                WHERE spend_type = 'FIXED_OVERHEAD'
                  AND month >= FORMAT_DATE('%Y-%m', DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 3 MONTH))
                  AND month < FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York'))
            ),
            (
                SELECT COALESCE(ROUND(SUM(monthly_run_rate), 2), 0.0)
                FROM `{project_id}.{dataset_id}.v_active_subscriptions`
                WHERE is_currently_active = TRUE
            ),
            0.0
        ) AS fixed_burn
    ),
    heloc AS (
        SELECT
            display_name AS account_name,
            current_balance AS heloc_balance,
            apr AS heloc_apr,
            daily_interest_cost,
            monthly_interest_cost
        FROM `{project_id}.{dataset_id}.v_heloc_daily_cost`
        ORDER BY current_balance DESC
        LIMIT 1
    ),
    mtd AS (
        SELECT
            COALESCE(ROUND(SUM(ABS(amount)), 2), 0.0) AS mtd_spend,
            COUNT(*) AS mtd_count
        FROM `{project_id}.{dataset_id}.raw_transactions`
        WHERE amount < 0
          AND NOT COALESCE(pending, FALSE)
          AND FORMAT_DATE('%Y-%m', transaction_date) = FORMAT_DATE('%Y-%m', CURRENT_DATE('America/New_York'))
          AND LOWER(COALESCE(category_name, '')) NOT IN (
              'transfer', 'credit card payment', 'balance transfer',
              'loan payment', 'investment', 'savings'
          )
    )
    SELECT
        CURRENT_DATE('America/New_York') AS brief_date,
        EXTRACT(DAY FROM CURRENT_DATE('America/New_York')) AS day_of_month,
        l.liquid_balance,
        f.fixed_burn,
        h.account_name AS heloc_name,
        h.heloc_balance,
        h.heloc_apr,
        h.daily_interest_cost,
        h.monthly_interest_cost,
        m.mtd_spend,
        m.mtd_count
    FROM liquid l
    CROSS JOIN fixed f
    LEFT JOIN heloc h ON TRUE
    CROSS JOIN mtd m;
    """
    query_failed = False
    try:
        rows = list(bq.query(sql).result())
        if rows:
            r = rows[0]
            if getattr(r, "brief_date", None):
                brief_date_str = str(r.brief_date)
            if getattr(r, "day_of_month", None):
                day_of_month = int(r.day_of_month)
            liquid_balance = float(getattr(r, "liquid_balance", 0.0) or 0.0)
            fixed_burn = float(getattr(r, "fixed_burn", 0.0) or 0.0)
            heloc_name = str(getattr(r, "heloc_name", "HELOC") or "HELOC")
            heloc_balance = float(getattr(r, "heloc_balance", 0.0) or 0.0)
            heloc_apr = float(getattr(r, "heloc_apr", 0.0) or 0.0)
            daily_interest_cost = float(getattr(r, "daily_interest_cost", 0.0) or 0.0)
            monthly_interest_cost = float(getattr(r, "monthly_interest_cost", 0.0) or 0.0)
            mtd_spend = float(getattr(r, "mtd_spend", 0.0) or 0.0)
            mtd_count = int(getattr(r, "mtd_count", 0) or 0)
        else:
            query_failed = True
    except Exception as e:
        logger.warning(f"Failed to query posture stats for morning brief: {e}")
        query_failed = True

    coverage_ratio = (liquid_balance / fixed_burn) if fixed_burn > 0 else 0.0
    daily_burn_rate = (mtd_spend / day_of_month) if day_of_month > 0 else 0.0
    pacing_desc = f"~${daily_burn_rate:,.2f}/day" if day_of_month > 3 else "pacing calibrating"

    posture_lines = []
    posture_md_lines = []

    if query_failed:
        posture_lines.append("⚠️ <b>Account Posture:</b> BigQuery live data unavailable (query error).")
        posture_md_lines.append("• ⚠️ **Account Posture**: BigQuery live data unavailable (query error).")
    else:
        if liquid_balance < 0:
            posture_lines.append(f"🏦 <b>Liquid Cash:</b> -${abs(liquid_balance):,.2f} ⚠️ (OVERDRAWN)")
            posture_md_lines.append(f"• ⚠️ **Liquid Reserves**: -${abs(liquid_balance):,.2f} (OVERDRAWN)")
        elif liquid_balance > 0:
            buffer_str = f" ({coverage_ratio:.1f}x monthly buffer)" if coverage_ratio > 0 else ""
            posture_lines.append(f"🏦 <b>Liquid Cash:</b> ${liquid_balance:,.2f}{buffer_str}")
            posture_md_lines.append(f"• **Liquid Reserves**: ${liquid_balance:,.2f}{buffer_str}")
        else:
            posture_lines.append("🏦 <b>Liquid Cash:</b> $0.00")
            posture_md_lines.append("• **Liquid Reserves**: $0.00")

        if heloc_balance > 0:
            posture_lines.append(
                f"💳 <b>HELOC Carry:</b> ${daily_interest_cost:,.2f}/day (${monthly_interest_cost:,.2f}/mo) • Balance: ${heloc_balance:,.2f}"
            )
            posture_md_lines.append(
                f"• **HELOC Daily Carry**: ${daily_interest_cost:,.2f}/day (${monthly_interest_cost:,.2f}/mo) — Balance: ${heloc_balance:,.2f}"
            )
        if mtd_spend > 0:
            posture_lines.append(
                f"📊 <b>Month-to-Date Spend:</b> ${mtd_spend:,.2f} (Day {day_of_month} • {pacing_desc})"
            )
            posture_md_lines.append(
                f"• **Month-to-Date Spend**: ${mtd_spend:,.2f} (Day {day_of_month} • {pacing_desc})"
            )

    posture_text = "<br>".join(posture_lines)
    posture_md = "\n".join(posture_md_lines)

    focus_items = []
    focus_items_md = []

    if query_failed:
        focus_items.append(
            "⚠️ <b>Data Degraded:</b> Live BigQuery posture query failed. Check credentials and dataset views."
        )
        focus_items_md.append(
            "⚠️ **Data Degraded**: Live BigQuery posture query failed. Check credentials and dataset views."
        )
    else:
        if liquid_balance < 0:
            focus_items.append(
                f"🚨 <b>Overdrawn Checking:</b> Liquid balance is negative (-${abs(liquid_balance):,.2f}). Replenish immediately."
            )
            focus_items_md.append(
                f"🚨 **Overdrawn Checking**: Liquid balance is negative (-${abs(liquid_balance):,.2f}). Replenish immediately."
            )

        dup_alerts = [a for a in alerts if a.get("type") == "DUPLICATE_CHARGE"]
        for da in dup_alerts[:1]:
            focus_items.append(f"⚡ <b>Duplicate Charge:</b> {da.get('title', '')} — {da.get('detail', '')}")
            focus_items_md.append(f"**Duplicate Charge**: {da.get('title', '')} — {da.get('detail', '')}")

        new_sub_alerts = [a for a in alerts if a.get("type") == "NEW_SUBSCRIPTION_DETECTED"]
        for nsa in new_sub_alerts[:1]:
            focus_items.append(f"🆕 <b>New Recurring Plan:</b> {nsa.get('title', '')}. {nsa.get('suggested_fix', '')}")
            focus_items_md.append(f"**New Recurring Plan**: {nsa.get('title', '')}. {nsa.get('suggested_fix', '')}")

        annual_alerts = [a for a in alerts if a.get("type") == "ANNUAL_BILL_RADAR"]
        for aa in annual_alerts[:1]:
            focus_items.append(f"📅 <b>Annual Bill Radar:</b> {aa.get('title', '')} — {aa.get('detail', '')}")
            focus_items_md.append(f"**Annual Bill Radar**: {aa.get('title', '')} — {aa.get('detail', '')}")

        price_alerts = [a for a in alerts if a.get("type") == "PRICE_CREEP"]
        for pa in price_alerts[:2]:
            t = pa.get("title", "").replace("Subscription Price Hike: ", "")
            focus_items.append(f"🔍 <b>Price Hike:</b> {t} — {pa.get('detail', '')}. {pa.get('suggested_fix', '')}")
            focus_items_md.append(f"**Price Hike**: {t} — {pa.get('detail', '')}. {pa.get('suggested_fix', '')}")

        food_alerts = [a for a in alerts if a.get("type") == "FOOD_LEAKAGE"]
        for fa in food_alerts[:1]:
            focus_items.append(f"🍔 <b>Food Pacing:</b> {fa.get('title', '')}. {fa.get('suggested_fix', '')}")
            focus_items_md.append(f"**Food Pacing**: {fa.get('title', '')}. {fa.get('suggested_fix', '')}")

        overlap_alerts = [a for a in alerts if a.get("type") == "SUBSCRIPTION_OVERLAP"]
        for oa in overlap_alerts[:1]:
            focus_items.append(
                f"🔄 <b>Subscription Duplication:</b> {oa.get('title', '')}. {oa.get('suggested_fix', '')}"
            )
            focus_items_md.append(f"**Subscription Duplication**: {oa.get('title', '')}. {oa.get('suggested_fix', '')}")

        budget_alerts = [a for a in alerts if a.get("type") in ("BUDGET_CAP_EXCEEDED", "BUDGET_CAP_PACING")]
        for ba in budget_alerts[:1]:
            focus_items.append(f"⚠️ <b>Budget Warning:</b> {ba.get('title', '')} ({ba.get('detail', '')})")
            focus_items_md.append(f"**Budget Warning**: {ba.get('title', '')} ({ba.get('detail', '')})")

        micro_alerts = [a for a in alerts if a.get("type") == "MICRO_TRANSACTION_LEAKAGE"]
        for ma in micro_alerts[:1]:
            focus_items.append(f"☕ <b>Convenience Leakage:</b> {ma.get('title', '')}. {ma.get('suggested_fix', '')}")
            focus_items_md.append(f"**Convenience Leakage**: {ma.get('title', '')}. {ma.get('suggested_fix', '')}")

        if daily_interest_cost >= 15.0:
            focus_items.append(
                f"💳 <b>HELOC Paydown:</b> Running at ${daily_interest_cost:,.2f}/day. Prioritize sweeping surplus cash to eliminate carry."
            )
            focus_items_md.append(
                f"**HELOC Paydown**: Running at ${daily_interest_cost:,.2f}/day. Prioritize sweeping surplus cash to eliminate carry."
            )

        if not focus_items:
            focus_items.append(
                "✅ <b>All Systems Normal:</b> Spend is tracking normally and no recurring charge spikes or anomalies were detected today."
            )
            focus_items_md.append(
                "**All Systems Normal**: Spend is tracking normally and no recurring charge spikes or anomalies were detected today."
            )

    return {
        "date": brief_date_str,
        "day_of_month": day_of_month,
        "liquid_balance": liquid_balance,
        "fixed_burn": fixed_burn,
        "coverage_ratio": round(coverage_ratio, 2),
        "heloc_name": heloc_name,
        "heloc_balance": heloc_balance,
        "heloc_apr": heloc_apr,
        "daily_interest_cost": daily_interest_cost,
        "monthly_interest_cost": monthly_interest_cost,
        "mtd_spend": mtd_spend,
        "mtd_count": mtd_count,
        "daily_burn_rate": round(daily_burn_rate, 2),
        "posture_text": posture_text,
        "posture_md": posture_md,
        "focus_items": focus_items,
        "focus_items_md": focus_items_md,
        "is_error": query_failed,
    }


def build_chat_card_v2(
    alerts: list[dict[str, Any]],
    synopsis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Constructs Google Chat Card V2 representation of the daily morning brief synopsis
    and spend optimization alerts with interactive snooze actions.
    """
    sections: list[dict[str, Any]] = []

    # Section 1: Morning Financial Synopsis (if synopsis provided)
    if synopsis:
        synopsis_widgets: list[dict[str, Any]] = []
        posture_text = synopsis.get("posture_text")
        is_error = bool(synopsis.get("is_error"))
        header_text = "⚠️ Morning Financial Synopsis (Data Degraded)" if is_error else "🌅 Morning Financial Synopsis"
        posture_icon = "ERROR" if is_error else "DOLLAR"

        if posture_text:
            synopsis_widgets.append(
                {
                    "decoratedText": {
                        "topLabel": f"DAILY POSTURE SNAPSHOT • {synopsis.get('date', 'TODAY')}",
                        "text": posture_text,
                        "startIcon": {"knownIcon": posture_icon},
                        "wrapText": True,
                    }
                }
            )
        focus_items = synopsis.get("focus_items", [])
        if focus_items:
            synopsis_widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "WHAT TO PAY ATTENTION TO TODAY",
                        "text": "<br>".join(focus_items),
                        "startIcon": {"knownIcon": "DESCRIPTION"},
                        "wrapText": True,
                    }
                }
            )
        if synopsis_widgets:
            sections.append(
                {
                    "header": header_text,
                    "widgets": synopsis_widgets,
                }
            )

    # Section 2: Daily Optimization Opportunities
    alert_widgets: list[dict[str, Any]] = []
    for a in alerts:
        if not a.get("title"):
            continue
        alert_widgets.append(
            {
                "decoratedText": {
                    "topLabel": a.get("type", "FINANCIAL ADVISORY").replace("_", " "),
                    "text": f'<b>{a["title"]}</b><br><font color="#5f6368">{a["detail"]}</font><br>👉 <b>Action:</b> {a["suggested_fix"]}',
                    "wrapText": True,
                }
            }
        )
        if a.get("alert_key"):
            snooze_key = str(a["alert_key"])
            snooze_days = 7
            snooze_ts = int(time.time())
            try:
                from app.monarch_service import generate_snooze_signature

                snooze_sig = generate_snooze_signature(snooze_key, snooze_days, snooze_ts)
            except Exception:
                snooze_sig = ""

            alert_widgets.append(
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "💤 Snooze 7 Days",
                                "onClick": {
                                    "action": {
                                        "function": "snooze_alert",
                                        "parameters": [
                                            {"key": "action", "value": "snooze_alert"},
                                            {"key": "alert_key", "value": snooze_key},
                                            {"key": "alert_type", "value": str(a.get("type", "GENERAL"))},
                                            {"key": "days", "value": str(snooze_days)},
                                            {"key": "ts", "value": str(snooze_ts)},
                                            {"key": "sig", "value": snooze_sig},
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                }
            )

    if not alert_widgets:
        alert_widgets.append(
            {
                "decoratedText": {
                    "text": "✅ No active financial anomalies or spending leaks detected.",
                    "startIcon": {"knownIcon": "MEMBERSHIP"},
                }
            }
        )

    sections.append(
        {
            "header": "Daily Optimization Opportunities",
            "widgets": alert_widgets,
        }
    )

    card_header = {
        "title": "FinSage",
        "subtitle": "Daily Synopsis & Spend Advisory" if synopsis else "Daily Spend Optimization & Debt Advisory",
        "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
        "imageType": "CIRCLE",
    }

    notification_text = (
        "🌅 *FinSage*: Morning financial synopsis and daily advisory recommendations."
        if synopsis
        else "🔔 *FinSage*: Proactive Advisory Scan completed with new recommendations."
    )

    return {
        "text": notification_text,
        "cardsV2": [
            {
                "cardId": "financialAdvisorDailyAlert",
                "card": {
                    "header": card_header,
                    "sections": sections,
                },
            }
        ],
    }


def build_snooze_success_card(alert_key: str, alert_type: str, days: int) -> dict[str, Any]:
    """Constructs a Google Chat Card v2 confirmation card for a snoozed alert."""
    clean_type = alert_type.replace("_", " ").title()
    return {
        "cardId": f"snoozeSuccess_{alert_key}",
        "card": {
            "header": {
                "title": "FinSage",
                "subtitle": "Alert Snoozed",
                "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                "imageType": "CIRCLE",
            },
            "sections": [
                {
                    "widgets": [
                        {
                            "decoratedText": {
                                "topLabel": "Suppression Active",
                                "text": f'💤 <b>{clean_type}</b> (<code>{alert_key}</code>) has been snoozed for <b>{days} days</b>.<br><font color="#5f6368">It will not appear in daily proactive scans until the snooze period ends.</font>',
                                "startIcon": {"knownIcon": "CLOCK"},
                                "wrapText": True,
                            }
                        }
                    ]
                }
            ],
        },
    }


def build_markdown_fallback(
    alerts: list[dict[str, Any]],
    synopsis: dict[str, Any] | None = None,
) -> str:
    """Constructs plain text / Markdown fallback for Slack, Discord, or terminal/chat output."""
    lines = []
    if synopsis:
        lines.append(f"**🌅 FinSage Morning Brief — {synopsis.get('date', '')}**\n")
        if synopsis.get("posture_md"):
            lines.append(f"**Daily Posture Snapshot:**\n{synopsis['posture_md']}\n")
        if synopsis.get("focus_items_md"):
            lines.append("**What to Pay Attention to Today:**")
            for item in synopsis["focus_items_md"]:
                lines.append(f"• {item}")
            lines.append("")
        lines.append("---\n")

    lines.append("**🔔 Daily Optimization Opportunities**\n")
    if not alerts or not any(a.get("title") for a in alerts):
        lines.append("✅ No active financial anomalies or spending leaks detected.")
    else:
        for a in alerts:
            if a.get("title"):
                lines.append(f"• **{a['title']}**\n  _{a['detail']}_\n  👉 **Action**: {a['suggested_fix']}\n")
    return "\n".join(lines)


def collect_all_alerts(
    bq: Any,
    target_project: str,
    target_dataset: str,
    user_email: str | None = None,
) -> list[dict[str, Any]]:
    """Runs all spend optimization checks synchronously and filters out suppressed alerts."""
    raw_alerts: list[dict[str, Any]] = []
    raw_alerts.extend(check_subscription_price_creep(bq, target_project, target_dataset))
    raw_alerts.extend(check_duplicate_charges(bq, target_project, target_dataset))
    raw_alerts.extend(check_new_subscriptions(bq, target_project, target_dataset))
    raw_alerts.extend(check_annual_bill_radar(bq, target_project, target_dataset))
    raw_alerts.extend(check_food_efficiency(bq, target_project, target_dataset))
    raw_alerts.extend(check_heloc_daily_cost(bq, target_project, target_dataset))
    raw_alerts.extend(check_subscription_overlap(bq, target_project, target_dataset))
    raw_alerts.extend(check_utility_seasonal_spike(bq, target_project, target_dataset))
    raw_alerts.extend(check_micro_transaction_leakage(bq, target_project, target_dataset))
    raw_alerts.extend(check_memory_budget_limits(bq, target_project, target_dataset, user_email))

    suppressed = get_active_suppressions(bq, target_project, target_dataset)
    return filter_suppressed_alerts(raw_alerts, suppressed)


async def execute_alert_scan(
    bq_client: Any | None = None,
    project_id: str | None = None,
    dataset_id: str | None = None,
    webhook_url: str | None = None,
    user_email: str | None = None,
) -> dict:
    """
    Scans BigQuery financial optimization views and generates proactive alerts
    with concrete suggestions to reduce spend and accelerate HELOC paydown.
    Optionally posts the summary to ALERT_WEBHOOK_URL (Google Chat/Slack/Discord).
    All blocking BigQuery queries and HTTP requests are offloaded to worker threads.
    """
    target_project = project_id or BQ_PROJECT_ID
    target_dataset = dataset_id or BQ_DATASET_ID

    if not target_project:
        raise ValueError("PROJECT_ID not set.")

    if bq_client:
        bq = bq_client
    elif bigquery:
        bq = bigquery.Client(project=target_project)
    else:
        raise RuntimeError("google-cloud-bigquery is not installed and no bq_client provided.")

    # Offload blocking BigQuery queries to worker thread
    alerts = await asyncio.to_thread(collect_all_alerts, bq, target_project, target_dataset, user_email)
    synopsis = await asyncio.to_thread(generate_daily_brief_synopsis, bq, target_project, target_dataset, alerts)

    # Dispatch to Webhook if configured (non-blocking)
    target_webhook = webhook_url or resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if target_webhook and requests:
        try:
            if "chat.googleapis.com" in target_webhook:
                payload = build_chat_card_v2(alerts, synopsis=synopsis)
            else:
                msg = build_markdown_fallback(alerts, synopsis=synopsis)
                payload = {"content": msg, "text": msg}

            resp = await asyncio.to_thread(requests.post, target_webhook, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Webhook post failed: {e}")

    return {
        "status": "success",
        "brief_synopsis": synopsis,
        "alert_count": len([a for a in alerts if a.get("type") != "QUERY_ERROR"]),
        "alerts": alerts,
        "webhook_dispatched": webhook_sent,
    }


def get_daily_morning_brief() -> str:
    """
    Retrieves the daily FinSage morning brief and executive synopsis,
    including liquid cash reserves, monthly fixed burn coverage, HELOC carrying cost,
    month-to-date spending pacing, and key focus items to pay attention to today.
    """
    target_project = BQ_PROJECT_ID
    target_dataset = BQ_DATASET_ID
    if not target_project:
        return "Error: BQ_PROJECT_ID is not configured."

    try:
        if bigquery:
            bq = bigquery.Client(project=target_project)
        else:
            return "BigQuery client is not available."
    except Exception as e:
        return f"Error initializing BigQuery client: {e}"

    alerts = collect_all_alerts(bq, target_project, target_dataset)
    synopsis = generate_daily_brief_synopsis(bq, target_project, target_dataset, alerts)
    return build_markdown_fallback(alerts, synopsis=synopsis)


def snooze_spend_alert(alert_key_or_name: str, days: int = 7) -> str:
    """
    Snooze or suppress a proactive financial alert (e.g. price creep, food leakage,
    micro-transaction habit, or subscription overlap) so it is not repeatedly flagged in daily scans.

    Args:
        alert_key_or_name: The alert key (e.g. 'price_creep:netflix', 'overlap:streaming', 'micro:starbucks')
                           or merchant name to snooze.
        days: The number of days to suppress the alert (default 7).
    """
    if not alert_key_or_name or not str(alert_key_or_name).strip():
        return "Please specify an alert key or merchant name to snooze."

    try:
        clamped_days = max(1, min(int(days), 90))
    except (ValueError, TypeError):
        clamped_days = 7

    clean_key = str(alert_key_or_name).strip().lower().replace(" ", "_")
    target_project = BQ_PROJECT_ID
    target_dataset = BQ_DATASET_ID

    if bigquery:
        bq = bigquery.Client(project=target_project)
    else:
        return "BigQuery client is not available."

    success = suppress_alert(
        bq=bq,
        project_id=target_project,
        dataset_id=target_dataset,
        alert_key=clean_key,
        alert_type="USER_REQUESTED",
        days=clamped_days,
        reason=f"Snoozed by user request via Gemini chat for {clamped_days} days",
    )

    if success:
        return f"Successfully snoozed alert '{clean_key}' for {clamped_days} days."
    else:
        return f"Failed to snooze alert '{clean_key}'. Please check logs."
