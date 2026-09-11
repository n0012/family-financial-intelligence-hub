#!/usr/bin/env python
"""
Batch entrypoint for Cloud Run Jobs.

Runs the scheduled/manual workloads (Monarch -> BigQuery sync, spend alert scan)
without starting an HTTP server, so the workload has no network ingress at all.
"""

import argparse
import asyncio
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("monarch-gemini.job")


async def run_sync(days_back: int | None) -> dict:
    from app.monarch_service import execute_sync

    return await execute_sync(days_back=days_back)


async def run_alerts() -> dict:
    from app.alerts import execute_alert_scan

    return await execute_alert_scan()


async def run_digest(period: str = "weekly") -> dict:
    import requests

    from app.alerts import (
        BQ_DATASET_ID,
        BQ_PROJECT_ID,
        build_executive_digest_card,
        build_executive_digest_markdown,
        generate_executive_digest,
        resolve_secret,
    )
    from app.bq_service import get_bq_client

    target_project = BQ_PROJECT_ID
    target_dataset = BQ_DATASET_ID
    bq = get_bq_client(target_project)
    p = "MONTHLY" if str(period).lower().startswith("m") else "WEEKLY"
    digest = await asyncio.to_thread(generate_executive_digest, bq, target_project, target_dataset, p)

    webhook_url = resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if webhook_url:
        try:
            if "chat.googleapis.com" in webhook_url:
                payload = build_executive_digest_card(digest)
            else:
                md = build_executive_digest_markdown(digest)
                payload = {"content": md, "text": md}
            resp = await asyncio.to_thread(requests.post, webhook_url, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Digest webhook dispatch failed: {e}")

    return {
        "status": "success",
        "digest": digest,
        "webhook_dispatched": webhook_sent,
    }


async def run_sweep() -> dict:
    import requests

    from app.alerts import (
        BQ_DATASET_ID,
        BQ_PROJECT_ID,
        build_chat_card_v2,
        build_markdown_fallback,
        check_paycheck_surplus_sweep,
        resolve_secret,
    )
    from app.bq_service import get_bq_client

    target_project = BQ_PROJECT_ID
    target_dataset = BQ_DATASET_ID
    bq = get_bq_client(target_project)

    sweep_alerts = await asyncio.to_thread(check_paycheck_surplus_sweep, bq, target_project, target_dataset)

    webhook_url = resolve_secret("alert-webhook-url", "ALERT_WEBHOOK_URL")
    webhook_sent = False
    if sweep_alerts and webhook_url:
        try:
            if "chat.googleapis.com" in webhook_url:
                payload = build_chat_card_v2(sweep_alerts)
            else:
                md = build_markdown_fallback(sweep_alerts)
                payload = {"content": md, "text": md}
            resp = await asyncio.to_thread(requests.post, webhook_url, json=payload, timeout=10)
            webhook_sent = resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Sweep webhook dispatch failed: {e}")

    return {
        "status": "success",
        "has_sweep_opportunity": len(sweep_alerts) > 0,
        "sweep_alerts": sweep_alerts,
        "webhook_dispatched": webhook_sent,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="job")
    sub = parser.add_subparsers(dest="task", required=True)

    sync_cmd = sub.add_parser("sync", help="Sync Monarch Money data into BigQuery")
    sync_cmd.add_argument(
        "--days-back",
        type=int,
        default=30,
        help="How many days of transactions to sync; 0 means all history",
    )

    sub.add_parser("alerts", help="Scan BigQuery views and dispatch spend alerts")

    digest_cmd = sub.add_parser("digest", help="Generate and dispatch weekly or monthly executive CFO digest")
    digest_cmd.add_argument(
        "--period",
        choices=["weekly", "monthly"],
        default="weekly",
        help="Period for executive digest (weekly or monthly)",
    )

    sub.add_parser("sweep", help="Scan for paycheck surplus sweep opportunities")

    args = parser.parse_args()

    try:
        if args.task == "sync":
            days_back = args.days_back if args.days_back > 0 else None
            result = asyncio.run(run_sync(days_back))
        elif args.task == "alerts":
            result = asyncio.run(run_alerts())
        elif args.task == "digest":
            result = asyncio.run(run_digest(args.period))
        elif args.task == "sweep":
            result = asyncio.run(run_sweep())
        else:
            result = {"error": f"Unknown task {args.task}"}
    except Exception as e:
        logger.exception(f"Task '{args.task}' failed: {e}")
        return 1

    logger.info(f"Task '{args.task}' succeeded: {json.dumps(result, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
