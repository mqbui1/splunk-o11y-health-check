#!/usr/bin/env python3
"""
Per-service breakdown of distinct values for a span tag (default: version).

Note on activeUserEnvironmentTagValues
--------------------------------------
That name does not appear as a field on the current APM GraphQL ``Query`` type
(``Cannot query field \"activeUserEnvironmentTagValues\" on type \"Query\"``).
The Splunk UI may use a different internal route or an older name.

This script uses the same operation as MCP ``apm_inferred_svc_tag_values``:
``GetInferredServiceTagValues`` → ``tagValues`` with ``groupbys: [{ tagName, limit }]``
and ``filters: { tags: [{ tagName: \"sf_service\", values: [<service>] }] }`` to
scope values to each service.

``GetTagValueAutocomplete`` (same as MCP ``apm_tag_value_autocomplete``) can return the
same value sets when:

- ``time`` is ``{ \"gte\": now - lookback, \"lte\": now }`` (aligns with ``lookbackMillis`` above)
- ``tagType`` is ``\"INDEXED\"`` (not ``\"service\"`` — the backend enum is
  ``INDEXED`` / ``ALL`` / ``UNINDEXED``)
- ``indexedTagFilters`` is ``[{ \"tagName\": \"sf_service\", \"values\": [<service>] }]``

Set ``APM_COMPARE_AUTOCOMPLETE=1`` to call both APIs per service and assert matching lists.

Env: SPLUNK_ACCESS_TOKEN (required), SPLUNK_REALM (default us0)
     APM_TAG_NAME (default version)
     APM_LOOKBACK_MS (default 604800000 = 7d)
     APM_COMPARE_AUTOCOMPLETE — set to 1 to verify against GetTagValueAutocomplete
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)

# Default services: reports/indexed-span-tags.md (version / service)
DEFAULT_SERVICES: list[str] = [
    "adservice",
    "ButtercupPayments",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "mysql:LxvGChW075",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
    "redis",
    "shippingservice",
]

_GQL = """query GetInferredServiceTagValues(
  $tag: String!,
  $limit: Int,
  $timeRange: TimeRangeInput,
  $filters: FilterInput
) {
  tagValues(
    groupbys: [{tagName: $tag, limit: $limit}]
    timeRange: $timeRange
    filters: $filters
  ) {
    tags {
      value
      __typename
    }
    __typename
  }
}
"""

_GQL_AUTOCOMPLETE = """query GetTagValueAutocomplete(
  $time: AutocompleteTimeInput,
  $tagName: String,
  $tagValueInput: String,
  $indexedTagFilters: [IndexedTagFiltersInput],
  $queryLimit: Int,
  $tagType: String
) {
  getTagValueAutocomplete(
    time: $time
    tagName: $tagName
    tagValueInput: $tagValueInput
    indexedTagFilters: $indexedTagFilters
    queryLimit: $queryLimit
    tagType: $tagType
  ) {
    values
    __typename
  }
}
"""


def splunk_graphql(app_url: str, token: str, op: str, query: str, variables: dict) -> dict:
    body = {"operationName": op, "query": query, "variables": variables}
    url = f"{app_url}/v2/apm/graphql?op={op}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"X-SF-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def parse_tag_values(data: dict | None) -> list[str]:
    """Flatten ValueBreakdown list to distinct string values."""
    if not data:
        return []
    out: list[str] = []
    rows = data if isinstance(data, list) else []
    for row in rows:
        for t in row.get("tags") or []:
            v = t.get("value")
            if v is not None and str(v) not in out:
                out.append(str(v))
    return sorted(out, key=lambda x: (len(x), x))


def fetch_autocomplete_values(
    app_url: str,
    token: str,
    *,
    tag_name: str,
    service: str,
    gte_ms: int,
    lte_ms: int,
    limit: int,
) -> list[str]:
    variables = {
        "time": {"gte": gte_ms, "lte": lte_ms},
        "tagName": tag_name,
        "tagValueInput": "",
        "indexedTagFilters": [{"tagName": "sf_service", "values": [service]}],
        "queryLimit": limit,
        "tagType": "INDEXED",
    }
    resp = splunk_graphql(
        app_url,
        token,
        "GetTagValueAutocomplete",
        _GQL_AUTOCOMPLETE,
        variables,
    )
    if resp.get("errors"):
        raise RuntimeError(resp["errors"][0].get("message", "GetTagValueAutocomplete failed"))
    raw = (resp.get("data") or {}).get("getTagValueAutocomplete") or {}
    vals = raw.get("values") or []
    return sorted({str(v) for v in vals}, key=lambda x: (len(x), x))


def main() -> int:
    setup_script_logging(__name__, verbose=False)
    logger.info("Per-service tag value breakdown (GetInferredServiceTagValues)")

    token = os.environ.get("SPLUNK_ACCESS_TOKEN")
    if not token:
        logger.error("SPLUNK_ACCESS_TOKEN is required.")
        return 1
    realm = os.environ.get("SPLUNK_REALM", "us0")
    app_url = f"https://app.{realm}.signalfx.com"
    tag_name = os.environ.get("APM_TAG_NAME", "version")
    lookback = int(os.environ.get("APM_LOOKBACK_MS", str(7 * 24 * 60 * 60 * 1000)))
    raw_svcs = os.environ.get("APM_SERVICES")
    if raw_svcs:
        services = [s.strip() for s in raw_svcs.split(",") if s.strip()]
    else:
        services = list(DEFAULT_SERVICES)

    compare_ac = os.environ.get("APM_COMPARE_AUTOCOMPLETE", "").strip() in (
        "1",
        "true",
        "yes",
    )
    now_ms = int(time.time() * 1000)
    gte_ms = now_ms - lookback
    lte_ms = now_ms

    print(
        f"# `{tag_name}` tag — distinct values per service (tagValues + sf_service filter)\n",
        file=sys.stderr,
    )
    print(f"Lookback: {lookback} ms | Realm: {realm}\n", file=sys.stderr)
    if compare_ac:
        print(
            "APM_COMPARE_AUTOCOMPLETE: also querying GetTagValueAutocomplete (tagType=INDEXED)\n",
            file=sys.stderr,
        )

    rows_out: list[tuple[str, list[str]]] = []
    mismatch = 0
    for svc in services:
        variables = {
            "tag": tag_name,
            "limit": 200,
            "timeRange": {"lookbackMillis": lookback},
            "filters": {"tags": [{"tagName": "sf_service", "values": [svc]}]},
        }
        resp = splunk_graphql(
            app_url,
            token,
            "GetInferredServiceTagValues",
            _GQL,
            variables,
        )
        if resp.get("errors"):
            err = resp["errors"][0].get("message", "")
            logger.error("GraphQL errors for service %s: %s", svc, err[:300])
            rows_out.append((svc, []))
            continue
        vals = parse_tag_values((resp.get("data") or {}).get("tagValues"))
        rows_out.append((svc, vals))
        line = f"  {svc}: {vals}"
        if compare_ac:
            try:
                ac_vals = fetch_autocomplete_values(
                    app_url,
                    token,
                    tag_name=tag_name,
                    service=svc,
                    gte_ms=gte_ms,
                    lte_ms=lte_ms,
                    limit=200,
                )
            except RuntimeError as e:
                print(f"{line}  [autocomplete ERROR: {e}]", file=sys.stderr)
                mismatch += 1
                continue
            if list(vals) != list(ac_vals):
                print(
                    f"{line}  [DIFF vs autocomplete: {ac_vals}]",
                    file=sys.stderr,
                )
                mismatch += 1
            else:
                print(f"{line}  [same as GetTagValueAutocomplete]", file=sys.stderr)
        else:
            print(line, file=sys.stderr)

    print()
    print(f"| Service | Distinct `{tag_name}` values | Count |")
    print("|---------|-------------------------------|-------|")
    for svc, vals in rows_out:
        cell = ", ".join(f"`{v}`" for v in vals) if vals else "—"
        print(f"| `{svc}` | {cell} | {len(vals)} |")

    if compare_ac and mismatch:
        logger.warning("APM_COMPARE_AUTOCOMPLETE: %s service(s) differ or failed", mismatch)
        print(
            f"\nAPM_COMPARE_AUTOCOMPLETE: {mismatch} service(s) differ or failed.",
            file=sys.stderr,
        )
        return 2
    logger.info("Done: %s service(s) analyzed", len(services))
    return 0


if __name__ == "__main__":
    sys.exit(main())
