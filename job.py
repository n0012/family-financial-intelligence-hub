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
    from monarch_service import execute_sync

    return await execute_sync(days_back=days_back)


async def run_alerts() -> dict:
    from alerts import execute_alert_scan

    return await execute_alert_scan()


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

    args = parser.parse_args()

    try:
        if args.task == "sync":
            days_back = args.days_back if args.days_back > 0 else None
            result = asyncio.run(run_sync(days_back))
        else:
            result = asyncio.run(run_alerts())
    except Exception as e:
        logger.exception(f"Task '{args.task}' failed: {e}")
        return 1

    logger.info(f"Task '{args.task}' succeeded: {json.dumps(result, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
