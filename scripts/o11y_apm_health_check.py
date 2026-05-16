#!/usr/bin/env python3
"""
Splunk Observability Cloud — APM health check (standalone script).

Implements the APM section of Splunk-Observability-Health-Check.md: health endpoints, usage by service,
orphans, optional trace-based checks (minimal spans, large payloads, PII/debug/duplicates), tag payload
share, and endpoint grouping. CLI flags tune windows and sampling; see --help.

**Auth:** `SPLUNK_ACCESS_TOKEN` or profile YAML (see `o11y_license_utilization`).

**Run examples:**
  python3 scripts/o11y_apm_health_check.py --md-out reports/apm-health-snapshot.md
  python3 scripts/o11y_apm_health_check.py --trace-checks --md-out reports/apm.md
    # runs minimal_spans + large_span_sizes (trace-heavy)
  python3 scripts/o11y_apm_health_check.py --checks large_span_sizes --md-out reports/span-size.md
  python3 scripts/o11y_apm_health_check.py --checks tags_high_cardinality --subscription-usage-timestamp-ms MS

See `.cursor/skills/o11y-apm-health-checks/SKILL.md`. Checks that need judgment or missing APIs may be
deferred; endpoint grouping and trace heuristics use `spans.count` + bounded trace samples.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

# Profile helpers (shared with license script)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import (  # noqa: E402
    load_customer_profile_scalars,
    resolve_profile_path,
)
from o11y_report_format import fmt_amount_commas, fmt_table_number  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (align with o11y-apm-health-checks SKILL.md)
# ---------------------------------------------------------------------------

APM_SIGNALFLOW_RESOLUTION_MS = 3_600_000  # 1 hour — required per skill

# sf_operation substring signals for **Health Endpoints Enabled** (+ minimal-span / rollup exclusions).
# Many values are URLs or route templates seen in OTel/Java/.NET/AWS/GCP meshes; extend via profile
# `health_check_apm_extra_health_operation_substrings` or `--extra-health-operation-substrings`.
# Matching stays case-insensitive in `is_health_operation` (and optional HTTP-verb strip — see below).
# Avoid bare tokens (`status`, `/up`) due to FP risk.
_DEFAULT_HEALTH_OPERATION_SUBSTRINGS: tuple[str, ...] = (
    # --- HTTP-ish routes ---
    "/__health",
    "/__health__",
    "/_ah/health",  # App Engine legacy / GCP-style wrappers
    "/_health/",
    "/_health",
    "/_healthz/",
    "/_healthz",
    "/alive",
    "/api/heartbeat",
    "/api/health/",
    "/api/health",
    "/api/healthz",
    "/api/v1/health/",
    "/api/v1/health",
    "/api/v2/health/",
    "/api/v2/health",
    "/check_health",
    "/check-health/",
    "/check-health",
    "/check/health/",
    "/check/health",
    "/diagnostic/health",
    "/diagnostics/health/",
    "/diagnostics/health",
    "/elb-health/",
    "/elb-health-check/",
    "/elb-health-check",
    "/elb-health",
    "/heartbeat",
    "/heart-beat",
    "/ingress/health",
    "/ingress-health",
    "/internal/heartbeat",
    "/internal-health",
    "/internal/health/",
    "/internal/health",
    "/is-alive",
    "/isalive/",
    "/isalive",
    "/is-ready",
    "/isready/",
    "/is_ready",
    "/kube-probe/",
    "/kube/readiness/",
    "/kube/readiness",
    "/liveness/",
    "/liveness-probe",
    "/mgmt/health",
    "/mgmt/heartbeat",
    "/monitoring/health/",
    "/monitoring/health",
    "/nginx-health",
    "/node-health/",
    "/node-health",
    "/ping-health",
    "/ping/",
    "/ping",
    "/probe/",
    "/probes/",
    "/q/health/",  # Quarkus (+ subpaths beyond generic /health segment)
    "/q/health",
    "/readiness/",
    "/readiness-probe",
    "/relay/health/",
    "/ready",
    "/self/health/",
    "/self/health",
    "/service/health/",
    "/service/health",
    "/status/healthy",
    "/status/health",
    "/svc/health/",
    "/svc/health",
    "/system/health",
    "/-/healthy",  # GitLab-ish / tooling health segments
    "/-/health",
    "/targetgroup-health/",
    "/targetgroup-health",
    "/version/health/",
    "/version/health",
    "/wasm_plugin_health_check",  # Envoy extension probe route (Envoy repos)
    "/well-known/live",
    # Spring / Micrometer / actuator (avoid bare "/actuator/" — matches non-health actuator routes)
    "/actuator/health/",
    "/actuator/health",
    "/actuator/ping",
    "/actuator/readiness/",
    "/actuator/readiness",
    "/actuator/liveness/",
    "/actuator/liveness",
    "/health/full",
    "/health/live",
    "/health/liveness",
    "/health/ready",
    "/health/readiness",
    "/health/startup/",
    "/health/startup",
    "/health/livez",
    "/health/readyz",
    "/healthcheck/",
    "/healthcheck",
    "/healthchecks/",
    "/healthchecks",
    "/health_checks/",
    "/health_checks",
    "/health-check/",
    "/health-check",
    "/health/",
    "/health",
    "/healthz/",
    "/healthz",
    "/livez/",
    "/livez",
    "/live/",
    "/live",
    "/readyz/",
    "/readyz",
    "/readiness/",
    "/readiness",
    "/startup/",
    "/startup",
    "/server/healthz",
    "/server-health",
    # --- gRPC / Connect / generic health service names on spans ---
    ".grpc.health",
    "/grpc.health",
    "grpc.health",
    # Full method names (Splunk / OTel often mirror protobuf RPC names)
    "/grpc.health.v1.health.check",
    "grpc.health.v1.health.check",
    "/grpc.health.v1.health.watch",
    "grpc.health.v1.health.watch",
    "/grpc.health.v1.Health/Check",
    "grpc.health.v1.Health/Check",
    "/grpc.health.v1.Health/Watch",
    "grpc.health.v1.Health/Watch",
    "/grpc.health.v1.Health/watch",
    # Double "grpc." prefix (Java agent / mesh — seen in MCP sandbox rollups)
    "/grpc.grpc.health.v1.Health/Check",
    "grpc.grpc.health.v1.Health/Check",
    "/grpc.grpc.health.v1.Health/Watch",
    "grpc.grpc.health.v1.Health/Watch",
    "/grpc.grpc.health.v1.health.check",
    "grpc.grpc.health.v1.health.check",
    "/grpc.grpc.health.v1.health.watch",
    "grpc.grpc.health.v1.health.watch",
    "grpc.grpc.health",
    "grpc.grpc.health.v1.health",
    # Health service type name variants
    "/grpc.health.v1.HealthService/Check",
    "grpc.health.v1.HealthService/Check",
    "/grpc.health.v1.HealthService/Watch",
    "grpc.health.v1.HealthService/Watch",
    "v1.health/check",
    "v1.health/watch",
    ".health/check",
    ".health/watch",
    "healthservice/check",
    "healthservice/watch",
    "health_service/check",
    "health_service/watch",
    "connectrpc.health",
    "connectrpc.health.v1",
    # Buf / Connect path-style
    "/grpc.health.v1.HealthService/",
    "/connect/health.v1.Health/",
)

# Public alias (tests / imports may reference this name)
HEALTH_OPERATION_SUBSTRINGS: tuple[str, ...] = _DEFAULT_HEALTH_OPERATION_SUBSTRINGS

_health_operation_patterns_active: tuple[str, ...] = _DEFAULT_HEALTH_OPERATION_SUBSTRINGS

# OTel HTTP server spans often use ``{method} {route}`` as the span / sf_operation name.
_HTTP_METHOD_PREFIX_FOR_HEALTH_RE = re.compile(
    r"^(?:GET|HEAD|POST|PUT|DELETE|PATCH|OPTIONS|CONNECT|TRACE)\s+",
    re.I | re.ASCII,
)


def _health_operation_match_strings(operation: str) -> list[str]:
    """Return strings to test against substring patterns (raw op and HTTP-verb-stripped form)."""
    s = operation.strip()
    out = [s]
    stripped = _HTTP_METHOD_PREFIX_FOR_HEALTH_RE.sub("", s, count=1).strip()
    if stripped and stripped != s:
        out.append(stripped)
    return out


def _parse_extra_health_substrings(*specs: str) -> list[str]:
    """Split comma/semicolon-separated user patterns; strip; drop empties."""
    out: list[str] = []
    for spec in specs:
        if not spec or not str(spec).strip():
            continue
        for part in re.split(r"[,;]", str(spec)):
            p = part.strip()
            if p:
                out.append(p)
    return out


def merge_health_operation_patterns(
    base: tuple[str, ...],
    *extra_csv_specs: str,
) -> tuple[str, ...]:
    """Union `base` with extra strings; dedupe by casefold; preserve first-seen casing."""
    seen: set[str] = set()
    merged: list[str] = []
    for x in base:
        cf = x.casefold()
        if cf not in seen:
            seen.add(cf)
            merged.append(x)
    for spec in extra_csv_specs:
        for x in _parse_extra_health_substrings(spec):
            cf = x.casefold()
            if cf not in seen:
                seen.add(cf)
                merged.append(x)
    return tuple(merged)


def set_active_health_operation_patterns(patterns: tuple[str, ...]) -> None:
    """Point matching at `patterns` (full tuple, usually from ``merge_health_operation_patterns``)."""
    global _health_operation_patterns_active
    _health_operation_patterns_active = patterns


def reset_active_health_operation_patterns() -> None:
    global _health_operation_patterns_active
    _health_operation_patterns_active = _DEFAULT_HEALTH_OPERATION_SUBSTRINGS


CHECK_KEYS: tuple[str, ...] = (
    "health_endpoints",
    "usage_by_service",
    "orphan_services",
    "minimal_spans",
    "large_span_sizes",
    "tags_high_cardinality",
    "sensitive_data",
    "debug_spans",
    "endpoint_grouping",
)

CHECK_TITLES: dict[str, str] = {
    "health_endpoints": "Health Endpoints Enabled",
    "usage_by_service": "Identify Usage by Service and Environment",
    "orphan_services": "Check for Orphan Services",
    "minimal_spans": "Traces with Minimal Spans",
    "large_span_sizes": "Review Span Size (tag / attribute payload)",
    "tags_high_cardinality": "Tags with High Cardinality",
    "sensitive_data": "Sensitive Data in Spans",
    "debug_spans": "Audit Debug/Verbose Spans in Production",
    "endpoint_grouping": "Review Endpoint Grouping Rules",
}

# Checklist-aligned descriptions (see Splunk-Observability-Health-Check.md).
CHECK_DESCRIPTIONS: dict[str, str] = {
    "health_endpoints": (
        "List of services that are sending traces for health-like endpoints (paths and operations that match "
        "common health-check patterns)."
    ),
    "usage_by_service": (
        "Analyze APM data across all services to provide an overview of each service's overall usage. The "
        "usage is analyzed across a small window of time so this should be used as an estimate and does not "
        "reflect usage across a billing period.\n\n"
        "- **Total Traces:** sum of trace counts for that service×environment (assessment window).\n"
        "- **% of Total Usage:** that row's trace total as a percentage of the sum of trace counts across all "
        "listed service×environment pairs."
    ),
    "orphan_services": (
        "Services that have no upstream or downstream dependencies. These may be services that still report "
        "traces but were supposed to be decommissioned, deprecated, or migrated, yet are still running and "
        "reporting data.\n\n"
        "List services:"
    ),
    "minimal_spans": (
        "For this check, health endpoints will be ignored as they are covered by the health endpoint check. "
        "For each service we will analyze if there are many traces with a small number of spans; this may "
        "provide little information and may be noisy traces.\n\n"
        "For each service and endpoint, if at least 20% of the traces analyzed meet one of the span-count bands "
        "below, the row is flagged in **Results**."
    ),
    "large_span_sizes": (
        "Spans with **large attribute payloads** drive trace-volume cost and noise. Assess using an "
        "**estimated span size** (e.g. UTF-8 byte length of span names plus all tag keys and values from "
        "retrieved traces), pooled across a **stratified sample** (top services by span volume, bounded trace "
        "fetches). Flag spans **above mean + 2 standard deviations** in that sample (or equivalent percentile "
        "if sample is small)."
    ),
    "tags_high_cardinality": (
        "Indexed span tags with a large number of unique values (high cardinality) may lead to an explosion on "
        "the usage of TMS and MMS. This health check does not provide analysis on your TMS and MMS, instead it "
        "focuses on analyzing the indexed tags and the number of unique values."
    ),
    "sensitive_data": (
        "List of services and spans with sensitive data (PII, HIPAA, credit card numbers, etc.) not obfuscated."
    ),
    "debug_spans": (
        "Ensure that trace-level logging or debug-level spans haven't been accidentally left enabled.\n\n"
        "List **service × environment × operation** combinations where debug-like spans appear — **one row per "
        "triple** (duplicates from multiple spans are omitted)."
    ),
    "endpoint_grouping": (
        "Poorly grouped endpoints (e.g. `/user/123`, `/user/456` treated as separate endpoints) inflate "
        "cardinality. Use endpoint grouping rules to collapse them (e.g. `/user/{id}`).\n\n"
        "List of services and endpoints affected."
    ),
}

# Share stratified `search_traces` sampling across span-size and tag-inspection heuristics.
TRACE_STRATIFIED_CHECKS: frozenset[str] = frozenset(
    {"large_span_sizes", "sensitive_data", "debug_spans"}
)

# All checks that share one get_trace_full pass (minimal spans + stratified span size + inspection).
TRACE_FULL_UNIFIED_CHECKS: frozenset[str] = frozenset(
    {"minimal_spans", "large_span_sizes", "sensitive_data", "debug_spans"}
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _qs(params: dict) -> str:
    filtered = {k: str(v) for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(filtered)) if filtered else ""


def splunk_post_json(
    base_url: str,
    path: str,
    body: dict[str, Any] | None,
    token: str,
    *,
    timeout: float = 30.0,
) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-SF-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"")[:4096].decode("utf-8", errors="replace")
        raise RuntimeError(f"Splunk API HTTP {e.code}: {detail}") from e


def iso_range_utc(start_ms: int, stop_ms: int) -> str:
    a = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ms / 1000))
    b = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stop_ms / 1000))
    return f"{a}/{b}"


def default_subscription_usage_timestamp_ms(stop_ms: int) -> int:
    """Hour-aligned bucket start (ms), common for Subscription usage snapshots."""
    return (stop_ms // APM_SIGNALFLOW_RESOLUTION_MS) * APM_SIGNALFLOW_RESOLUTION_MS


def apm_graphql_get_subscription_usage_tags(
    app_base: str, token: str, timestamp_millis: str
) -> dict[str, Any]:
    """POST GetSubscriptionUsageTags — same contract as MCP sub_usage_tags."""
    body = {
        "operationName": "GetSubscriptionUsageTags",
        "variables": {"timestampMillis": str(timestamp_millis)},
        "query": (
            "query GetSubscriptionUsageTags($timestampMillis: String!) {\n"
            "  getSubscriptionUsageTags(timestampMillis: $timestampMillis) {\n"
            "    tagName\n"
            "    charCount\n"
            "    percentage\n"
            "    __typename\n"
            "  }\n"
            "}\n"
        ),
    }
    return splunk_post_json(
        app_base,
        "/v2/apm/graphql?op=GetSubscriptionUsageTags",
        body,
        token,
        timeout=30.0,
    )


# APM GraphQL getTags — same contract as MCP ``apm_get_tag_definitions`` (indexed tag catalog).
_GQL_TAG_DEF_FRAGMENT_BLOCK = """fragment TagsFragment on TagDefinitionTagInfo {
  tagName
  originalTagName
  status
  type
  serviceName
  addedEpochMillis
  lastAnalyzedAtMillis
  dimensions
  __typename
}

