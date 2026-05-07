#!/usr/bin/env python3
"""
Multiplicative ``version`` cardinality by APM topology connected component.

For each undirected connected component in the service map (same time window as tag
lookback), estimate = product of distinct ``version`` counts per service in that
component: ∏ |V(s)| (chain intuition: A×B×C).

Uses POST /v2/apm/topology and GetInferredServiceTagValues (same as
scripts/apm_version_tag_value_breakdown.py).

Env: SPLUNK_ACCESS_TOKEN, SPLUNK_REALM (default us0)
     APM_LOOKBACK_MS (default 7d) — topology uses the matching ISO window
     APM_TAG_NAME (default version)
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

_GQL_TV = """query GetInferredServiceTagValues(
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
    tags { value }
    __typename
  }
}
"""


def splunk_post(app_url: str, token: str, path: str, body: dict | None) -> dict:
    url = f"{app_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-SF-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def splunk_graphql(app_url: str, token: str, op: str, query: str, variables: dict) -> dict:
    return splunk_post(
        app_url,
        token,
        "/v2/apm/graphql?op=" + op,
        {"operationName": op, "query": query, "variables": variables},
    )


def parse_tag_values(data: object) -> list[str]:
    out: list[str] = []
    rows = data if isinstance(data, list) else []
    for row in rows:
        for t in row.get("tags") or []:
            v = t.get("value")
            if v is not None and str(v) not in out:
                out.append(str(v))
    return sorted(out, key=lambda x: (len(x), x))


def connected_components(
    services: list[str], edges: list[tuple[str, str]]
) -> list[list[str]]:
    parent: dict[str, str] = {s: s for s in services}

    def find(x: str) -> str:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for u, v in edges:
        if u in parent and v in parent:
            union(u, v)

    comps: dict[str, list[str]] = {}
    for s in services:
        r = find(s)
        comps.setdefault(r, []).append(s)
    return [sorted(c) for c in comps.values()]


def main() -> int:
    setup_script_logging(__name__, verbose=False)
    logger.info("APM version × topology cardinality (distinct values per component)")

    token = os.environ.get("SPLUNK_ACCESS_TOKEN")
    if not token:
        logger.error("SPLUNK_ACCESS_TOKEN is required.")
        return 1
    realm = os.environ.get("SPLUNK_REALM", "us0")
    app_url = f"https://app.{realm}.signalfx.com"
    tag_name = os.environ.get("APM_TAG_NAME", "version")
    lookback = int(os.environ.get("APM_LOOKBACK_MS", str(7 * 24 * 60 * 60 * 1000)))

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback
    start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ms / 1000))
    end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ms / 1000))
    time_range = f"{start_iso}/{end_iso}"

    topo = splunk_post(app_url, token, "/v2/apm/topology", {"timeRange": time_range})
    d = (topo.get("data") or {})
    raw_nodes = d.get("nodes") or []
    raw_edges = d.get("edges") or []
    services = [n["serviceName"] for n in raw_nodes if n.get("serviceName")]
    services = sorted(set(services))
    edges: list[tuple[str, str]] = []
    for e in raw_edges:
        a, b = e.get("fromNode"), e.get("toNode")
        if a and b and a != b:
            edges.append((a, b))

    # Per-service |V|
    card: dict[str, int] = {}
    for svc in services:
        resp = splunk_graphql(
            app_url,
            token,
            "GetInferredServiceTagValues",
            _GQL_TV,
            {
                "tag": tag_name,
                "limit": 200,
                "timeRange": {"lookbackMillis": lookback},
                "filters": {"tags": [{"tagName": "sf_service", "values": [svc]}]},
            },
        )
        if resp.get("errors"):
            logger.warning(
                "TagValues error for service %s: %s",
                svc,
                resp["errors"][0].get("message", "")[:200],
            )
            card[svc] = 0
            continue
        vals = parse_tag_values((resp.get("data") or {}).get("tagValues"))
        card[svc] = len(vals)

    comps = connected_components(services, edges)

    print(f"Realm: {realm} | Window: {time_range}", file=sys.stderr)
    print(f"Topology: {len(services)} services, {len(edges)} directed edges (union as undirected)", file=sys.stderr)
    print(file=sys.stderr)

    print(f"## `{tag_name}` — multiplicative cardinality by connected component\n")
    print("Model: **∏ |V(s)|** over each service in the same APM connected component ")
    print("(undirected graph from service map). Same lookback for topology and tag values.\n")

    print("| Component # | Services | |V| per service | **Strict ∏** | **∏ (|V|>0 only)** |")
    print("|---------------|----------|----------------|--------------|---------------------|")

    grand_parts: list[str] = []
    for i, comp in enumerate(sorted(comps, key=len, reverse=True), start=1):
        factors = []
        prod = 1
        prod_pos = 1
        n_zero = 0
        for s in comp:
            n = card.get(s, 0)
            factors.append(f"`{s}`={n}")
            prod *= n
            if n == 0:
                n_zero += 1
            else:
                prod_pos *= n
        sv = ", ".join(f"`{x}`" for x in comp)
        fac = "; ".join(factors)
        grand_parts.append(f"Component {i}: {prod}")
        pos_cell = str(prod_pos) if n_zero else "—"
        print(f"| {i} | {sv} | {fac} | **{prod}** | {pos_cell} |")

    print()
    print("### Notes")
    print("- **Strict product** = 0 if **any** service in the component has **no** `version` values in the window.")
    print("- **∏ (|V|>0 only)** (extra column when applicable): multiplies only services with at least one value — exploratory when some services have no tag data.")
    print("- This is a **theoretical upper bound** if every combination of values could appear on one trace; real TMS is often lower.")
    print("- Topology edges are **observed dependencies** in the window; they are undirected for connectivity only.")
    logger.info(
        "Done: %s service(s), %s component(s) (markdown on stdout)",
        len(services),
        len(comps),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
