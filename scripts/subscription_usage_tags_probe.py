#!/usr/bin/env python3
"""
Call APM subscription-usage GraphQL on app.{realm}.signalfx.com (same as the UI).

The UI uses POST /v2/apm/graphql?op=GetSubscriptionUsageServices|GetSubscriptionUsageTags|GetSubscriptionUsageTraces
with X-SF-Token — not REST /api/subscription-usage/tags.

Usage:
  SPLUNK_ACCESS_TOKEN=... SPLUNK_REALM=us1 python3 scripts/subscription_usage_tags_probe.py --op tags --ts 1775239200000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)

QUERIES: dict[str, tuple[str, str]] = {
    "services": (
        "GetSubscriptionUsageServices",
        "query GetSubscriptionUsageServices($timestampMillis: String!) {\n"
        "  getSubscriptionUsageServices(timestampMillis: $timestampMillis) {\n"
        "    serviceName\n    spanCount\n    percentage\n    __typename\n  }\n}\n",
    ),
    "tags": (
        "GetSubscriptionUsageTags",
        "query GetSubscriptionUsageTags($timestampMillis: String!) {\n"
        "  getSubscriptionUsageTags(timestampMillis: $timestampMillis) {\n"
        "    tagName\n    charCount\n    percentage\n    __typename\n  }\n}\n",
    ),
    "traces": (
        "GetSubscriptionUsageTraces",
        "query GetSubscriptionUsageTraces($timestampMillis: String!) {\n"
        "  getSubscriptionUsageTraces(timestampMillis: $timestampMillis) {\n"
        "    traceId\n    spanCount\n    __typename\n  }\n}\n",
    ),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument(
        "--op",
        choices=("services", "tags", "traces"),
        default="tags",
        help="Which GraphQL operation to run",
    )
    p.add_argument(
        "--ts",
        "--timestampMillis",
        dest="timestampMillis",
        required=True,
        help="Snapshot millis as string (copy from UI request payload)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Subscription usage GraphQL probe (--op=%s)", args.op)

    profile_path = resolve_profile_path(args.profile)
    profile: dict[str, str] = {}
    if profile_path and os.path.isfile(profile_path):
        profile = load_customer_profile_scalars(profile_path)

    token = (
        os.environ.get("SPLUNK_ACCESS_TOKEN")
        or profile.get("access_token")
        or profile.get("ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1

    realm = (
        args.realm
        or profile.get("realm")
        or os.environ.get("SPLUNK_REALM")
        or "us0"
    ).strip()

    logger.info("Realm: %s | POST /v2/apm/graphql op=%s", realm, args.op)

    op_name, query = QUERIES[args.op]
    base = f"https://app.{realm}.signalfx.com"
    path = f"/v2/apm/graphql?op={op_name}"
    url = base + path
    body = {
        "operationName": op_name,
        "variables": {"timestampMillis": str(args.timestampMillis)},
        "query": query,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-SF-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            logger.debug("Response bytes: %s", len(raw))
            print(json.dumps(json.loads(raw), indent=2))
            logger.info("GraphQL request succeeded")
            return 0
    except urllib.error.HTTPError as e:
        err = (e.read() or b"").decode("utf-8", errors="replace")
        logger.error("HTTP %s from GraphQL: %s", e.code, err[:2000])
        print(f"HTTP {e.code}", file=sys.stderr)
        print(err[:8000], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