fragment AnalysisJobInfoAdaptedFragmentWithMMS on AnalysisJobInfoAdapted {
  status
  lastRanAtMillis
  resultsReadyForMillis
  cardinalityEstimate
  cardinalityEstimateMMS
  cardinalityEntitlement
  cardinalityEntitlementMMS
  analyzedTagSet {
    dimensionalizations {
      id
      tagList
      operations
      services
      environment
      disabled
      __typename
    }
    __typename
  }
  tags {
    ...TagsFragment
  }
  __typename
}

fragment InferredServiceFragment on InferredServiceInfo {
  status
  serviceName
  type
  addedEpochMillis
  lastAnalyzedAtMillis
  dimensions
  __typename
}

fragment TagDefinitionAdaptedResponseFragmentWithErrors on TagDefinitionAdaptedResponse {
  tags {
    ...TagsFragment
    __typename
  }
  analysisJob {
    ...AnalysisJobInfoAdaptedFragmentWithMMS
    __typename
  }
  inferredServices {
    ...InferredServiceFragment
    __typename
  }
  isAnalyzing
  dimensionalizations {
    id
    tagList
    operations
    services
    environment
    disabled
    __typename
  }
  databaseTags {
    commonSqlTags {
      ...TagsFragment
      __typename
    }
    redisTags {
      ...TagsFragment
      __typename
    }
    __typename
  }
  errorTags {
    ...TagsFragment
    __typename
  }
  __typename
}
"""

_GQL_GET_TAGS_QUERY = _GQL_TAG_DEF_FRAGMENT_BLOCK + """
query getTags {
  getTags {
    ...TagDefinitionAdaptedResponseFragmentWithErrors
    __typename
  }
}
"""


def apm_graphql_get_tags(app_base: str, token: str) -> dict[str, Any]:
    """POST APM getTags — indexed tag definitions (same as UI / MCP ``apm_get_tag_definitions``)."""
    body = {
        "operationName": "getTags",
        "query": _GQL_GET_TAGS_QUERY,
    }
    return splunk_post_json(
        app_base,
        "/v2/apm/graphql?op=getTags",
        body,
        token,
        timeout=30.0,
    )


def collect_indexed_tag_names_from_get_tags(payload: dict[str, Any]) -> tuple[set[str], list[str]]:
    """
    Flatten tag names from ``getTags`` (main list, analysis job, DB/error tags, dimensionalizations).
    Returns (name set, warning messages).
    """
    warnings: list[str] = []
    if payload.get("errors"):
        errs = payload.get("errors")
        detail = errs[0].get("message", str(errs)) if isinstance(errs, list) and errs else str(errs)
        warnings.append(f"getTags GraphQL errors: {detail[:200]}")
    data = payload.get("data") if isinstance(payload, dict) else None
    gt = (data or {}).get("getTags") if isinstance(data, dict) else None
    if not isinstance(gt, dict):
        return set(), warnings + (["getTags data missing in response."] if not warnings else [])

    names: set[str] = set()

    def add_name(s: object) -> None:
        if isinstance(s, str) and s.strip():
            names.add(s.strip())

    def walk_tag_dicts(tag_list: object) -> None:
        if not isinstance(tag_list, list):
            return
        for t in tag_list:
            if not isinstance(t, dict):
                continue
            add_name(t.get("tagName"))
            on = t.get("originalTagName")
            add_name(on)

    walk_tag_dicts(gt.get("tags"))
    job = gt.get("analysisJob")
    if isinstance(job, dict):
        walk_tag_dicts(job.get("tags"))
    db = gt.get("databaseTags")
    if isinstance(db, dict):
        walk_tag_dicts(db.get("commonSqlTags"))
        walk_tag_dicts(db.get("redisTags"))
    walk_tag_dicts(gt.get("errorTags"))

    def walk_dim_tag_lists(block: dict[str, Any]) -> None:
        for d in block.get("dimensionalizations") or []:
            if not isinstance(d, dict):
                continue
            tl = d.get("tagList")
            if isinstance(tl, list):
                for x in tl:
                    add_name(x)

    walk_dim_tag_lists(gt)
    if isinstance(job, dict):
        walk_dim_tag_lists(job)

    return names, warnings


def subscription_tag_matches_indexed_catalog(tag_name: str, indexed: set[str]) -> bool:
    """Case-sensitive match first, then case-insensitive for safety."""
    if tag_name in indexed:
        return True
    t_lower = tag_name.lower()
    return any(x.lower() == t_lower for x in indexed)


# Same operation as MCP ``get_endpoint_breakdown`` / UI Endpoints breakdown.
ENDPOINTS_BREAKDOWN_OVER_TIME_QUERY = """query EndpointsBreakdownOverTimeNode($timeRange: TimeRangeInput, $filters: NodeFilterInput!, $groupbys: [GroupByTagInput!], $includeRequestCountTimeSeries: Boolean!, $includeRequestDurationMicrosP50TimeSeries: Boolean!, $includeRequestDurationMicrosP90TimeSeries: Boolean!, $includeRequestDurationMicrosP99TimeSeries: Boolean!, $includeErrorCountTimeSeries: Boolean!) {
  nodes(timeRange: $timeRange, filters: $filters, groupbys: $groupbys) {
    node {
      nodeTags {
        tagName
        value
        __typename
      }
      __typename
    }
    requestCount {
      value
      valueByTime @include(if: $includeRequestCountTimeSeries)
      resolutionMillis @include(if: $includeRequestCountTimeSeries)
      __typename
    }
    requestDurationMicrosP50 {
      value
      valueByTime @include(if: $includeRequestDurationMicrosP50TimeSeries)
      resolutionMillis @include(if: $includeRequestDurationMicrosP50TimeSeries)
      __typename
    }
    requestDurationMicrosP90 {
      value
      valueByTime @include(if: $includeRequestDurationMicrosP90TimeSeries)
      resolutionMillis @include(if: $includeRequestDurationMicrosP90TimeSeries)
      __typename
    }
    requestDurationMicrosP99 {
      value
      valueByTime @include(if: $includeRequestDurationMicrosP99TimeSeries)
      resolutionMillis @include(if: $includeRequestDurationMicrosP99TimeSeries)
      __typename
    }
    errorCount {
      value
      valueByTime @include(if: $includeErrorCountTimeSeries)
      resolutionMillis @include(if: $includeErrorCountTimeSeries)
      __typename
    }
    __typename
  }
}
"""


def apm_graphql_endpoints_breakdown_over_time(
    app_base: str,
    token: str,
    *,
    lookback_millis: int,
    resolution_millis: int,
    filter_tags: list[dict[str, Any]],
    groupbys: list[dict[str, Any]] | None = None,
    include_request_count_time_series: bool = False,
) -> dict[str, Any]:
    """
    POST ``/v2/apm/graphql?op=EndpointsBreakdownOverTimeNode`` — endpoint request counts
    and latencies (same family as APM UI endpoint breakdown).
    """
    if groupbys is None:
        groupbys = [
            {
                "tagName": "sf_endpoint",
                "orderby": "requestDurationMicrosP90",
                "limit": 500,
            }
        ]
    variables: dict[str, Any] = {
        "timeRange": {
            "lookbackMillis": int(lookback_millis),
            "resolutionMillis": int(resolution_millis),
        },
        "filters": {"tags": filter_tags},
        "groupbys": groupbys,
        "includeRequestCountTimeSeries": include_request_count_time_series,
        "includeRequestDurationMicrosP50TimeSeries": False,
        "includeRequestDurationMicrosP90TimeSeries": False,
        "includeRequestDurationMicrosP99TimeSeries": False,
        "includeErrorCountTimeSeries": False,
    }
    body = {
        "operationName": "EndpointsBreakdownOverTimeNode",
        "variables": variables,
        "query": ENDPOINTS_BREAKDOWN_OVER_TIME_QUERY,
    }
    return splunk_post_json(
        app_base,
        "/v2/apm/graphql?op=EndpointsBreakdownOverTimeNode",
        body,
        token,
        timeout=30.0,
    )


def parse_endpoints_breakdown_nodes(payload: dict[str, Any]) -> dict[str, float]:
    """Map ``sf_endpoint`` tag value -> ``requestCount.value`` from EndpointsBreakdownOverTimeNode."""
    out: dict[str, float] = {}
    nodes = (payload.get("data") or {}).get("nodes") or []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        node = n.get("node") or {}
        tags = node.get("nodeTags") or []
        ep_val: str | None = None
        for t in tags:
            if not isinstance(t, dict):
                continue
            if t.get("tagName") == "sf_endpoint":
                ep_val = str(t.get("value") or "")
                break
        if not ep_val:
            continue
        rc = n.get("requestCount") or {}
        val = rc.get("value")
        if val is None:
            continue
        try:
            out[ep_val] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _match_endpoint_request_count(endpoint_to_requests: dict[str, float], sf_operation: str) -> float | None:
    """Match MMS ``sf_operation`` to UI ``sf_endpoint`` key (exact, then case-insensitive)."""
    if sf_operation in endpoint_to_requests:
        return endpoint_to_requests[sf_operation]
    op_l = sf_operation.strip().lower()
    for k, v in endpoint_to_requests.items():
        if k.strip().lower() == op_l:
            return v
    return None


def enrich_health_endpoints_with_request_counts(
    rows: list[dict[str, Any]],
    app_base: str,
    token: str,
    *,
    lookback_millis: int,
    resolution_millis: int = 60_000,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    One GraphQL call per distinct (service, environment); fill ``requestCountWindow`` on each row.
    Calls are issued in parallel (up to 8 workers).
    """
    extra_findings: list[str] = []
    if not rows:
        return rows, extra_findings

    pairs: list[tuple[str, str]] = sorted(
        {(str(r["service"]), str(r["environment"])) for r in rows}
    )
    cache: dict[tuple[str, str], dict[str, float]] = {}

    def _fetch_pair(svc: str, env: str) -> tuple[tuple[str, str], dict[str, float], str | None]:
        filter_tags: list[dict[str, Any]] = [{"tagName": "sf_service", "values": [svc]}]
        if env is not None and str(env).strip() != "":
            filter_tags.append({"tagName": "sf_environment", "values": [str(env)]})
        try:
            raw = apm_graphql_endpoints_breakdown_over_time(
                app_base,
                token,
                lookback_millis=max(60_000, int(lookback_millis)),
                resolution_millis=resolution_millis,
                filter_tags=filter_tags,
            )
            if raw.get("errors"):
                return (svc, env), {}, f"Request counts could not be loaded for {svc}/{env} (endpoint breakdown unavailable)."
            return (svc, env), parse_endpoints_breakdown_nodes(raw), None
        except Exception as e:
            return (svc, env), {}, f"Request counts could not be loaded for {svc}/{env} (endpoint breakdown request failed)."

    workers = max(1, min(8, len(pairs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_fetch_pair, svc, env) for svc, env in pairs]
        for fut in concurrent.futures.as_completed(futs):
            key, ep_map, err = fut.result()
            cache[key] = ep_map
            if err:
                extra_findings.append(err)

    for r in rows:
        svc, env, op = str(r["service"]), str(r["environment"]), str(r.get("endpointName") or "")
        ep_map = cache.get((svc, env), {})
        cnt = _match_endpoint_request_count(ep_map, op) if op else None
        r["requestCountWindow"] = round(cnt, 4) if cnt is not None else None

    return rows, extra_findings


# ---------------------------------------------------------------------------
# SignalFlow SSE — collect metadata + datapoints (for matrix rollups)
# ---------------------------------------------------------------------------


def _parse_sse_block(lines: list[str]) -> tuple[str, dict]:
    event_name = "message"
    data_parts: list[str] = []
    for raw in lines:
        if raw.startswith(":"):
            continue
        if raw.startswith("event:"):
            event_name = raw[6:].strip() or "message"
        elif raw.startswith("data:"):
            data_parts.append(raw[5:].lstrip())
    payload = "\n".join(data_parts).strip()
    if not payload:
        return event_name, {}
    try:
        return event_name, json.loads(payload)
    except json.JSONDecodeError:
        return event_name, {}


def _payload_kind(sse_event: str, msg: dict) -> str | None:
    if not msg:
        return None
    t = msg.get("type")
    if isinstance(t, str) and t.strip():
        sl = "".join(c for c in t.lower() if c not in "_-")
        aliases = {
            "datamessage": "data",
            "data": "data",
            "controlmessage": "control-message",
            "errormessage": "error",
            "error": "error",
        }
        return aliases.get(sl, t.lower())
    if sse_event and sse_event != "message":
        return sse_event.lower()
    return None


def _stop_from_event(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    u = val.strip().upper().replace("-", "_")
    if u == "END_OF_CHANNEL":
        return "end"
    return None


def signalflow_matrix_collect(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 90.0,
    read_timeout: float = 90.0,
    max_body_bytes: int = 12 * 1024 * 1024,
    max_data_points: int = 50_000,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    """
    Stream SignalFlow execute; return (metadata_by_tsid, data_points, error, stop_reason).
    Each data point: tsId, value, timestampMs.
    """
    query_params = {
        "start": start_ms,
        "stop": stop_ms,
        "resolution": resolution_ms,
        "immediate": "true",
    }
    url = f"{stream_url}/v2/signalflow/execute" + _qs(query_params)
    req = urllib.request.Request(
        url,
        data=program.encode("utf-8"),
        headers={"X-SF-Token": token, "Content-Type": "text/plain"},
        method="POST",
    )
    metadata: dict[str, dict[str, Any]] = {}
    data_points: list[dict[str, Any]] = []
    err: str | None = None
    stop_reason: str | None = None
    t0 = time.monotonic()
    raw_bytes = 0

    def handle_msg(ev: str, msg: dict) -> bool:
        nonlocal err, stop_reason
        kind = _payload_kind(ev, msg)
        if kind == "error":
            err = json.dumps(msg)[:800]
            return True
        if kind and kind != "data":
            sr = _stop_from_event(msg.get("event"))
            if sr == "end":
                stop_reason = "end_of_channel"
                return True
        if kind == "metadata":
            tsid = msg.get("tsId")
            if tsid is not None:
                metadata[str(tsid)] = msg.get("properties") or {}
            return False
        if kind == "data":
            ts_ms = msg.get("logicalTimestampMs")
            for pt in msg.get("data") or []:
                if len(data_points) >= max_data_points:
                    stop_reason = "max_data_points"
                    return True
                tsid = pt.get("tsId")
                if tsid is None:
                    continue
                val = pt.get("value")
                if val is None or not isinstance(val, (int, float)):
                    continue
                data_points.append(
                    {
                        "tsId": str(tsid),
                        "value": float(val),
                        "timestampMs": int(ts_ms) if ts_ms is not None else None,
                    }
                )
            return False
        return False

    # Use wall_seconds as the per-read socket timeout so readline() cannot block
    # longer than the overall wall budget. This ensures the wall_seconds check
    # actually fires even when SignalFlow is slow to send data between bursts.
    per_read_timeout = max(5.0, min(wall_seconds, read_timeout))
    try:
        with urllib.request.urlopen(req, timeout=per_read_timeout) as resp:
            block: list[str] = []
            while True:
                if time.monotonic() - t0 > wall_seconds:
                    stop_reason = stop_reason or "wall_timeout"
                    break
                try:
                    line_b = resp.readline()
                except TimeoutError:
                    stop_reason = stop_reason or "wall_timeout"
                    break
                except OSError:
                    stop_reason = stop_reason or "wall_timeout"
                    break
                if not line_b:
                    break
                raw_bytes += len(line_b)
                if raw_bytes > max_body_bytes:
                    stop_reason = stop_reason or "max_response_bytes"
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if block:
                        ev, msg = _parse_sse_block(block)
                        block = []
                        if handle_msg(ev, msg):
                            break
                    continue
                block.append(line)
            if block and stop_reason is None and err is None:
                ev, msg = _parse_sse_block(block)
                handle_msg(ev, msg)
    except TimeoutError:
        err = err or "socket_timeout"
    except urllib.error.HTTPError as e:
        err = f"http_{e.code}:{(e.read() or b'')[:800].decode('utf-8', errors='replace')}"
    except urllib.error.URLError as e:
        err = f"url_error:{e.reason!s}"

    return metadata, data_points, err, stop_reason


def _rollup_prop_str(props: dict[str, Any], *keys: str) -> str:
    """First non-empty string among common SignalFlow / APM dimension property names."""
    for k in keys:
        v = props.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def rollup_spans_by_dims(
    metadata: dict[str, dict[str, Any]],
    data_points: list[dict[str, Any]],
) -> dict[tuple[str, str, str], float]:
    """Sum values keyed by (sf_service, sf_operation, sf_environment)."""
    sums: dict[tuple[str, str, str], float] = defaultdict(float)
    for dp in data_points:
        props = metadata.get(dp["tsId"], {})
        svc = _rollup_prop_str(props, "sf_service", "service", "serviceName")
        op = _rollup_prop_str(
            props,
            "sf_operation",
            "sf.operation",
            "operation",
            "operationName",
            "sfOperation",
        )
        env = _rollup_prop_str(props, "sf_environment", "environment")
        sums[(svc, op, env)] += dp["value"]
    return dict(sums)


def is_health_operation(operation: str) -> bool:
    """True if sf_operation / route text matches typical probe or health-RPC patterns.

    Matching is **case-insensitive** so variants like ``GET /Health`` still surface in the rollup.
    A leading HTTP method (``GET /ready``) is stripped for a **second** pass so path-only substrings
    still match consistently. Uses ``_health_operation_patterns_active`` (defaults to
    ``_DEFAULT_HEALTH_OPERATION_SUBSTRINGS``; merge profile/CLI extras via ``main()``).
    """
    if not operation:
        return False
    for cand in _health_operation_match_strings(operation):
        op_cf = cand.casefold()
        if any(p.casefold() in op_cf for p in _health_operation_patterns_active):
            return True
    return False


def topology_post(api_base: str, token: str, time_range_iso: str) -> dict[str, Any]:
    return splunk_post_json(
        api_base,
        "/v2/apm/topology",
        {"timeRange": time_range_iso},
        token,
        timeout=30.0,
    )


def edge_endpoints(edge: dict[str, Any]) -> tuple[str | None, str | None]:
    a = edge.get("fromNode") or edge.get("source") or edge.get("caller") or edge.get("from")
    b = edge.get("toNode") or edge.get("target") or edge.get("callee") or edge.get("to")
    if isinstance(a, dict):
        a = a.get("serviceName") or a.get("name")
    if isinstance(b, dict):
        b = b.get("serviceName") or b.get("name")
    if isinstance(a, str) and isinstance(b, str):
        return a, b
    return None, None


def node_service_env(node: dict[str, Any]) -> tuple[str, str]:
    svc = str(node.get("serviceName") or node.get("name") or "").strip()
    env = node.get("serviceEnvironment") or node.get("environment")
    if env is None and isinstance(node.get("tags"), dict):
        env = node["tags"].get("sf_environment")
    env_s = str(env).strip() if env else "—"
    return svc, env_s


def apm_search_traces(
    app_base: str,
    token: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int,
    services: list[str] | None = None,
    operations: list[str] | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    trace_filters: list[dict[str, Any]] = []
    tag_filters: list[dict[str, Any]] = []
    if environment:
        tag_filters.append(
            {"tag": "sf_environment", "operation": "IN", "values": [environment]}
        )
    if services:
        tag_filters.append({"tag": "sf_service", "operation": "IN", "values": services})
    if operations:
        tag_filters.append({"tag": "sf_operation", "operation": "IN", "values": operations})
    if tag_filters:
        trace_filters.append({"traceFilter": {"tags": tag_filters}, "filterType": "traceFilter"})

    parameters: dict[str, Any] = {
        "sharedParameters": {
            "timeRangeMillis": {"gte": start_ms, "lte": end_ms},
            "filters": trace_filters,
            "samplingFactor": 100,
        },
        "sectionsParameters": [{"sectionType": "traceExamples", "limit": limit}],
    }
    start_body = {
        "operationName": "StartAnalyticsSearch",
        "variables": {"parameters": parameters},
        "query": (
            "query StartAnalyticsSearch($parameters: JSON!) {\n"
            "  startAnalyticsSearch(parameters: $parameters)\n"
            "}\n"
        ),
    }
    start_result = splunk_post_json(
        app_base,
        "/v2/apm/graphql?op=StartAnalyticsSearch",
        start_body,
        token,
        timeout=30.0,
    )
    job_id = (
        (start_result.get("data") or {}).get("startAnalyticsSearch") or {}
    ).get("jobId")
    if not job_id:
        return {"error": "no_job_id", "raw": start_result}

    get_body = {
        "operationName": "GetAnalyticsSearch",
        "variables": {"jobId": job_id},
        "query": (
            "query GetAnalyticsSearch($jobId: ID!) {\n"
            "  getAnalyticsSearch(jobId: $jobId)\n"
            "}\n"
        ),
    }
    examples: list[Any] = []
    for _ in range(12):
        poll = splunk_post_json(
            app_base,
            "/v2/apm/graphql?op=GetAnalyticsSearch",
            get_body,
            token,
            timeout=30.0,
        )
        sections = (
            (poll.get("data") or {}).get("getAnalyticsSearch") or {}
        ).get("sections", [])
        for section in sections:
            if section.get("sectionType") == "traceExamples":
                examples = section.get("legacyTraceExamples") or []
                if section.get("isComplete"):
                    return {
                        "traces": examples[:limit],
                        "jobId": job_id,
                        "isComplete": True,
                    }
        time.sleep(0.5)

    return {"traces": examples[:limit], "jobId": job_id, "isComplete": False}


def trace_primary_operation(ex: dict[str, Any]) -> str | None:
    """Best-effort root / primary operation on a trace-analytics example payload."""
    for key in (
        "rootSpanOperationName",
        "rootOperationName",
        "operationName",
        "name",
        "primaryOperationName",
        "spanOperation",
        "rootOperation",
    ):
        v = ex.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    root = ex.get("rootSpan") or ex.get("rootSpanSummary") or ex.get("root")
    if isinstance(root, dict):
        for key in ("operationName", "name", "sf_operation", "operation"):
            v = root.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def trace_example_is_health_like(ex: dict[str, Any]) -> bool:
    """True if the example looks dominated by a health-style operation (substring rules)."""
    op = trace_primary_operation(ex)
    if op and is_health_operation(op):
        return True
    ops = ex.get("operations")
    if isinstance(ops, list):
        for o in ops:
            if isinstance(o, str) and is_health_operation(o):
                return True
            if isinstance(o, dict):
                nm = o.get("name") or o.get("operationName")
                if isinstance(nm, str) and is_health_operation(nm):
                    return True
    return False


def trace_example_trace_id(ex: dict[str, Any]) -> str | None:
    for k in ("traceId", "traceID", "id", "trace_id"):
        v = ex.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def ordered_trace_ids_from_examples(examples: list[dict[str, Any]]) -> list[str]:
    """Deduplicated trace IDs preserving first-seen order (for full-trace follow-up)."""
    seen: set[str] = set()
    out: list[str] = []
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        tid = trace_example_trace_id(ex)
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def get_trace_full_graphql(app_base: str, token: str, trace_id: str) -> dict[str, Any]:
    """Same GraphQL as MCP get_trace_full — spans with tags { key value }."""
    body = {
        "operationName": "TraceFullDetailsLessValidation",
        "variables": {"id": trace_id},
        "query": (
            "query TraceFullDetailsLessValidation($id: ID!) {"
            " trace(id: $id) {"
            " traceID startTime duration"
            " spans { spanID operationName serviceName"
            " startTime duration tags { key value } } } }"
        ),
    }
    return splunk_post_json(
        app_base,
        "/v2/apm/graphql?op=TraceFullDetailsLessValidation",
        body,
        token,
        timeout=30.0,
    )


def fetch_full_traces_by_ids(
    app_base: str,
    token: str,
    ordered_ids: list[str],
    *,
    max_successful: int,
    sleep_between_fetch_s: float,
    max_workers: int = 8,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    """
    Load each trace ID at most once, using a thread pool for parallel fetches.
    Returns:
    - cache: trace_id -> GraphQL ``trace`` object (includes ``spans``)
    - findings: per-trace load errors (HTTP/GraphQL)
    - stopped_at_cap: True if more IDs were left but ``max_successful`` successful loads was reached
    """
    # Cap the IDs we'll even attempt to the max_successful limit to avoid over-fetching
    ids_to_fetch = ordered_ids[:max_successful]
    stopped_at_cap = len(ordered_ids) > max_successful

    def _fetch_one(tid: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            raw = get_trace_full_graphql(app_base, token, tid)
        except RuntimeError as e:
            return tid, None, f"Could not load trace `{tid[:12]}…`: {str(e)[:120]}"
        if raw.get("errors"):
            return tid, None, f"Could not load trace `{tid[:12]}…` (GraphQL errors from get_trace_full)."
        tr = (raw.get("data") or {}).get("trace")
        if not isinstance(tr, dict):
            return tid, None, None
        spans = tr.get("spans") or []
        if not isinstance(spans, list):
            return tid, None, None
        return tid, tr, None

    cache: dict[str, dict[str, Any]] = {}
    findings: list[str] = []
    workers = max(1, min(max_workers, len(ids_to_fetch)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, tid): tid for tid in ids_to_fetch}
        for fut in concurrent.futures.as_completed(futures):
            tid, tr, err = fut.result()
            if err:
                findings.append(err)
            elif tr is not None:
                cache[tid] = tr

    # Preserve ordering of cache keys to match original ordered_ids (downstream code may rely on order)
    ordered_cache: dict[str, dict[str, Any]] = {}
    for tid in ids_to_fetch:
        if tid in cache:
            ordered_cache[tid] = cache[tid]
    return ordered_cache, findings, stopped_at_cap


def merge_unified_fetch_order(
    stratified_ids: list[str],
    service_to_minimal_ids: dict[str, list[str]],
    top_services_order: list[str],
) -> list[str]:
    """Stratified IDs first (span size + inspection), then minimal-span IDs per service not already seen."""
    seen: set[str] = set()
    out: list[str] = []
    for tid in stratified_ids:
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    for svc in top_services_order:
        for tid in service_to_minimal_ids.get(svc) or []:
            if tid and tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def compute_unified_full_trace_cap(
    need_minimal: bool,
    need_stratified: bool,
    minimal_max: int,
    span_max: int,
) -> int:
    """Upper bound on successful full-trace loads when merging ID lists (worst case: disjoint sets)."""
    if need_minimal and need_stratified:
        return max(1, minimal_max + span_max)
    if need_minimal:
        return max(1, minimal_max)
    return max(1, span_max)


def _tag_value_utf8_length(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, str):
        return len(val.encode("utf-8"))
    try:
        return len(json.dumps(val, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(val).encode("utf-8"))


def estimate_span_payload_bytes(span: dict[str, Any]) -> int:
    """
    Approximate UTF-8 byte size of span operation/service names plus all tag keys and values.
    Proxy for ingest / trace-volume payload attributable to attributes (not identical to billing bytes).
    """
    n = 0
    for k in ("operationName", "serviceName", "spanID"):
        v = span.get(k)
        if isinstance(v, str):
            n += len(v.encode("utf-8"))
    tags = span.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if not isinstance(t, dict):
                continue
            n += _tag_value_utf8_length(t.get("key"))
            n += _tag_value_utf8_length(t.get("value"))
    return n


def span_sf_environment(span: dict[str, Any]) -> str:
    """Environment from span tags: prefer ``deployment.environment`` (OTel), else ``sf_environment``."""
    tags = span.get("tags")
    if not isinstance(tags, list):
        return "—"
    dep: str = ""
    sf: str = ""
    for t in tags:
        if not isinstance(t, dict):
            continue
        key = str(t.get("key") or "")
        v = t.get("value")
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        if key == "deployment.environment":
            dep = s
        elif key == "sf_environment":
            sf = s
    if dep:
        return dep
    if sf:
        return sf
    return "—"


def top_services_by_span_volume(
    rollup: dict[tuple[str, str, str], float],
    *,
    top_n: int,
    exclude_health_ops: bool,
) -> list[str]:
    per_svc: dict[str, float] = defaultdict(float)
    for (svc, op, env), val in rollup.items():
        if not svc:
            continue
        if exclude_health_ops and is_health_operation(op):
            continue
        per_svc[svc] += val
    n = max(1, top_n)
    return [s for s, _ in sorted(per_svc.items(), key=lambda x: -x[1])[:n]]


def top_services_by_trace_volume(
    traces_rollup: dict[tuple[str, str], float] | None,
    *,
    top_n: int,
) -> list[str]:
    """Rank services by sum of traces.count across environments (requires traces SignalFlow rollup)."""
    if not traces_rollup:
        return []
    per_svc: dict[str, float] = defaultdict(float)
    for (svc, _env), val in traces_rollup.items():
        if svc:
            per_svc[svc] += val
    n = max(1, top_n)
    return [s for s, _ in sorted(per_svc.items(), key=lambda x: -x[1])[:n]]


def distinct_services_in_rollup(
    rollup: dict[tuple[str, str, str], float],
    *,
    exclude_health_ops: bool,
) -> set[str]:
    """Services with any non-zero span volume (optionally excluding health-like operations)."""
    out: set[str] = set()
    for (svc, op, env), val in rollup.items():
        if not svc or val <= 0:
            continue
        if exclude_health_ops and is_health_operation(op):
            continue
        out.add(svc)
    return out


def dynamic_top_service_count(num_distinct_services: int) -> int:
    """
    Min 5 services, up to ceil(10% of services), capped by org size.
    Examples: 13 -> 5, 100 -> 10, 500 -> 50.
    """
    if num_distinct_services <= 0:
        return 0
    return min(num_distinct_services, max(5, math.ceil(num_distinct_services * 0.1)))


def operations_ranked_for_service(
    rollup: dict[tuple[str, str, str], float],
    service: str,
    *,
    exclude_health_ops: bool,
    max_operations: int,
) -> list[tuple[str, float]]:
    """
    Distinct sf_operation values for a service, sorted by descending spans.count volume.
    """
    per_op: dict[str, float] = defaultdict(float)
    for (svc, op, env), val in rollup.items():
        if svc != service or not op:
            continue
        if exclude_health_ops and is_health_operation(op):
            continue
        per_op[op] += val
    ranked = sorted(per_op.items(), key=lambda x: -x[1])
    if max_operations > 0:
        ranked = ranked[:max_operations]
    return ranked


# --- Endpoint grouping (metrics-only) ---

_UNGROUPED_OP_HINT = re.compile(
    r"/\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|/user/\d+|/id/\d+",
    re.I,
)


def check_endpoint_grouping(
    rollup: dict[tuple[str, str, str], float],
    *,
    exclude_health_ops: bool,
    high_distinct_ops_yellow: int,
    max_suspicious_samples: int,
) -> dict[str, Any]:
    """
    Flag services with many distinct sf_operation values (possible grouping gap) and sample
    operation strings that look like raw IDs in paths (heuristic — not a substitute for UI rules).
    """
    per_svc: dict[str, set[str]] = defaultdict(set)
    for (svc, op, env), val in rollup.items():
        if not svc or not op or val <= 0:
            continue
        if exclude_health_ops and is_health_operation(op):
            continue
        per_svc[svc].add(op)

    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    suspicious_shown = 0

    for svc, ops in sorted(per_svc.items(), key=lambda x: -len(x[1])):
        n = len(ops)
        if n >= high_distinct_ops_yellow:
            rows.append(
                {
                    "color": "Yellow",
                    "serviceName": svc,
                    "environment": "—",
                    "endpointName": f"({n} distinct sf_operation in window)",
                    "note": "High endpoint cardinality — review grouping rules in APM settings.",
                }
            )
        for op in ops:
            if suspicious_shown >= max_suspicious_samples:
                break
            if _UNGROUPED_OP_HINT.search(op):
                rows.append(
                    {
                        "color": "Yellow",
                        "serviceName": svc,
                        "environment": "—",
                        "endpointName": op[:240],
                        "note": "Operation name may contain ungrouped IDs — confirm with UI.",
                    }
                )
                suspicious_shown += 1

    if not rows:
        findings.append("No grouping concerns stood out in this window.")
    else:
        findings.append(
            "Some services show many distinct endpoint names or path-like IDs — review grouping rules in APM settings."
        )
    rec = (
        "Collapse high-cardinality path segments in APM endpoint grouping rules; validate in the Splunk UI."
    )
    return {"rows": rows[:80], "findings": findings, "recommendations": [rec]}


# --- Stratified trace ID collection (shared by span size + inspection checks) ---


def resolve_stratified_service_plan(
    rollup: dict[tuple[str, str, str], float],
    *,
    top_services: int,
    exclude_health_ranking: bool,
) -> dict[str, Any]:
    """Build top service list and sampling mode. Keys: ok, svc_list, mode, n_pick, total_distinct, findings."""
    svc_universe = distinct_services_in_rollup(rollup, exclude_health_ops=exclude_health_ranking)
    total_distinct = len(svc_universe)
    findings: list[str] = []

    if top_services <= 0:
        n_pick = dynamic_top_service_count(total_distinct)
        mode = "dynamic"
    else:
        n_pick = min(top_services, total_distinct) if total_distinct else 0
        mode = "fixed"

    if n_pick <= 0:
        return {
            "ok": False,
            "svc_list": [],
            "mode": mode,
            "n_pick": n_pick,
            "total_distinct": total_distinct,
            "findings": findings,
        }

    svc_list = top_services_by_span_volume(
        rollup, top_n=n_pick, exclude_health_ops=exclude_health_ranking
    )
    if not svc_list and rollup and exclude_health_ranking:
        findings.append(
            "Only health-style traffic appeared in the rollup — widen the window or include health traffic in ranking if needed."
        )
        return {
            "ok": False,
            "svc_list": [],
            "mode": mode,
            "n_pick": n_pick,
            "total_distinct": total_distinct,
            "findings": findings,
        }
    if not svc_list:
        findings.append("No service traffic appeared in the rollup for this window — cannot sample traces.")
        return {
            "ok": False,
            "svc_list": [],
            "mode": mode,
            "n_pick": n_pick,
            "total_distinct": total_distinct,
            "findings": findings,
        }

    return {
        "ok": True,
        "svc_list": svc_list,
        "mode": mode,
        "n_pick": n_pick,
        "total_distinct": total_distinct,
        "findings": findings,
    }


def collect_stratified_trace_ids(
    app_base: str,
    token: str,
    start_ms: int,
    end_ms: int,
    rollup: dict[tuple[str, str, str], float],
    svc_list: list[str],
    *,
    traces_per_endpoint: int,
    max_operations_per_service: int,
    max_trace_fetches: int,
    exclude_health_ranking: bool,
    sleep_between_fetch_s: float,
) -> tuple[list[str], list[str], list[dict[str, Any]], int, bool]:
    """
    Returns: trace_ids, finding_lines, endpoint_shortfall, search_calls, stopped_by_cap.
    Searches are issued in parallel (one worker per service).
    """
    findings: list[str] = []
    trace_ids_ordered: list[str] = []
    seen: set[str] = set()
    endpoint_shortfall: list[dict[str, Any]] = []
    search_calls = 0
    stopped_by_cap = False

    # Build full work list first (svc → ops pairs), then search in parallel per service.
    svc_ops: list[tuple[str, list[tuple[str, float]]]] = []
    for svc in svc_list:
        ops_ranked = operations_ranked_for_service(
            rollup,
            svc,
            exclude_health_ops=exclude_health_ranking,
            max_operations=max_operations_per_service,
        )
        if not ops_ranked:
            findings.append(f"No eligible operations for `{svc}` in this view — skipped.")
        else:
            svc_ops.append((svc, ops_ranked))

    def _search_svc(svc: str, ops: list[tuple[str, float]]) -> tuple[
        str, list[str], list[dict[str, Any]], list[str], int
    ]:
        """Search all ops for one service; returns (svc, trace_ids, shortfalls, findings, n_calls)."""
        local_ids: list[str] = []
        local_seen: set[str] = set()
        local_shortfall: list[dict[str, Any]] = []
        local_findings: list[str] = []
        n_calls = 0
        for op, vol in ops:
            need = traces_per_endpoint
            collected = 0
            n_calls += 1
            try:
                r = apm_search_traces(
                    app_base, token, start_ms, end_ms,
                    limit=max(need + 5, 12),
                    services=[svc],
                    operations=[op],
                )
            except RuntimeError as e:
                local_findings.append(f"Trace lookup failed for `{svc}` / `{op[:48]}`: {str(e)[:200]}")
                local_shortfall.append({"service": svc, "operation": op, "collected": 0, "target": need, "error": True})
                continue
            if r.get("error"):
                local_findings.append(f"{svc} / {op[:48]}: {r.get('error')}")
                local_shortfall.append({"service": svc, "operation": op, "collected": 0, "target": need, "error": True})
                continue
            for ex in r.get("traces") or []:
                if not isinstance(ex, dict):
                    continue
                tid = trace_example_trace_id(ex)
                if not tid or tid in local_seen:
                    continue
                local_seen.add(tid)
                local_ids.append(tid)
                collected += 1
                if collected >= need:
                    break
            if collected < need:
                local_shortfall.append({
                    "service": svc, "operation": op,
                    "collected": collected, "target": need,
                    "rollup_volume": round(vol, 2),
                })
        return svc, local_ids, local_shortfall, local_findings, n_calls

    workers = max(1, min(8, len(svc_ops)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_search_svc, svc, ops) for svc, ops in svc_ops]
        # Collect in svc_list order to keep trace ordering deterministic.
        # Cap total wall time at 120s — if the APM API is unreachable (e.g. VPN/firewall)
        # individual requests time out at 30s but many parallel fetches can still add up.
        svc_results: dict[str, tuple] = {}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=120):
                svc, ids, shortfall, fnd, n_calls = fut.result()
                svc_results[svc] = (ids, shortfall, fnd, n_calls)
        except concurrent.futures.TimeoutError:
            logger.warning("APM trace collection timed out after 120s — APM API may be unreachable (VPN/firewall). Partial results only.")

    for svc, _ in svc_ops:
        if svc not in svc_results:
            continue
        ids, shortfall, fnd, n_calls = svc_results[svc]
        search_calls += n_calls
        findings.extend(fnd)
        endpoint_shortfall.extend(shortfall)
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                trace_ids_ordered.append(tid)
            if len(trace_ids_ordered) >= max_trace_fetches:
                stopped_by_cap = True
                break
        if stopped_by_cap:
            break

    return trace_ids_ordered, findings, endpoint_shortfall, search_calls, stopped_by_cap


def collect_minimal_spans_trace_ids(
    app_base: str,
    token: str,
    start_ms: int,
    end_ms: int,
    top_services: list[str],
    *,
    fetch_limit: int,
    traces_per_service: int,
    exclude_health: bool,
    max_workers: int = 8,
) -> tuple[dict[str, list[str]], list[str]]:
    """
    Trace Analytics search — ordered trace IDs per service, fetched in parallel.
    Returns (service_to_ids, findings).
    """
    def _search_one(svc: str) -> tuple[str, list[str] | None, str | None]:
        try:
            r = apm_search_traces(
                app_base,
                token,
                start_ms,
                end_ms,
                limit=max(fetch_limit, traces_per_service),
                services=[svc],
            )
        except RuntimeError as e:
            return svc, None, f"{svc}: trace search failed ({str(e)[:200]})."
        if r.get("error"):
            return svc, None, f"{svc}: {r.get('error')}"
        traces = r.get("traces") or []
        pool: list[dict[str, Any]] = []
        for ex in traces:
            if not isinstance(ex, dict):
                continue
            if exclude_health and trace_example_is_health_like(ex):
                continue
            pool.append(ex)
        if exclude_health and not pool and traces:
            return svc, None, (
                f"{svc}: the sample only contained health-style traffic — no minimal-span stats for this service."
            )
        ids = ordered_trace_ids_from_examples(pool)
        if not ids:
            return svc, None, (
                f"{svc}: no trace IDs in Trace Analytics examples — cannot load full traces for span counts."
            )
        return svc, ids, None

    service_to_ids: dict[str, list[str]] = {}
    findings: list[str] = []
    if not top_services:
        return service_to_ids, findings

    workers = max(1, min(max_workers, len(top_services)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_search_one, svc): svc for svc in top_services}
        for fut in concurrent.futures.as_completed(futures):
            svc, ids, err = fut.result()
            if err:
                findings.append(err)
            elif ids is not None:
                service_to_ids[svc] = ids

    # Preserve original service ordering
    ordered: dict[str, list[str]] = {}
    for svc in top_services:
        if svc in service_to_ids:
            ordered[svc] = service_to_ids[svc]
    return ordered, findings


# --- Sensitive / debug span heuristics (same trace fetch as span size) ---

_SENSITIVE_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "api_key",
    "credential",
    "ssn",
    "social",
    "credit",
    "card",
    "authorization",
    "auth",
    "cookie",
    "bearer",
    "pin",
    "token",
)

_DEBUG_OP_SUBSTRINGS: tuple[str, ...] = ("debug", "verbose", "trace", "loglevel", "log_level")
_DEBUG_TAG_HINTS: tuple[str, ...] = ("debug", "verbose", "trace", "fatal")


def _span_sensitive_hits(sp: dict[str, Any]) -> list[str]:
    hits: list[str] = []
    for t in sp.get("tags") or []:
        if not isinstance(t, dict):
            continue
        k = str(t.get("key") or "").lower()
        for frag in _SENSITIVE_KEY_FRAGMENTS:
            if frag in k:
                hits.append(f"tag key matches sensitive fragment `{frag}`")
                break
        val = t.get("value")
        if isinstance(val, str) and "@" in val and "." in val and len(val) < 120:
            if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", val.strip()):
                hits.append("tag value looks email-shaped (redact in production)")
    op = str(sp.get("operationName") or "").lower()
    for frag in ("password", "secret", "token", "credential"):
        if frag in op:
            hits.append(f"operation name contains `{frag}`")
    return hits


def _span_debug_like(sp: dict[str, Any]) -> bool:
    op = str(sp.get("operationName") or "").lower()
    if any(s in op for s in _DEBUG_OP_SUBSTRINGS):
        return True
    for t in sp.get("tags") or []:
        if not isinstance(t, dict):
            continue
        k = str(t.get("key") or "").lower()
        v = str(t.get("value") or "").lower()
        if any(x in k for x in ("log.level", "log_level", "level", "severity", "verbosity")):
            if any(h in v for h in _DEBUG_TAG_HINTS):
                return True
        if "debug" in v or "verbose" in v or "trace" in v:
            return True
    return False


def run_trace_inspection_checks(
    trace_ids: list[str],
    fetched_traces: dict[str, dict[str, Any]],
    *,
    max_traces: int,
) -> dict[str, Any]:
    """
    Inspect spans using **pre-fetched** full traces only (no get_trace_full).
    Returns keys: sensitive_data, debug_spans (each check_* shaped).
    """
    sens_raw: list[dict[str, Any]] = []
    dbg_rows: list[dict[str, Any]] = []
    findings_s: list[str] = []
    findings_d: list[str] = []
    fetches = 0

    for tid in trace_ids:
        if fetches >= max_traces:
            break
        tr = fetched_traces.get(tid)
        if not isinstance(tr, dict):
            continue
        spans = tr.get("spans") or []
        if not isinstance(spans, list):
            continue
        fetches += 1

        for sp in spans:
            if not isinstance(sp, dict):
                continue
            svc = str(sp.get("serviceName") or "").strip() or "—"
            op = str(sp.get("operationName") or "").strip() or "—"
            env = span_sf_environment(sp)
            for note in _span_sensitive_hits(sp):
                sens_raw.append(
                    {
                        "color": "Red",
                        "serviceName": svc,
                        "environment": env,
                        "spanName": op,
                        "note": note,
                    }
                )
            if _span_debug_like(sp):
                dbg_rows.append(
                    {
                        "color": "Yellow",
                        "serviceName": svc,
                        "environment": env,
                        "spanName": op,
                        "note": "Debug/verbose-like span name or tag",
                    }
                )

    findings_s.append(
        "Automated pattern match only — validate any sensitive-data hits before acting."
    )
    findings_d.append(
        "Some matches may be legitimate debug or correlation spans — confirm in context."
    )

    sens_seen: set[tuple[str, str, str]] = set()
    sens_rows: list[dict[str, Any]] = []
    for r in sens_raw:
        op = str(r.get("spanName") or "").strip() or "—"
        k = (str(r.get("serviceName") or ""), str(r.get("environment") or ""), op)
        if k in sens_seen:
            continue
        sens_seen.add(k)
        sens_rows.append(
            {
                "color": r.get("color"),
                "serviceName": r["serviceName"],
                "spanName": op,
            }
        )

    # One row per (service × environment × operation); duplicate triples from multiple spans are dropped.
    dbg_seen: set[tuple[str, str, str]] = set()
    dbg_deduped: list[dict[str, Any]] = []
    for r in dbg_rows:
        op = str(r.get("spanName") or "").strip() or "—"
        k = (str(r.get("serviceName") or ""), str(r.get("environment") or ""), op)
        if k in dbg_seen:
            continue
        dbg_seen.add(k)
        dbg_deduped.append(
            {
                "color": r.get("color"),
                "serviceName": r["serviceName"],
                "environment": r["environment"],
                "operationName": op,
            }
        )

    return {
        "sensitive_data": {
            "rows": sens_rows[:50],
            "findings": findings_s,
            "recommendations": [
                "Remove secrets from span tags; use redaction processors or log pipelines instead."
            ],
        },
        "debug_spans": {
            "rows": dbg_deduped[:50],
            "findings": findings_d,
            "recommendations": [
                "Disable verbose debug spans in production; reduce log level in instrumentation."
            ],
        },
        "trace_fetches": fetches,
    }


def check_large_span_sizes(
    app_base: str,
    token: str,
    start_ms: int,
    end_ms: int,
    rollup: dict[tuple[str, str, str], float],
    *,
    top_services: int,
    traces_per_endpoint: int,
    max_operations_per_service: int,
    max_trace_fetches: int,
    exclude_health_ranking: bool,
    sleep_between_fetch_s: float,
    precomputed_trace_ids: list[str] | None = None,
    precomputed_collection_meta: dict[str, Any] | None = None,
    fetched_traces: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Stratified trace samples: top services (dynamic min 5 / 10% by default) × top endpoints
    (sf_operation from rollup), at least ``traces_per_endpoint`` trace IDs per (service, endpoint)
    when Trace Analytics returns enough examples. Pooled span payload sizes → mean + 2σ outliers.

    If ``precomputed_trace_ids`` is set (shared with inspection checks), ID collection is skipped.
    If ``fetched_traces`` is set, ``get_trace_full`` is not called — spans come from the shared cache.
    """
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    plan = resolve_stratified_service_plan(
        rollup,
        top_services=top_services,
        exclude_health_ranking=exclude_health_ranking,
    )
    if not plan["ok"]:
        findings.extend(plan["findings"])
        return {
            "rows": [],
            "findings": findings,
            "recommendations": [],
            "stats": {
                "sampling_mode": plan["mode"],
                "distinct_services_in_rollup": plan["total_distinct"],
                "top_services_target": plan["n_pick"],
            },
        }

    svc_list = plan["svc_list"]
    mode = plan["mode"]
    n_pick = plan["n_pick"]
    total_distinct = plan["total_distinct"]

    findings.append(
        f"Reviewing **{len(svc_list)}** active services from **{total_distinct}** seen in the window."
    )

    if precomputed_trace_ids is not None:
        trace_ids_ordered = list(precomputed_trace_ids)
        endpoint_shortfall = list((precomputed_collection_meta or {}).get("endpoint_shortfall") or [])
        search_calls = int((precomputed_collection_meta or {}).get("search_calls") or 0)
        stopped_by_cap = bool((precomputed_collection_meta or {}).get("stopped_by_cap"))
        findings.extend((precomputed_collection_meta or {}).get("collection_findings") or [])
    else:
        trace_ids_ordered, coll_findings, endpoint_shortfall, search_calls, stopped_by_cap = (
            collect_stratified_trace_ids(
                app_base,
                token,
                start_ms,
                end_ms,
                rollup,
                svc_list,
                traces_per_endpoint=traces_per_endpoint,
                max_operations_per_service=max_operations_per_service,
                max_trace_fetches=max_trace_fetches,
                exclude_health_ranking=exclude_health_ranking,
                sleep_between_fetch_s=sleep_between_fetch_s,
            )
        )
        findings.extend(coll_findings)

    if stopped_by_cap:
        findings.append(
            "Stopped at the configured trace sample limit — some service/endpoint pairs may be under-represented."
        )

    span_records: list[dict[str, Any]] = []
    fetches_ok = 0
    for tid in trace_ids_ordered:
        if fetches_ok >= max_trace_fetches:
            break
        tr: dict[str, Any] | None = None
        if fetched_traces is not None:
            tr = fetched_traces.get(tid)
        else:
            try:
                raw = get_trace_full_graphql(app_base, token, tid)
            except RuntimeError as e:
                findings.append(f"Could not load trace `{tid[:16]}…`: {str(e)[:160]}")
                time.sleep(sleep_between_fetch_s)
                continue
            if raw.get("errors"):
                findings.append(f"Could not load trace `{tid[:16]}…` for analysis.")
                time.sleep(sleep_between_fetch_s)
                continue
            tr = (raw.get("data") or {}).get("trace")
            if not isinstance(tr, dict):
                time.sleep(sleep_between_fetch_s)
                continue
        if not isinstance(tr, dict):
            continue
        spans = tr.get("spans") or []
        if not isinstance(spans, list):
            if fetched_traces is None:
                time.sleep(sleep_between_fetch_s)
            continue
        fetches_ok += 1
        for sp in spans:
            if not isinstance(sp, dict):
                continue
            b = estimate_span_payload_bytes(sp)
            span_records.append(
                {
                    "traceId": tid,
                    "serviceName": str(sp.get("serviceName") or "").strip() or "—",
                    "operationName": str(sp.get("operationName") or "").strip() or "—",
                    "environment": span_sf_environment(sp),
                    "bytes": b,
                }
            )
        if fetched_traces is None:
            time.sleep(sleep_between_fetch_s)

    sizes = [r["bytes"] for r in span_records]
    n_spans = len(sizes)
    stats: dict[str, Any] = {
        "sampling_mode": mode,
        "distinct_services_in_rollup": total_distinct,
        "top_services_target": n_pick,
        "services_sampled": len(svc_list),
        "trace_analytics_searches": search_calls,
        "traces_per_endpoint_target": traces_per_endpoint,
        "max_operations_per_service": max_operations_per_service,
        "endpoint_pairs_below_target": len(endpoint_shortfall),
        "services_considered": len(svc_list),
        "trace_fetches": fetches_ok,
        "span_sample_size": n_spans,
    }
    if fetched_traces is not None:
        stats["shared_trace_cache"] = True
    if endpoint_shortfall:
        stats["endpoint_shortfall"] = endpoint_shortfall[:40]

    if n_spans < 3:
        findings.append(
            "Too few spans in the sample to highlight outliers reliably — try a wider time window or adjust sampling."
        )
        return {
            "rows": rows,
            "findings": findings,
            "recommendations": [
                "Widen the window or adjust sampling so enough spans are available to compare sizes."
            ],
            "stats": stats,
        }

    mean_b = statistics.mean(sizes)
    stdev_b = statistics.stdev(sizes) if n_spans >= 2 else 0.0
    stats["mean_bytes"] = round(mean_b, 2)
    stats["stdev_bytes"] = round(stdev_b, 2)
    threshold = mean_b + 2.0 * stdev_b
    stats["threshold_mean_plus_2sigma_bytes"] = round(threshold, 2)

    findings.append(
        f"Compared {n_spans} spans from {fetches_ok} traces; unusually large attribute sets are listed below."
    )

    if stdev_b == 0.0:
        findings.append("No variation in this sample — no standout large spans by the outlier rule.")
        return {
            "rows": [],
            "findings": findings,
            "recommendations": [
                "If this is unexpected, widen the window or focus on a busier service."
            ],
            "stats": stats,
        }

    outliers = [r for r in span_records if r["bytes"] > threshold]
    # One row per (serviceName, environment, operationName); keep largest observed payload
    by_triple: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in outliers:
        key = (r["serviceName"], r["environment"], r["operationName"])
        prev = by_triple.get(key)
        if prev is None or r["bytes"] > prev["bytes"]:
            by_triple[key] = r

    deduped = sorted(
        by_triple.values(),
        key=lambda x: (-x["bytes"], x["serviceName"], x["environment"], x["operationName"]),
    )
    for r in deduped[:50]:
        rows.append(
            {
                "color": "Red",
                "serviceName": r["serviceName"],
                "environment": r["environment"],
                "operationName": r["operationName"],
                "est_bytes": r["bytes"],
            }
        )

    recs = [
        "Trim oversized attributes at the collector or application; move large blobs to logs."
    ]
    if not rows:
        findings.append("No spans stood out as unusually large in this sample.")
        recs = ["No change needed unless traffic patterns shift — re-run after major releases."]

    return {"rows": rows, "findings": findings, "recommendations": recs, "stats": stats}


# ---------------------------------------------------------------------------
# Individual checks → structured outcomes
# ---------------------------------------------------------------------------


def check_health_endpoints(
    rollup: dict[tuple[str, str, str], float],
    *,
    app_base: str | None = None,
    token: str | None = None,
    start_ms: int | None = None,
    stop_ms: int | None = None,
    rollup_stop_reason: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    if rollup_stop_reason == "max_data_points":
        findings.append(
            "The **spans.count** SignalFlow stream stopped at the datapoint cap before it finished — "
            "in high-cardinality orgs, low-volume operations (including health checks) may be missing from "
            "this rollup. Re-run with a higher `--signalflow-max-data-points`, or shorten `--hours`."
        )
    # One row per health-like (service, environment, sf_operation)
    for (svc, op, env), val in sorted(
        rollup.items(),
        key=lambda x: (x[0][0] or "", x[0][2] or "", x[0][1] or ""),
    ):
        if not svc or not is_health_operation(op):
            continue
        rows.append(
            {
                "color": "Yellow",
                "service": svc,
                "environment": env,
                "endpointName": op,
                "span_volume_proxy": round(val, 2),
                "requestCountWindow": None,
            }
        )

    if not rows:
        if rollup_stop_reason == "max_data_points":
            if rollup:
                findings.append(
                    "No health-like **sf_operation** values appeared in the **partial** rollup — they may have been "
                    "omitted by the datapoint cap, or there was no matching traffic in the window."
                )
            else:
                findings.append(
                    "The **spans.count** rollup was empty when the stream stopped (often truncation or an ingest gap) — "
                    "try a higher `--signalflow-max-data-points` or confirm APM metrics in the chosen window."
                )
        else:
            findings.append("No health check–style endpoint traffic was detected in this window.")
    else:
        findings.append(
            "Health check traffic appears on one or more services — confirm whether that instrumentation "
            "should remain, move to synthetics, or be reduced."
        )
        if app_base and token and start_ms is not None and stop_ms is not None:
            lb = max(60_000, int(stop_ms - start_ms))
            rows, extra = enrich_health_endpoints_with_request_counts(
                rows,
                app_base,
                token,
                lookback_millis=lb,
                resolution_millis=60_000,
            )
            findings.extend(extra)
        else:
            findings.append("Request counts were not loaded for this run (missing app URL, token, or time window).")

    rec = (
        "Use Synthetics for health checks where possible; drop or sample health spans at the "
        "collector (OTel) to reduce noise and cost."
    )
    out: dict[str, Any] = {
        "rows": rows,
        "findings": findings,
        "recommendations": [rec],
    }
    if start_ms is not None and stop_ms is not None:
        out["requests_period_label"] = format_requests_period_label(start_ms, stop_ms)
    return out


def check_usage_by_service(
    rollup: dict[tuple[str, str, str], float],
    traces_rollup: dict[tuple[str, str], float] | None,
) -> dict[str, Any]:
    per_svc_env: dict[tuple[str, str], float] = defaultdict(float)
    for (svc, op, env), val in rollup.items():
        if not svc:
            continue
        per_svc_env[(svc, env)] += val

    keys: set[tuple[str, str]] = set(per_svc_env.keys())
    if traces_rollup:
        keys |= {k for k in traces_rollup.keys() if k[0]}

    trace_total_all = sum(traces_rollup.values()) if traces_rollup else 0.0

    def _usage_sort_key(k: tuple[str, str]) -> tuple:
        vol = per_svc_env.get(k, 0.0)
        if traces_rollup:
            trv = traces_rollup.get(k) or 0.0
            return (-trv, -vol, k[0] or "", k[1] or "")
        return (-vol, k[0] or "", k[1] or "")

    rows: list[dict[str, Any]] = []
    for (svc, env) in sorted(keys, key=_usage_sort_key):
        tr_raw = traces_rollup.get((svc, env)) if traces_rollup else None
        tr = float(tr_raw) if tr_raw is not None else None

        total_traces_cell: str | float = "—"
        if tr is not None:
            total_traces_cell = round(tr, 2)

        pct: str | float = "—"
        if trace_total_all > 0 and tr is not None:
            pct = round(100.0 * tr / trace_total_all, 2)

        rows.append(
            {
                "serviceName": svc,
                "environment": env,
                "total_traces": total_traces_cell,
                "pct_total": pct,
            }
        )

    findings = [
        "Use this view to see which services drive trace volume — good for prioritizing instrumentation and spend."
    ]
    return {"rows": rows, "findings": findings, "recommendations": []}


def check_orphan_services(
    topology: dict[str, Any],
    service_universe: set[str],
) -> dict[str, Any]:
    data = topology.get("data") or topology
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    touched: set[str] = set()
    for e in edges:
        a, b = edge_endpoints(e if isinstance(e, dict) else {})
        if a:
            touched.add(a)
        if b:
            touched.add(b)

    rows: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        svc, env = node_service_env(n)
        if not svc:
            continue
        if svc not in touched:
            rows.append({"color": "Yellow", "service": svc, "environment": env})

    findings: list[str] = []
    missing_topology = not nodes
    if missing_topology:
        findings.append("Topology returned no nodes for the selected window.")
    elif not rows:
        findings.append("No orphan services detected (every node had at least one topology edge).")
    else:
        findings.append(
            "These services appear on the map without dependencies — confirm they are still intended."
        )

    rec = (
        "Validate whether orphan services are expected stand-alone workloads or stale instrumentation."
    )
    return {"rows": rows, "findings": findings, "recommendations": [rec] if rows else []}


def analyze_minimal_spans_from_cache(
    top_services: list[str],
    *,
    traces_per_service: int,
    exclude_health: bool,
    service_to_ids: dict[str, list[str]],
    fetched_traces: dict[str, dict[str, Any]],
    collection_findings: list[str],
    stopped_at_unified_cap: bool,
    unified_fetch_cap: int,
) -> dict[str, Any]:
    """
    Minimal-span stats from **shared** full-trace cache: span count is ``len(spans)`` on each trace.
    """
    rows: list[dict[str, Any]] = []
    findings: list[str] = list(collection_findings)
    for svc in top_services:
        ids = service_to_ids.get(svc) or []
        counts: list[int] = []
        for tid in ids:
            if len(counts) >= traces_per_service:
                break
            tr = fetched_traces.get(tid)
            if not isinstance(tr, dict):
                continue
            spans = tr.get("spans") or []
            if not isinstance(spans, list):
                continue
            counts.append(len(spans))

        if not counts:
            findings.append(
                f"{svc}: no full traces in the shared cache for minimal-span stats "
                f"(searched {len(ids)} trace ID(s))."
            )
            continue
        if exclude_health and len(counts) < traces_per_service and not stopped_at_unified_cap:
            findings.append(
                f"{svc}: only {len(counts)} full trace(s) in the shared cache (target {traces_per_service} — "
                "try a wider window or raise --trace-fetch-limit)."
            )
        n = len(counts)
        red_pct = 100.0 * sum(1 for c in counts if c <= 2) / n
        yellow_pct = 100.0 * sum(1 for c in counts if 3 <= c <= 5) / n
        color = "—"
        if red_pct >= 20:
            color = "Red"
        elif yellow_pct >= 20:
            color = "Yellow"
        rows.append(
            {
                "color": color,
                "service": svc,
                "sample_size": n,
                "pct_1_2_spans": round(red_pct, 1),
                "pct_3_5_spans": round(yellow_pct, 1),
            }
        )

    if stopped_at_unified_cap:
        findings.append(
            f"Unified full-trace fetch reached its cap ({unified_fetch_cap} successful loads); "
            "some trace IDs were not retrieved — minimal-span and other trace checks share this pool."
        )

    notes = [
        "Span counts come from shared full trace payloads (get_trace_full); each trace ID is loaded at most once "
        "for all trace-based checks. Percentages are over traces present in the cache, not all production traffic.",
    ]
    if exclude_health:
        notes.append(
            "Health-like operations (same heuristics as the Health Endpoints check) are excluded from "
            "ranking and from Trace Analytics examples before full-trace loads; API filters use exact "
            "IN lists for services only, so exclusion uses client-side filtering on example metadata."
        )
    findings.extend(notes)
    rec = (
        "Review Yellow/Red services for low-value traces; tune instrumentation or drop noisy spans at the collector."
    )
    full_fetches = len(fetched_traces)
    return {
        "rows": rows,
        "findings": findings,
        "recommendations": [rec] if rows else [],
        "stats": {
            "full_trace_fetches": full_fetches,
            "full_trace_fetch_cap": unified_fetch_cap,
            "shared_trace_cache": True,
        },
    }


def check_tags_high_cardinality(
    raw: dict[str, Any],
    *,
    timestamp_ms: int,
    raw_get_tags: dict[str, Any] | None = None,
    get_tags_error: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    indexed_names: set[str] | None = None
    apply_index_filter = False

    if (
        raw_get_tags is not None
        and not get_tags_error
        and not raw_get_tags.get("errors")
    ):
        indexed_names, _gt_warnings = collect_indexed_tag_names_from_get_tags(raw_get_tags)
        if indexed_names:
            apply_index_filter = True

    data = raw.get("data") if isinstance(raw, dict) else None
    tags = (data or {}).get("getSubscriptionUsageTags")
    if raw.get("errors"):
        findings.append("Subscription tag usage request returned an error.")
    if not isinstance(tags, list):
        return {
            "rows": rows,
            "findings": findings
            or ["None — check not executed or empty subscription tags response."],
            "recommendations": [
                "Open Subscription usage in Splunk Observability and confirm tag analytics are available for this org."
            ],
            "timestampMillis": str(timestamp_ms),
            "indexed_tag_catalog_size": len(indexed_names) if indexed_names is not None else None,
        }

    for t in tags:
        if not isinstance(t, dict):
            continue
        name = str(t.get("tagName") or "").strip()
        if apply_index_filter and indexed_names is not None:
            if not subscription_tag_matches_indexed_catalog(name, indexed_names):
                continue
        cc = t.get("charCount")
        pct = t.get("percentage")
        try:
            pct_f = float(pct) if pct is not None else 0.0
        except (TypeError, ValueError):
            pct_f = 0.0
        rows.append(
            {
                "color": "Yellow",
                "tagName": name,
                "charCount": cc,
                "pct_total": round(pct_f, 2) if pct is not None else "—",
            }
        )
    rec = (
        "Confirm distinct value cardinality with Usage Analytics tooling where needed. Consider if the tag "
        "needs to be indexed and if the scope can be reduced."
    )
    out: dict[str, Any] = {
        "rows": rows,
        "findings": findings,
        "recommendations": [rec],
        "timestampMillis": str(timestamp_ms),
    }
    if indexed_names is not None:
        out["indexed_tag_catalog_size"] = len(indexed_names)
    elif apply_index_filter:
        out["indexed_tag_catalog_size"] = 0
    return out


def check_not_executed(reason: str) -> dict[str, Any]:
    return {
        "rows": [],
        "findings": [f"None — check not executed. {reason}"],
        "recommendations": [],
    }


# ---------------------------------------------------------------------------
# Markdown report (internal snapshot; customer-facing file stays outcomes-only per AGENTS.md)
# ---------------------------------------------------------------------------


def _md_esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def format_requests_period_label(start_ms: int | None, stop_ms: int | None) -> str:
    """UTC range for Health Endpoints JSON metadata (assessment window for request counts)."""
    if start_ms is None or stop_ms is None:
        return "analysis window"
    s = time.strftime("%Y-%m-%d %H:%M", time.gmtime(start_ms / 1000))
    e = time.strftime("%Y-%m-%d %H:%M", time.gmtime(stop_ms / 1000))
    delta_ms = int(stop_ms) - int(start_ms)
    if delta_ms <= 0:
        return f"{s}–{e} UTC"
    hrs = delta_ms / 3_600_000.0
    if hrs >= 1.0 and abs(hrs - round(hrs)) < 0.06:
        return f"{s}–{e} UTC ({hrs:.0f}h)"
    mins = delta_ms / 60_000.0
    if mins >= 1.0 and abs(mins - round(mins)) < 0.06:
        return f"{s}–{e} UTC ({mins:.0f}m)"
    return f"{s}–{e} UTC"


_HEALTH_COLOR_ORDER = {"Red": 0, "Yellow": 1, "Green": 2}


def _severity_rank(color: str | None) -> int:
    """Lower = higher priority for health-check tables (Red → Yellow → Green)."""
    c = (str(color) if color is not None else "").strip()
    return _HEALTH_COLOR_ORDER.get(c, 3)


def _sort_rows_by_health_color(
    rows: list[dict[str, Any]],
    *,
    within_tier: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Sort Result rows for customer-facing reports: **Red** first, **Yellow** second, **Green** third;
    unknown/missing colors last. Optional ``within_tier`` orders rows inside each color band.
    """

    def _default_within(r: dict[str, Any]) -> tuple:
        return (
            str(r.get("serviceName") or r.get("service") or ""),
            str(r.get("environment") or ""),
            str(
                r.get("operationName")
                or r.get("endpointName")
                or r.get("spanName")
                or r.get("tagName")
                or ""
            ),
        )

    w = within_tier or _default_within
    return sorted(rows, key=lambda r: (_severity_rank(r.get("color")), w(r)))


def _within_tag_unique_values(r: dict[str, Any]) -> tuple:
    """Sort by indexed volume proxy (charCount) descending within each color band."""
    v = r.get("charCount")
    try:
        vf = float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        vf = 0.0
    return (-vf, str(r.get("tagName") or ""))


def _within_debug_svc_env_op(r: dict[str, Any]) -> tuple:
    return (
        str(r.get("serviceName") or ""),
        str(r.get("environment") or ""),
        str(r.get("operationName") or ""),
    )


def _within_large_span_est_bytes(r: dict[str, Any]) -> tuple:
    eb = r.get("est_bytes")
    try:
        ebf = float(eb) if eb is not None else 0.0
    except (TypeError, ValueError):
        ebf = 0.0
    return (
        -ebf,
        str(r.get("serviceName") or ""),
        str(r.get("environment") or ""),
        str(r.get("operationName") or ""),
    )


def render_apm_checks_markdown(
    checks: dict[str, Any],
    *,
    heading_prefix: str = "##",
) -> str:
    """
    Per-check **Results** / **Recommendation** blocks (checklist-shaped).
    Use ``heading_prefix='##'`` for standalone APM snapshot; ``'###'`` when nested under
    a parent ``## APM health checks`` section in a consolidated report.
    """
    lines: list[str] = []
    for key in CHECK_KEYS:
        if key not in checks:
            continue
        block = checks[key]
        title_h = CHECK_TITLES.get(key, key)
        lines += [f"{heading_prefix} {title_h}", ""]
        desc = CHECK_DESCRIPTIONS.get(key)
        if desc:
            lines += [desc, ""]

        # Results tables vary by check
        if key == "health_endpoints":
            req_col = _md_esc("Requests (24H)")
            lines += [
                "### Results",
                "",
                f"| Color | Service Name | Environment Name | Endpoint Name | {req_col} |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(list(block.get("rows") or [])):
                ep = r.get("endpointName", "—")
                rc = r.get("requestCountWindow")
                rc_s = fmt_amount_commas(rc)
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(r['service'])} | "
                    f"{_md_esc(r['environment'])} | {_md_esc(str(ep))} | {rc_s} |"
                )
        elif key == "usage_by_service":
            lines += [
                "### Results",
                "",
                "| ServiceName | Environment | Total Traces | % of Total Usage |",
                "| --- | --- | --- | --- |",
            ]
            for r in block.get("rows") or []:
                tt = r.get("total_traces", "—")
                tt_s = "—" if tt == "—" else fmt_table_number(tt, decimals=2)
                pct = r.get("pct_total", "—")
                pct_s = "—" if pct == "—" else fmt_table_number(pct, decimals=2, percent=True)
                lines.append(
                    f"| {_md_esc(r['serviceName'])} | {_md_esc(r['environment'])} | "
                    f"{tt_s} | {pct_s} |"
                )
        elif key == "orphan_services":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Environment Name |",
                "| --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(list(block.get("rows") or [])):
                lines.append(
                    f"| {_md_esc(str(r.get('color', 'Yellow')))} | {_md_esc(r['service'])} | "
                    f"{_md_esc(r['environment'])} |"
                )
        elif key == "minimal_spans":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Sample size | % traces 1–2 spans | % traces 3–5 spans |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(list(block.get("rows") or [])):
                ss = r.get("sample_size", "—")
                ss_s = "—" if ss == "—" else fmt_table_number(ss)
                p12 = r.get("pct_1_2_spans", "—")
                p12_s = "—" if p12 == "—" else fmt_table_number(p12, decimals=1, percent=True)
                p35 = r.get("pct_3_5_spans", "—")
                p35_s = "—" if p35 == "—" else fmt_table_number(p35, decimals=1, percent=True)
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(r['service'])} | "
                    f"{ss_s} | {p12_s} | {p35_s} |"
                )
        elif key == "large_span_sizes":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Environment Name | Operation Name | Est. bytes |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(
                list(block.get("rows") or []), within_tier=_within_large_span_est_bytes
            ):
                eb = r.get("est_bytes", "—")
                eb_s = "—" if eb == "—" else fmt_table_number(eb)
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(str(r.get('serviceName', '—')))} | "
                    f"{_md_esc(str(r.get('environment', '—')))} | {_md_esc(str(r.get('operationName', '—')))} | "
                    f"{eb_s} |"
                )
        elif key == "tags_high_cardinality":
            lines += [
                "### Results",
                "",
                "| Color | Tag Name | Unique Values |",
                "| --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(
                list(block.get("rows") or []), within_tier=_within_tag_unique_values
            ):
                uv = r.get("charCount", "—")
                uv_s = "—" if uv == "—" else fmt_table_number(uv)
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(str(r.get('tagName', '—')))} | "
                    f"{uv_s} |"
                )
        elif key == "sensitive_data":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Span Name |",
                "| --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(list(block.get("rows") or [])):
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(str(r.get('serviceName', '—')))} | "
                    f"{_md_esc(str(r.get('spanName', '—')))} |"
                )
        elif key == "debug_spans":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Environment Name | Operation Name |",
                "| --- | --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(
                list(block.get("rows") or []), within_tier=_within_debug_svc_env_op
            ):
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(str(r.get('serviceName', '—')))} | "
                    f"{_md_esc(str(r.get('environment', '—')))} | "
                    f"{_md_esc(str(r.get('operationName', '—')))} |"
                )
        elif key == "endpoint_grouping":
            lines += [
                "### Results",
                "",
                "| Color | Service Name | Environment Name | Endpoint Name |",
                "| --- | --- | --- | --- |",
            ]
            for r in _sort_rows_by_health_color(list(block.get("rows") or [])):
                ep = str(r.get("endpointName") or "—")
                note = r.get("note")
                if note and str(note).strip():
                    ep = f"{ep} — {note}"
                lines.append(
                    f"| {_md_esc(str(r.get('color', '—')))} | {_md_esc(str(r.get('serviceName', '—')))} | "
                    f"{_md_esc(str(r.get('environment', '—')))} | {_md_esc(ep)} |"
                )
        else:
            lines += ["### Results", "", "(no table)", ""]

        lines += ["", "### Recommendation", ""]
        recs = block.get("recommendations") or []
        if recs:
            for rec in recs:
                lines.append(f"- {_md_esc(str(rec))}")
        else:
            lines.append("*None.*")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_markdown(report: dict[str, Any], *, title: str) -> str:
    meta = {
        "o11y_apm_health_check_schema": int(report.get("schema") or 1),
        "realm": report["realm"],
        "window_hours": report["window_hours"],
        "generated_utc": report["generated_utc"],
    }
    fm = ["---"]
    for k, v in meta.items():
        fm.append(f"{k}: {json.dumps(v)}")
    fm.append("---")

    lines: list[str] = [
        *fm,
        "",
        f"# {title}",
        "",
        "> Internal APM snapshot (token-bound). For customer deliverables follow `AGENTS.md` "
        "(outcomes only; omit tooling and program details).",
        "",
        f"Assessment window: **{report['window_hours']}h** ending at report generation time.",
        "",
    ]

    lines.append(render_apm_checks_markdown(report["checks"], heading_prefix="##"))

    lines += [
        "## Machine-readable payload",
        "",
        "```json",
        json.dumps(report, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def parse_checks_arg(s: str, *, trace_checks: bool) -> set[str]:
    s = (s or "all").strip().lower()
    if s == "all":
        keys = {
            "health_endpoints",
            "usage_by_service",
            "orphan_services",
            "tags_high_cardinality",
            "sensitive_data",
            "debug_spans",
            "endpoint_grouping",
        }
        if trace_checks:
            keys.add("minimal_spans")
            keys.add("large_span_sizes")
        return keys
    out: set[str] = set()
    for x in s.split(","):
        k = x.strip()
        if not k:
            continue
        if k == "tag_bloat":
            out.add("large_span_sizes")
        else:
            out.add(k)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Splunk Observability APM health checks (script).")
    p.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default 24)")
    p.add_argument(
        "--signalflow-max-data-points",
        type=int,
        default=300_000,
        metavar="N",
        help="Max datapoints to read from each spans.count / traces.count SignalFlow stream (default 300000). "
        "Lower values risk **truncated** rollups and empty Health Endpoints rows in large orgs.",
    )
    p.add_argument(
        "--signalflow-wall-seconds",
        type=float,
        default=30.0,
        metavar="SEC",
        help="Max wall-clock seconds to stream each SignalFlow query (default 30). "
        "Lower = faster but may truncate rollups for very large orgs.",
    )
    p.add_argument("--realm", default=None, help="Realm (default: profile or SPLUNK_REALM or us0)")
    p.add_argument("--profile", default=None, metavar="PATH", help="YAML profile (see license script)")
    p.add_argument(
        "--checks",
        default="all",
        help=f"Comma-separated subset or 'all'. Known: {', '.join(CHECK_KEYS)}",
    )
    p.add_argument(
        "--trace-checks",
        action="store_true",
        help="With --checks all, also run trace-based checks: minimal_spans and large_span_sizes "
        "(or pass those names explicitly in --checks).",
    )
    p.add_argument(
        "--minimal-spans-services",
        type=int,
        default=20,
        help="Top N services by **traces.count** volume (rollup) to sample for minimal_spans when that "
        "metric is queried; otherwise by non-health span volume from spans.count (default 20)",
    )
    p.add_argument(
        "--trace-limit",
        type=int,
        default=20,
        help="Target number of trace examples per service used for minimal_spans stats (default 20)",
    )
    p.add_argument(
        "--trace-fetch-limit",
        type=int,
        default=None,
        metavar="N",
        help="How many trace examples to request per service from Trace Analytics (default: same as "
        "--trace-limit, or min(100, max(limit, 2*limit)) when health exclusion is on)",
    )
    p.add_argument(
        "--minimal-spans-include-health-traces",
        action="store_true",
        help="Include health-like operations in top-service ranking and in minimal-span samples "
        "(default: exclude so jobs focus on non-health traffic).",
    )
    p.add_argument(
        "--minimal-spans-max-full-traces",
        type=int,
        default=400,
        metavar="N",
        help="Global cap on successful get_trace_full loads used for minimal_spans (default 400)",
    )
    p.add_argument(
        "--minimal-spans-fetch-sleep-s",
        type=float,
        default=0.1,
        metavar="SEC",
        help="Delay between get_trace_full calls for minimal_spans (default 0.1)",
    )
    p.add_argument(
        "--span-size-services",
        type=int,
        default=0,
        help="How many top services to sample (by spans.count volume). **0** = dynamic: "
        "min(5, ceil(10%% of distinct services)) capped by org size (default 0).",
    )
    p.add_argument(
        "--span-size-traces-per-endpoint",
        type=int,
        default=3,
        help="Target trace examples per (service, sf_operation) pair from Trace Analytics (default 3)",
    )
    p.add_argument(
        "--span-size-max-operations-per-service",
        type=int,
        default=40,
        help="Max distinct endpoints (sf_operation) per sampled service, ranked by rollup volume "
        "(default 40; 0 = no limit — can be expensive)",
    )
    p.add_argument(
        "--span-size-max-traces",
        type=int,
        default=200,
        help="Global cap on unique traces to fetch with get_trace_full for large_span_sizes (default 200)",
    )
    p.add_argument(
        "--span-size-include-health-ranking",
        action="store_true",
        help="Include health-like operations when ranking services for large_span_sizes sampling.",
    )
    p.add_argument(
        "--span-size-fetch-sleep-s",
        type=float,
        default=0.1,
        help="Delay between trace API calls to reduce throttling (default 0.1)",
    )
    p.add_argument(
        "--endpoint-grouping-distinct-ops-yellow",
        type=int,
        default=80,
        metavar="N",
        help="endpoint_grouping: Yellow when a service has at least N distinct sf_operation values "
        "in the rollup (default 80)",
    )
    p.add_argument(
        "--endpoint-grouping-max-samples",
        type=int,
        default=40,
        metavar="N",
        help="endpoint_grouping: max ID-like sf_operation samples to list (default 40)",
    )
    p.add_argument("--md-out", default=None, help="Write markdown report path")
    p.add_argument("--json-out", default=None, help="Write JSON report path")
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--report-title", default=None, help="Title for markdown H1")
    p.add_argument(
        "--subscription-usage-timestamp-ms",
        default=None,
        metavar="MS",
        help="Epoch ms string for GetSubscriptionUsageTags (Tags with High Cardinality). "
        "Default: hour-aligned end of the analysis window.",
    )
    p.add_argument(
        "--extra-health-operation-substrings",
        default="",
        metavar="CSV",
        help="Comma/semicolon-separated extra substrings for Health Endpoints detection; merged with "
        "baseline list and profile key health_check_apm_extra_health_operation_substrings.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("APM health check starting (window=%s h)", args.hours)

    profile_path = resolve_profile_path(args.profile)
    profile: dict[str, str] = {}
    if profile_path:
        if not os.path.isfile(profile_path):
            logger.error("Profile not found: %s", profile_path)
            return 1
        profile = load_customer_profile_scalars(profile_path)

    merged_hp = merge_health_operation_patterns(
        _DEFAULT_HEALTH_OPERATION_SUBSTRINGS,
        profile.get("health_check_apm_extra_health_operation_substrings") or "",
        args.extra_health_operation_substrings or "",
    )
    set_active_health_operation_patterns(merged_hp)
    if merged_hp != _DEFAULT_HEALTH_OPERATION_SUBSTRINGS:
        logger.info(
            "Health endpoint matchers: merged %s substring pattern(s) (profile/CLI extras on top of baseline)",
            len(merged_hp),
        )

    try:
        return _apm_run_checks_after_profile_loaded(args, profile)
    finally:
        reset_active_health_operation_patterns()


def _apm_run_checks_after_profile_loaded(
    args: argparse.Namespace,
    profile: dict[str, str],
) -> int:
        token = (
            os.environ.get("SPLUNK_ACCESS_TOKEN")
            or profile.get("access_token")
            or profile.get("ACCESS_TOKEN")
            or ""
        ).strip()
        if not token:
            logger.error(
                "Set SPLUNK_ACCESS_TOKEN or access_token in profile (see customer-profile.example.yaml)."
            )
            return 1

        realm = (
            args.realm
            or profile.get("realm")
            or os.environ.get("SPLUNK_REALM")
            or "us0"
        ).strip()
        api_base = f"https://api.{realm}.signalfx.com"
        app_base = f"https://app.{realm}.signalfx.com"
        stream_base = f"https://stream.{realm}.signalfx.com"

        logger.info(
            "Realm: %s | checks=%s | trace_checks=%s",
            realm,
            args.checks,
            getattr(args, "trace_checks", False),
        )

        stop_ms = int(time.time() * 1000)
        start_ms = stop_ms - max(1, args.hours) * 3600 * 1000
        time_iso = iso_range_utc(start_ms, stop_ms)

        want = parse_checks_arg(args.checks, trace_checks=args.trace_checks)

        checks_out: dict[str, Any] = {}

        rollup: dict[tuple[str, str, str], float] = {}
        traces_rollup: dict[tuple[str, str], float] | None = None
        topology: dict[str, Any] = {}
        service_universe: set[str] = set()

        need_sf = bool(
            want
            & {
                "health_endpoints",
                "usage_by_service",
                "minimal_spans",
                "large_span_sizes",
                "endpoint_grouping",
                "sensitive_data",
                "debug_spans",
            }
        )
        need_topology = bool(want & {"orphan_services", "health_endpoints", "usage_by_service"})

        sf_max_pts = max(5_000, int(args.signalflow_max_data_points))
        spans_sf_stop_reason: str | None = None

        need_traces_rollup = bool(want & {"usage_by_service", "minimal_spans"})
        sf_wall = float(args.signalflow_wall_seconds)

        # Run SignalFlow queries + topology POST concurrently — all three are independent.
        spans_program = (
            "data('spans.count').sum(by=['sf_service','sf_operation','sf_environment'])"
            ".publish(label='apm')"
            if need_sf else None
        )
        traces_program = (
            "data('traces.count').sum(by=['sf_service','sf_environment']).publish(label='tr')"
            if (need_sf and need_traces_rollup) else None
        )

        def _run_spans_sf() -> tuple:
            return signalflow_matrix_collect(
                stream_url=stream_base,
                token=token,
                program=spans_program,
                start_ms=start_ms,
                stop_ms=stop_ms,
                resolution_ms=APM_SIGNALFLOW_RESOLUTION_MS,
                wall_seconds=sf_wall,
                max_data_points=sf_max_pts,
            )

        def _run_traces_sf() -> tuple:
            return signalflow_matrix_collect(
                stream_url=stream_base,
                token=token,
                program=traces_program,
                start_ms=start_ms,
                stop_ms=stop_ms,
                resolution_ms=APM_SIGNALFLOW_RESOLUTION_MS,
                wall_seconds=min(sf_wall, 45.0),
                max_data_points=sf_max_pts,
            )

        def _run_topology() -> dict[str, Any]:
            return topology_post(api_base, token, time_iso)

        init_workers = (
            (1 if need_sf else 0)
            + (1 if (need_sf and need_traces_rollup) else 0)
            + (1 if need_topology else 0)
        )
        spans_sf_result = traces_sf_result = topology_result = topology_error = None
        if init_workers > 0:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, init_workers)) as init_pool:
                spans_fut = init_pool.submit(_run_spans_sf) if need_sf else None
                traces_fut = init_pool.submit(_run_traces_sf) if (need_sf and need_traces_rollup) else None
                topo_fut = init_pool.submit(_run_topology) if need_topology else None
                if spans_fut:
                    spans_sf_result = spans_fut.result()
                if traces_fut:
                    traces_sf_result = traces_fut.result()
                if topo_fut:
                    try:
                        topology_result = topo_fut.result()
                    except RuntimeError as e:
                        topology_error = str(e)

        if need_sf and spans_sf_result is not None:
            meta, dps, err, sr = spans_sf_result
            spans_sf_stop_reason = sr
            if err:
                checks_out["_signalflow_error"] = {
                    "error": err,
                    "stop_reason": sr,
                    "note": "Downstream checks that need spans.count may be empty.",
                }
            else:
                rollup = rollup_spans_by_dims(meta, dps)
                for svc, _op, _env in rollup:
                    if svc:
                        service_universe.add(svc)
                if sr == "max_data_points":
                    logger.warning(
                        "spans.count SignalFlow stopped at max_data_points=%s — rollup may omit low-volume endpoints",
                        sf_max_pts,
                    )

        # traces.count rollup is used by usage_by_service and (when present) to rank minimal_spans sampling.
        if need_traces_rollup and traces_sf_result is not None:
            tmeta, tdps, terr, _tsr = traces_sf_result
            if not terr:
                tr = rollup_spans_by_dims(tmeta, tdps)
                traces_rollup = defaultdict(float)
                for (svc, _op, env), val in tr.items():
                    if svc:
                        traces_rollup[(svc, env)] += val
                traces_rollup = dict(traces_rollup)
            if traces_rollup is not None and not traces_rollup:
                traces_rollup = None

        if need_topology:
            if topology_error is not None:
                topology = {"data": {"nodes": [], "edges": []}, "error": topology_error}
                checks_out["_topology_error"] = topology_error
            else:
                topology = topology_result or {}
            data = topology.get("data") or topology
            for n in data.get("nodes") or []:
                if isinstance(n, dict):
                    svc, _ = node_service_env(n)
                    if svc:
                        service_universe.add(svc)

        if "health_endpoints" in want:
            checks_out["health_endpoints"] = check_health_endpoints(
                rollup,
                app_base=app_base,
                token=token,
                start_ms=start_ms,
                stop_ms=stop_ms,
                rollup_stop_reason=spans_sf_stop_reason,
            )

        if "usage_by_service" in want:
            checks_out["usage_by_service"] = check_usage_by_service(rollup, traces_rollup)

        if "orphan_services" in want:
            checks_out["orphan_services"] = check_orphan_services(topology, service_universe)

        want_trace_unified = bool(want & TRACE_FULL_UNIFIED_CHECKS)
        need_minimal = "minimal_spans" in want
        need_stratified = bool(want & TRACE_STRATIFIED_CHECKS)

        unified_cache: dict[str, dict[str, Any]] = {}
        unified_fetch_findings: list[str] = []
        stopped_at_unified_cap = False
        unified_cap = 0
        precomputed_trace_ids: list[str] | None = None
        precomputed_collection_meta: dict[str, Any] | None = None
        plan_trace: dict[str, Any] | None = None
        service_to_minimal_ids: dict[str, list[str]] = {}
        minimal_collect_findings: list[str] = []
        top_services_minimal: list[str] = []
        minimal_stub_done = False
        trace_limit_for_minimal = max(1, args.trace_limit)
        minimal_include_health = bool(args.minimal_spans_include_health_traces)
        merged_ids: list[str] = []
        attach_fetch_err_to_minimal = False

        if want_trace_unified:
            unified_cap = compute_unified_full_trace_cap(
                need_minimal,
                need_stratified,
                max(1, args.minimal_spans_max_full_traces),
                max(1, args.span_size_max_traces),
            )
            sleep_u = max(float(args.minimal_spans_fetch_sleep_s), max(0.0, args.span_size_fetch_sleep_s))

            if need_stratified:
                plan_trace = resolve_stratified_service_plan(
                    rollup,
                    top_services=args.span_size_services,
                    exclude_health_ranking=not bool(args.span_size_include_health_ranking),
                )
                if plan_trace.get("ok"):
                    tid_list, coll_findings, ep_shortfall, search_calls, stopped_cap = (
                        collect_stratified_trace_ids(
                            app_base,
                            token,
                            start_ms,
                            stop_ms,
                            rollup,
                            plan_trace["svc_list"],
                            traces_per_endpoint=max(1, args.span_size_traces_per_endpoint),
                            max_operations_per_service=max(0, args.span_size_max_operations_per_service),
                            max_trace_fetches=max(1, args.span_size_max_traces),
                            exclude_health_ranking=not bool(args.span_size_include_health_ranking),
                            sleep_between_fetch_s=max(0.0, args.span_size_fetch_sleep_s),
                        )
                    )
                    precomputed_trace_ids = tid_list
                    precomputed_collection_meta = {
                        "endpoint_shortfall": ep_shortfall,
                        "search_calls": search_calls,
                        "stopped_by_cap": stopped_cap,
                        "collection_findings": coll_findings,
                    }

            if need_minimal:
                top_n_m = max(1, args.minimal_spans_services)
                top_services_minimal = top_services_by_trace_volume(
                    traces_rollup, top_n=top_n_m
                )
                if not top_services_minimal:
                    top_services_minimal = top_services_by_span_volume(
                        rollup, top_n=top_n_m, exclude_health_ops=not minimal_include_health
                    )
                if args.trace_fetch_limit is not None:
                    fetch_lim = max(trace_limit_for_minimal, args.trace_fetch_limit)
                elif not minimal_include_health:
                    fetch_lim = min(100, max(trace_limit_for_minimal, trace_limit_for_minimal * 2))
                else:
                    fetch_lim = trace_limit_for_minimal
                if not top_services_minimal and not minimal_include_health and rollup:
                    checks_out["minimal_spans"] = {
                        "rows": [],
                        "findings": [
                            "No services had non-health `spans.count` volume in the rollup — cannot pick "
                            "top services for sampling. Use `--minimal-spans-include-health-traces` if you "
                            "still want minimal-span analysis on health-heavy workloads.",
                        ],
                        "recommendations": [],
                    }
                    minimal_stub_done = True
                else:
                    service_to_minimal_ids, minimal_collect_findings = collect_minimal_spans_trace_ids(
                        app_base,
                        token,
                        start_ms,
                        stop_ms,
                        top_services_minimal,
                        fetch_limit=fetch_lim,
                        traces_per_service=trace_limit_for_minimal,
                        exclude_health=not minimal_include_health,
                    )

            merged_ids = merge_unified_fetch_order(
                list(precomputed_trace_ids or []),
                service_to_minimal_ids,
                top_services_minimal,
            )
            if merged_ids:
                unified_cache, unified_fetch_findings, stopped_at_unified_cap = fetch_full_traces_by_ids(
                    app_base,
                    token,
                    merged_ids,
                    max_successful=unified_cap,
                    sleep_between_fetch_s=sleep_u,
                )

            attach_fetch_err_to_minimal = bool(
                need_minimal
                and not minimal_stub_done
                and (minimal_collect_findings or unified_fetch_findings)
            )

            if need_minimal and not minimal_stub_done:
                mf = list(minimal_collect_findings)
                if attach_fetch_err_to_minimal:
                    mf.extend(unified_fetch_findings)
                checks_out["minimal_spans"] = analyze_minimal_spans_from_cache(
                    top_services_minimal,
                    traces_per_service=trace_limit_for_minimal,
                    exclude_health=not minimal_include_health,
                    service_to_ids=service_to_minimal_ids,
                    fetched_traces=unified_cache,
                    collection_findings=mf,
                    stopped_at_unified_cap=stopped_at_unified_cap,
                    unified_fetch_cap=unified_cap,
                )

        if "large_span_sizes" in want:
            max_ops = args.span_size_max_operations_per_service
            ls_findings_extra = (
                list(unified_fetch_findings)
                if want_trace_unified and unified_fetch_findings and not attach_fetch_err_to_minimal
                else []
            )
            ls = check_large_span_sizes(
                app_base,
                token,
                start_ms,
                stop_ms,
                rollup,
                top_services=args.span_size_services,
                traces_per_endpoint=max(1, args.span_size_traces_per_endpoint),
                max_operations_per_service=max(0, max_ops),
                max_trace_fetches=max(1, args.span_size_max_traces),
                exclude_health_ranking=not bool(args.span_size_include_health_ranking),
                sleep_between_fetch_s=max(0.0, args.span_size_fetch_sleep_s),
                precomputed_trace_ids=precomputed_trace_ids,
                precomputed_collection_meta=precomputed_collection_meta,
                fetched_traces=unified_cache if (want_trace_unified and merged_ids) else None,
            )
            if ls_findings_extra:
                ls["findings"] = ls_findings_extra + ls["findings"]
            checks_out["large_span_sizes"] = ls

        want_inspection = want & {"sensitive_data", "debug_spans"}
        if want_inspection:
            if precomputed_trace_ids is not None:
                insp_pack = run_trace_inspection_checks(
                    precomputed_trace_ids,
                    unified_cache,
                    max_traces=max(1, args.span_size_max_traces),
                )
                if "sensitive_data" in want:
                    checks_out["sensitive_data"] = insp_pack["sensitive_data"]
                if "debug_spans" in want:
                    checks_out["debug_spans"] = insp_pack["debug_spans"]
            else:
                fail_findings = list((plan_trace or {}).get("findings") or [])
                if not fail_findings:
                    fail_findings.append(
                        "No trace sample was available — there may be no eligible service traffic in this window."
                    )
                stub = {
                    "rows": [],
                    "findings": fail_findings,
                    "recommendations": [
                        "Widen the assessment window or check that APM ingest is flowing for this org."
                    ],
                }
                if "sensitive_data" in want:
                    checks_out["sensitive_data"] = {
                        **stub,
                        "recommendations": [
                            "Remove secrets from span tags; use redaction processors or log pipelines instead."
                        ],
                    }
                if "debug_spans" in want:
                    checks_out["debug_spans"] = {
                        **stub,
                        "recommendations": [
                            "Disable verbose debug spans in production; reduce log level in instrumentation."
                        ],
                    }

        if "endpoint_grouping" in want:
            checks_out["endpoint_grouping"] = check_endpoint_grouping(
                rollup,
                exclude_health_ops=not bool(args.span_size_include_health_ranking),
                high_distinct_ops_yellow=max(1, args.endpoint_grouping_distinct_ops_yellow),
                max_suspicious_samples=max(1, args.endpoint_grouping_max_samples),
            )

        subscription_ts_ms: int | None = None
        if "tags_high_cardinality" in want:
            if args.subscription_usage_timestamp_ms is not None:
                try:
                    subscription_ts_ms = int(str(args.subscription_usage_timestamp_ms).strip())
                except ValueError:
                    logger.error("--subscription-usage-timestamp-ms must be integer epoch milliseconds")
                    return 1
            else:
                subscription_ts_ms = default_subscription_usage_timestamp_ms(stop_ms)
            _sub_ts = str(subscription_ts_ms)

            def _fetch_sub_tags() -> dict[str, Any]:
                return apm_graphql_get_subscription_usage_tags(app_base, token, _sub_ts)

            def _fetch_get_tags() -> dict[str, Any]:
                return apm_graphql_get_tags(app_base, token)

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as tag_pool:
                    sub_fut = tag_pool.submit(_fetch_sub_tags)
                    gt_fut = tag_pool.submit(_fetch_get_tags)
                    raw_tags = sub_fut.result()
                    raw_get_tags: dict[str, Any] | None = None
                    get_tags_error: str | None = None
                    try:
                        raw_get_tags = gt_fut.result()
                    except RuntimeError as e:
                        get_tags_error = str(e)[:400]
                checks_out["tags_high_cardinality"] = check_tags_high_cardinality(
                    raw_tags,
                    timestamp_ms=subscription_ts_ms,
                    raw_get_tags=raw_get_tags,
                    get_tags_error=get_tags_error,
                )
            except RuntimeError as e:
                checks_out["tags_high_cardinality"] = {
                    "rows": [],
                    "findings": [f"Subscription tag usage could not be loaded: {str(e)[:400]}"],
                    "recommendations": [
                        "Confirm access to Subscription usage in the Splunk Observability UI and try again."
                    ],
                    "timestampMillis": str(subscription_ts_ms),
                }

        report_title = (
            args.report_title
            or profile.get("report_title")
            or "APM health check snapshot"
        ).strip()

        report: dict[str, Any] = {
            "schema": 1,
            "realm": realm,
            "window_hours": args.hours,
            "start_ms": start_ms,
            "stop_ms": stop_ms,
            "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checks": checks_out,
        }
        if subscription_ts_ms is not None:
            report["subscription_usage_tags_timestamp_ms"] = subscription_ts_ms

        if args.md_out:
            parent = os.path.dirname(args.md_out)
            if parent:
                os.makedirs(parent, exist_ok=True)
            md = build_markdown(report, title=report_title)
            with open(args.md_out, "w", encoding="utf-8") as f:
                f.write(md)
            logger.info("Wrote markdown: %s", args.md_out)

        if args.json_out:
            os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
            logger.info("Wrote JSON: %s", args.json_out)

        if args.print_json:
            print(json.dumps(report, indent=2))

        logger.info(
            "APM workload summary: rollup_keys=%d service_universe=%d spans_sf_stop=%s "
            "trace_unified=%s merged_trace_ids=%d full_traces_cached=%d minimal_top_services=%d",
            len(rollup),
            len(service_universe),
            spans_sf_stop_reason,
            want_trace_unified,
            len(merged_ids),
            len(unified_cache),
            len(top_services_minimal),
        )
        logger.info("APM health check complete (%s check block(s))", len(checks_out))
        return 0



if __name__ == "__main__":
    raise SystemExit(main())
