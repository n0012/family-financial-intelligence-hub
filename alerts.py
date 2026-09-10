"""
Proactive spend optimization alerts and notification dispatching.
Evaluates BigQuery analytical views (food efficiency, price creep, HELOC daily cost)
and formats/posts Google Chat Card V2 notifications.
"""
import asyncio
import logging
from typing import Optional, List, Dict, Any
try:
    import requests
except ImportError:
    requests = None

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None

from config import BQ_PROJECT_ID, BQ_DATASET_ID, resolve_secret

logger = logging.getLogger("monarch-gemini.alerts")


def check_subscription_price_creep(bq: Any, project_id: str, dataset_id: str) -> List[Dict[str, Any]]:
    """Checks for subscriptions where price has increased."""
    alerts = []
    price_creep_sql = f"""
    SELECT merchant, min_charge, max_charge, estimated_annual_cost
    FROM `{project_id}.{dataset_id}.v_active_subscriptions`
    WHERE has_price_increased = TRUE
    ORDER BY estimated_annual_cost DESC
    LIMIT 3;
    """
    try:
        rows = list(bq.query(price_creep_sql).result())
        for r in rows:
            alerts.append({
                "type": "PRICE_CREEP",
                "severity": "WARNING",
                "title": f"Subscription Price Hike: {r.merchant}",
                "detail": f"Charge increased from ${r.min_charge:.2f} to ${r.max_charge:.2f} (Annual cost: ${r.estimated_annual_cost:.2f}).",
                "suggested_fix": f"Audit usage for {r.merchant} or cancel/rotate to save up to ${r.estimated_annual_cost:.2f}/year.",
            })
    except Exception as e:
        logger.warning(f"Subscription price creep check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Subscription check failed: {e}"})
    return alerts


def check_food_efficiency(bq: Any, project_id: str, dataset_id: str) -> List[Dict[str, Any]]:
    """Checks dining & delivery percentage of food budget."""
    alerts = []
    food_sql = f"""
    SELECT month, grocery_spend, dining_delivery_spend, total_food_spend, dining_percentage_of_food_budget
    FROM `{project_id}.{dataset_id}.v_food_efficiency`
    ORDER BY month DESC
    LIMIT 1;
    """
    try:
        rows = list(bq.query(food_sql).result())
        if rows:
            r = rows[0]
            dining_pct = float(r.dining_percentage_of_food_budget) if r.dining_percentage_of_food_budget is not None else 0.0
            dining_spend = float(r.dining_delivery_spend) if r.dining_delivery_spend is not None else 0.0
            total_spend = float(r.total_food_spend) if r.total_food_spend is not None else 0.0
            if dining_pct > 35.0:
                potential_savings = round(dining_spend * 0.30, 2)
                alerts.append({
                    "type": "FOOD_LEAKAGE",
                    "severity": "WARNING",
                    "title": f"High Dining/Delivery Ratio ({dining_pct:.1f}% of food budget)",
                    "detail": f"In {r.month}, dining & delivery accounted for ${dining_spend:.2f} out of ${total_spend:.2f} total food spend.",
                    "suggested_fix": f"Shifting 2 delivery meals/month to home cooking could liberate ~${potential_savings:.2f}/month.",
                })
    except Exception as e:
        logger.warning(f"Food efficiency check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"Food check failed: {e}"})
    return alerts


def check_heloc_daily_cost(bq: Any, project_id: str, dataset_id: str) -> List[Dict[str, Any]]:
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
            alerts.append({
                "type": "HELOC_OPPORTUNITY",
                "severity": "INFO",
                "title": f"HELOC Cost: ${daily:.2f}/day (${monthly:.2f}/mo)",
                "detail": f"Current balance is ${bal:,.2f} at {apr*100:.2f}% APR.",
                "suggested_fix": f"Every $100 trimmed from discretionary spend and swept into this debt eliminates ${annual_per_100:.2f} in compounding annual interest.",
            })
    except Exception as e:
        logger.warning(f"HELOC cost check failed: {e}")
        alerts.append({"type": "QUERY_ERROR", "detail": f"HELOC check failed: {e}"})
    return alerts


def build_chat_card_v2(alerts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Constructs Google Chat Card V2 representation of alerts."""
    widgets = []
    for a in alerts:
        if a.get("title"):
            widgets.append({
                "decoratedText": {
                    "topLabel": a.get("type", "FINANCIAL ADVISORY").replace("_", " "),
                    "text": f"<b>{a['title']}</b><br><font color=\"#5f6368\">{a['detail']}</font><br>👉 <b>Action:</b> {a['suggested_fix']}",
                    "wrapText": True,
                }
            })
    return {
        "text": "🔔 *Sage*: Proactive Advisory Scan completed with new recommendations.",
        "cardsV2": [
            {
                "cardId": "financialAdvisorDailyAlert",
                "card": {
                    "header": {
                        "title": "Sage",
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


def build_markdown_fallback(alerts: List[Dict[str, Any]]) -> str:
    """Constructs plain text / Markdown fallback for Slack or Discord."""
    lines = ["**🔔 Sage: Proactive Advisory Scan**\n"]
    for a in alerts:
        if a.get("title"):
            lines.append(f"• **{a['title']}**\n  _{a['detail']}_\n  👉 **Action**: {a['suggested_fix']}\n")
    return "\n".join(lines)


def collect_all_alerts(bq: Any, target_project: str, target_dataset: str) -> List[Dict[str, Any]]:
    """Runs all spend optimization checks synchronously."""
    results: List[Dict[str, Any]] = []
    results.extend(check_subscription_price_creep(bq, target_project, target_dataset))
    results.extend(check_food_efficiency(bq, target_project, target_dataset))
    results.extend(check_heloc_daily_cost(bq, target_project, target_dataset))
    return results


async def execute_alert_scan(
    bq_client: Optional[Any] = None,
    project_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    webhook_url: Optional[str] = None,
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
    alerts = await asyncio.to_thread(collect_all_alerts, bq, target_project, target_dataset)

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
