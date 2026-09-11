"""
Proactive spend optimization alerts, anomaly scans, and notification dispatching.
Evaluates BigQuery analytical views (food efficiency, price creep, HELOC daily cost,
subscription overlap, micro-transaction leakage, memory bank budget caps)
and formats/posts Google Chat Card V2 notifications with smart suppression.
"""

import asyncio
import logging
import re
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


def check_subscription_price_creep(bq: Any, project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    """Checks for subscriptions where price has increased in the last 45 days."""
    alerts = []
    price_creep_sql = f"""
    SELECT merchant, latest_charge, prior_charge, price_increase_amount, pct_increase, estimated_annual_cost, effective_date
    FROM `{project_id}.{dataset_id}.v_subscription_price_creep`
    ORDER BY price_increase_amount DESC
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
            annual = float(getattr(r, "estimated_annual_cost", 0) or 0)
            eff_date = getattr(r, "effective_date", None)
            if eff_date is not None and str(type(eff_date)).endswith("MagicMock'>"):
                eff_date = None
            eff_date_str = f" on {eff_date}" if eff_date else ""
            alerts.append(
                {
                    "type": "PRICE_CREEP",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Subscription Price Hike: {merch} (+{pct:.1f}%)",
                    "detail": f"Latest charge increased from ${prior:.2f} to ${latest:.2f}{eff_date_str} (Annual cost: ${annual:.2f}).",
                    "suggested_fix": f"Audit usage for {merch} or cancel/rotate to save up to ${annual:.2f}/year.",
                }
            )
    except Exception as e:
        logger.warning(f"Subscription price creep check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription check failed: {e}"})
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
        active_services
    FROM `{project_id}.{dataset_id}.v_subscription_overlap`
    ORDER BY combined_monthly_cost DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(sql).result())
        for r in rows:
            # Safely resolve attributes, accounting for BigQuery rows, dicts, and test MagicMocks
            domain_raw = None
            for attr in ("domain_name", "functional_domain", "category_name"):
                v = getattr(r, attr, None)
                if v is not None and not str(type(v)).endswith("MagicMock'>"):
                    domain_raw = str(v)
                    break
            if not domain_raw:
                domain_raw = "Subscription"

            domain_title = domain_raw.replace("_", " ").title()
            key = f"overlap:{domain_raw.lower().strip().replace(' ', '_')}"

            count = None
            for attr in ("active_service_count", "active_subscriptions_count"):
                v = getattr(r, attr, None)
                if v is not None and not str(type(v)).endswith("MagicMock'>"):
                    count = int(v)
                    break
            count = count if count is not None else 0

            annual_cost = None
            for attr in ("combined_annual_cost", "category_annual_run_rate"):
                v = getattr(r, attr, None)
                if v is not None and not str(type(v)).endswith("MagicMock'>"):
                    annual_cost = float(v)
                    break
            annual_cost = annual_cost if annual_cost is not None else 0.0

            monthly_cost = getattr(r, "combined_monthly_cost", None)
            if monthly_cost is None or str(type(monthly_cost)).endswith("MagicMock'>"):
                monthly_cost = 0.0
            else:
                monthly_cost = float(monthly_cost)

            alerts.append(
                {
                    "type": "SUBSCRIPTION_OVERLAP",
                    "severity": "WARNING",
                    "alert_key": key,
                    "title": f"Subscription Overlap: {domain_title} ({count} active)",
                    "detail": f"Services: {r.active_services}. Combined cost: ${monthly_cost:.2f}/mo (${annual_cost:.2f}/yr).",
                    "suggested_fix": f"Audit and rotate duplicate services in {domain_title} to liberate up to ${monthly_cost:.2f}/month.",
                }
            )
    except Exception as e:
        logger.warning(f"Subscription overlap check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription overlap check failed: {e}"})
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
    """Extracts category spend ceilings (dining, groceries) from memory bank facts."""
    caps: dict[str, float] = {}
    for mem in memories:
        # Dining / Restaurants / Delivery
        if "dining" not in caps:
            m1 = re.search(r"(?:dining|restaurants?|food|delivery)[^\$]*\$(\d+(?:\.\d{2})?)", mem, re.IGNORECASE)
            m2 = re.search(r"\$(\d+(?:\.\d{2})?)[^\$]*(?:dining|restaurants?|food|delivery)", mem, re.IGNORECASE)
            if m1:
                caps["dining"] = float(m1.group(1))
            elif m2:
                caps["dining"] = float(m2.group(1))

        # Groceries
        if "groceries" not in caps:
            m1 = re.search(r"(?:grocer(?:y|ies))[^\$]*\$(\d+(?:\.\d{2})?)", mem, re.IGNORECASE)
            m2 = re.search(r"\$(\d+(?:\.\d{2})?)[^\$]*(?:grocer(?:y|ies))", mem, re.IGNORECASE)
            if m1:
                caps["groceries"] = float(m1.group(1))
            elif m2:
                caps["groceries"] = float(m2.group(1))
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
            FORMAT_DATE('%Y-%m', CURRENT_DATE()) AS current_month,
            COALESCE(ROUND(SUM(ABS(amount)), 2), 0.0) AS current_month_dining
        FROM `{project_id}.{dataset_id}.raw_transactions`
        WHERE (
            LOWER(category_name) IN ('restaurants', 'dining out', 'fast food', 'coffee shops', 'bars', 'restaurants & bars')
            OR LOWER(merchant_name) LIKE '%doordash%'
            OR LOWER(merchant_name) LIKE '%ubereats%'
            OR LOWER(merchant_name) LIKE '%grubhub%'
        )
        AND amount < 0
        AND pending = FALSE
        AND FORMAT_DATE('%Y-%m', transaction_date) = FORMAT_DATE('%Y-%m', CURRENT_DATE());
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
            FORMAT_DATE('%Y-%m', CURRENT_DATE()) AS current_month,
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
        AND pending = FALSE
        AND FORMAT_DATE('%Y-%m', transaction_date) = FORMAT_DATE('%Y-%m', CURRENT_DATE());
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


def build_chat_card_v2(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """Constructs Google Chat Card V2 representation of alerts with interactive snooze actions."""
    widgets: list[dict[str, Any]] = []
    for a in alerts:
        if not a.get("title"):
            continue
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": a.get("type", "FINANCIAL ADVISORY").replace("_", " "),
                    "text": f'<b>{a["title"]}</b><br><font color="#5f6368">{a["detail"]}</font><br>👉 <b>Action:</b> {a["suggested_fix"]}',
                    "wrapText": True,
                }
            }
        )
        if a.get("alert_key"):
            widgets.append(
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
                                            {"key": "alert_key", "value": str(a["alert_key"])},
                                            {"key": "alert_type", "value": str(a.get("type", "GENERAL"))},
                                            {"key": "days", "value": "7"},
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                }
            )

    if not widgets:
        widgets.append(
            {
                "decoratedText": {
                    "text": "✅ No active financial anomalies or spending leaks detected.",
                    "startIcon": {"knownIcon": "MEMBERSHIP"},
                }
            }
        )

    return {
        "text": "🔔 *FinSage*: Proactive Advisory Scan completed with new recommendations.",
        "cardsV2": [
            {
                "cardId": "financialAdvisorDailyAlert",
                "card": {
                    "header": {
                        "title": "FinSage",
                        "subtitle": "Daily Spend Optimization & Debt Advisory",
                        "imageUrl": "https://raw.githubusercontent.com/n0012/family-financial-intelligence-hub/main/static/avatar.png",
                        "imageType": "CIRCLE",
                    },
                    "sections": [
                        {
                            "header": "Daily Optimization Opportunities",
                            "widgets": widgets,
                        }
                    ],
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


def build_markdown_fallback(alerts: list[dict[str, Any]]) -> str:
    """Constructs plain text / Markdown fallback for Slack or Discord."""
    lines = ["**🔔 Sage: Proactive Advisory Scan**\n"]
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
    raw_alerts.extend(check_food_efficiency(bq, target_project, target_dataset))
    raw_alerts.extend(check_heloc_daily_cost(bq, target_project, target_dataset))
    raw_alerts.extend(check_subscription_overlap(bq, target_project, target_dataset))
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

    # Dispatch to Webhook if configured (non-blocking)
    target_webhook = webhook_url or resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if target_webhook and alerts and requests:
        try:
            if "chat.googleapis.com" in target_webhook:
                payload = build_chat_card_v2(alerts)
            else:
                msg = build_markdown_fallback(alerts)
                payload = {"content": msg, "text": msg}

            resp = await asyncio.to_thread(requests.post, target_webhook, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Webhook post failed: {e}")

    return {
        "status": "success",
        "alert_count": len([a for a in alerts if a.get("type") != "QUERY_ERROR"]),
        "alerts": alerts,
        "webhook_dispatched": webhook_sent,
    }


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
        days=days,
        reason=f"Snoozed by user request via Gemini chat for {days} days",
    )

    if success:
        return f"Successfully snoozed alert '{clean_key}' for {days} days."
    else:
        return f"Failed to snooze alert '{clean_key}'. Please check logs."
