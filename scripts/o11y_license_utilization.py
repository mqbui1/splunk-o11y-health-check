#!/usr/bin/env python3
"""
Splunk Observability Cloud — license / entitlement utilization (health-check helper).

Sources of truth:
  - Splunk-Observability-Health-Check.md (License utilization bands: Green 40–85%, Yellow <40%,
    Orange >85–100%, Red >100%)
  - License_utilizations.md (sf.org.* subscription vs usage metric pairs)

Approach:
  - Standalone stdlib script (no MCP). Uses POST https://stream.{realm}.signalfx.com/v2/signalflow/execute;
    hourly (default **1h**; override ``--resolution-hours``) points are bucketed into **calendar months (UTC)** for tables.
    (same family of API as server.py / Splunk SignalFlow docs).
  - **Single organization** — the access token identifies one org; no multi-org filters.
  - **License utilization only** (no throttle/diagnostic sf.org metrics).
  - One SignalFlow program per metric: data('<metric>').mean().publish() (or entitlement-specific
    program) over a configurable window with **1h** execute resolution by default; Python aggregates points per calendar
    month. **APM trace volume** uses ``rollup='rate'``.scale(60) + ``mean()`` at **hourly** execute
    resolution; Python stores the **arithmetic mean of hourly point values** per UTC calendar month
    (``hourly_mean_month``; Chart Builder parity ~ ``Mean(monthly)``; × optional ``rate_integral_scale``).
    **Profiling ingest** (``sf.org.profiling.numMessageBytesReceived``) uses the **same** program and Python
    aggregation as **APM trace volume** (``rollup='rate'``.scale(60).``mean()`` + ``hourly_mean_month``); only the
    metric name differs.
    Most gauges use mean of points per month; **RUM sessions** and **Synthetics run counts** use
    ``data(...).sum()`` per bucket and **sum buckets per UTC month** (see ``usage_aggregate``).
    See LIMITATIONS in --help.
  - **Markdown output** includes **o11y-license-chart** JSON per entitlement (bars = usage, line = subscription);
    the HTML report viewer renders SVG charts after load.

Environment:
  SPLUNK_ACCESS_TOKEN  (required unless set in profile) — org API token; overrides profile file
  SPLUNK_REALM         (optional)  — used when --realm and profile omit realm; default us0

Profile (optional YAML):
  If --profile is omitted, the script loads the first file that exists:
    ./customer-profile.local.yaml, then ./customer-profile.yaml
  Recognized top-level keys: access_token or ACCESS_TOKEN, realm, report_title
  (see customer-profile.example.yaml). Same file can hold report/realm for agents.

Usage:
  python3 scripts/o11y_license_utilization.py --md-out reports/acme-license-snapshot.md   # default ~6 months
  python3 scripts/o11y_license_utilization.py --profile customer-profile.yaml --md-out reports/snap.md
  python3 scripts/o11y_license_utilization.py --days 90 --json-out report/license.json   # optional shorter window
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from o11y_report_format import fmt_bytes_as_mib, fmt_table_number
from o11y_script_logging import setup_script_logging

logger = logging.getLogger(__name__)

PROFILE_FILENAMES: tuple[str, ...] = ("customer-profile.local.yaml", "customer-profile.yaml")

# Profiling allowance math (License_utilizations.md) uses decimal MB multipliers (e.g. 10.24 MB/host).
BYTES_PER_MB: float = 1_000_000.0
# Tables / license charts label byte amounts as **MiB** (1 MiB = 1,048,576 B) to match Observability UI.
BYTES_PER_MIB: float = 1024.0 * 1024.0

# Infrastructure Monitoring: row order in reports (hosts → containers → shared custom metrics).
IM_ENTITLEMENT_KEY_ORDER: tuple[str, ...] = ("im_hosts", "im_containers", "im_custom_metrics")


def _strip_yaml_inline_comment(s: str) -> str:
    in_single = in_double = False
    for i, c in enumerate(s):
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            return s[:i].rstrip()
    return s


def _parse_yaml_scalar_line_value(rest: str) -> str:
    rest = rest.strip()
    rest = _strip_yaml_inline_comment(rest)
    if not rest:
        return ""
    q = rest[0]
    if q in "\"'":
        if len(rest) >= 2 and rest.endswith(q):
            return rest[1:-1]
        return rest[1:]
    return rest


def load_customer_profile_scalars(path: str) -> dict[str, str]:
    """Load top-level scalar keys from a simple YAML file (stdlib only; no nested maps)."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n\r")
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if line[:1] in " \t":
                continue
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            key = key.strip()
            if not key or key.startswith("#"):
                continue
            val = _parse_yaml_scalar_line_value(rest)
            if val != "":
                out[key] = val
    return out


def utc_start_of_day_ms(iso_date: str) -> int:
    """UTC midnight at the start of ``YYYY-MM-DD``."""
    d = datetime.strptime(iso_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def utc_end_of_day_ms_inclusive(iso_date: str) -> int:
    """Last millisecond of ``YYYY-MM-DD`` in UTC (inclusive calendar day)."""
    start = utc_start_of_day_ms(iso_date)
    return start + 24 * 3600 * 1000 - 1


def resolve_profile_path(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for name in PROFILE_FILENAMES:
        if os.path.isfile(name):
            return name
    return None


# ---------------------------------------------------------------------------
# Entitlement catalog (keep in sync with License_utilizations.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntitlementSpec:
    """One row for the License utilization Results table."""

    key: str
    product: str
    label: str
    subscription_metric: str | None
    usage_metric: str
    # Optional SignalFlow filter inside data(..., filter=...)
    filter_fragment: str | None = None
    # Full usage program (overrides filter_fragment / bare metric), must publish label='usage'
    usage_program: str | None = None
    notes: str | None = None
    # Tables / charts show byte amounts as **MiB** (values remain raw bytes in JSON).
    display_values_as_mb: bool = False
    # ``mean``: average of rollup values (gauges) per month.
    # ``sum``: sum every usage point in the query window (legacy / rare; prefer ``monthly_sum`` for ``delta()``).
    # ``monthly_sum``: sum usage points **within each UTC month** (RUM/Synthetics per-bucket totals); headline
    #    usage = mean of those monthly totals (matches Chart Builder monthly totals for RUM/Synthetics).
    # ``rate_integral``: each usage point is bytes **per second** (rate); sum(rate × bucket_seconds) per month.
    # ``rate_mean_calendar_month``: mean of rate points in each UTC month × **full calendar seconds** in that month
    #    × ``rate_integral_scale``.
    # ``platform_cycle_month``: Splunk ``mean(cycle='month',…)`` output; one point per month; see
    #    :func:`monthly_usage_from_platform_cycle_mean` (compare tooling / legacy).
    # ``hourly_mean_month``: mean of execute values (e.g. hourly) within each UTC calendar month
    #    (``apm_span_bytes`` after ``rollup='rate'``.scale(60).mean()).
    usage_aggregate: str = "mean"
    # Extra multiplier after SignalFlow (``apm_span_bytes``: ``hourly_mean_month`` / rate paths / legacy cycle).
    rate_integral_scale: float = 1.0
    # ``mean``: average subscription points per month (gauges). ``rate_integral``: subscription points
    # are B/s; sum(rate × bucket_seconds) per month to compare to integrated usage (rare — prefer ``mean``).
    subscription_aggregate: str = "mean"

    def program_usage(self) -> str:
        if self.usage_program:
            return self.usage_program
        if self.filter_fragment:
            return (
                f"data('{self.usage_metric}', filter={self.filter_fragment}).mean().publish(label='usage')"
            )
        return f"data('{self.usage_metric}').mean().publish(label='usage')"

    def program_subscription(self) -> str | None:
        if not self.subscription_metric:
            return None
        return f"data('{self.subscription_metric}').mean().publish(label='sub')"


DEFAULT_ENTITLEMENTS: tuple[EntitlementSpec, ...] = (
    # --- APM (License_utilizations.md) ---
    EntitlementSpec(
        "apm_hosts",
        "APM",
        "APM hosts (host model)",
        "sf.org.apm.subscription.hosts",
        "sf.org.apm.numHosts",
    ),
    EntitlementSpec(
        "apm_traces_tapm",
        "APM",
        "APM traces (TAPM model)",
        "sf.org.apm.subscription.traces",
        "sf.org.apm.numTracesReceived",
    ),
    EntitlementSpec(
        "apm_containers",
        "APM",
        "APM containers / serverless",
        "sf.org.apm.subscription.containers",
        "sf.org.apm.numContainers",
    ),
    EntitlementSpec(
        "apm_mms",
        "APM",
        "APM Monitoring MetricSets (MMS)",
        "sf.org.apm.subscription.monitoringMetricSets",
        "sf.org.apm.numMonitoringMetricSets",
    ),
    EntitlementSpec(
        "apm_tms",
        "APM",
        "APM Troubleshooting MetricSets (TMS)",
        "sf.org.apm.subscription.troubleshootingMetricSets",
        "sf.org.apm.numTroubleshootingMetricSets",
    ),
    EntitlementSpec(
        "apm_span_bytes",
        "APM",
        "APM trace volume",
        "sf.org.apm.subscription.spanBytes",
        "sf.org.apm.numSpanBytesReceived",
        display_values_as_mb=True,
        # Hourly buckets: B/min per point; Python = mean of points per UTC month (~ Chart Builder Mean(monthly)).
        usage_program=(
            "data('sf.org.apm.numSpanBytesReceived', rollup='rate').scale(60).mean().publish(label='usage')"
        ),
        usage_aggregate="hourly_mean_month",
        rate_integral_scale=1.0,
    ),
    EntitlementSpec(
        "apm_profiling_bytes",
        "APM",
        "Profiling ingest",
        None,
        "sf.org.profiling.numMessageBytesReceived",
        # Same SignalFlow + hourly_mean_month as apm_span_bytes; metric name only.
        usage_program=(
            "data('sf.org.profiling.numMessageBytesReceived', rollup='rate').scale(60).mean().publish(label='usage')"
        ),
        notes="Profiling Ingest Subscription is estimated, if there is an add-on in your contract for this entitlement this may be wrong.",
        display_values_as_mb=True,
        usage_aggregate="hourly_mean_month",
        rate_integral_scale=1.0,
    ),
    # --- IM: hosts → containers → custom metrics (shared by host & container models) ---
    EntitlementSpec(
        "im_hosts",
        "Infrastructure Monitoring",
        "IM hosts",
        "sf.org.subscription.hosts",
        "sf.org.numResourcesMonitored",
        filter_fragment="filter('resourceType', 'host')",
    ),
    EntitlementSpec(
        "im_containers",
        "Infrastructure Monitoring",
        "IM containers",
        "sf.org.subscription.containers",
        "sf.org.numResourcesMonitored",
        filter_fragment="filter('resourceType', 'container')",
    ),
    EntitlementSpec(
        "im_custom_metrics",
        "Infrastructure Monitoring",
        "Custom metrics (MTS-based)",
        "sf.org.subscription.customMetrics",
        "sf.org.numCustomMetrics",
    ),
    # --- RUM ---
    EntitlementSpec(
        "rum_sessions",
        "RUM",
        "RUM sessions (monthly model)",
        "sf.org.rum.subscription.sessionsPerMonth",
        "sf.org.rum.numSessions",
        usage_program="data('sf.org.rum.numSessions').sum().publish(label='usage')",
        usage_aggregate="monthly_sum",
        notes="Monthly usage = sum of bucket values in each **UTC** calendar month; global execute resolution (default **1h**). Kept as UTC to match Observability UI for most orgs (local-time months differ slightly).",
    ),
    EntitlementSpec(
        "rum_mms",
        "RUM",
        "RUM MMS",
        "sf.org.rum.limit.monitoringMetricSets",
        "sf.org.numRumMonitoringMetricSetMetrics",
    ),
    # --- Synthetics (License_utilizations.md: test_type filters) ---
    EntitlementSpec(
        "syn_browser_runs",
        "Synthetics",
        "Synthetic browser test runs",
        "sf.org.synthetics.subscription.browser_tests",
        "synthetics.run.count",
        filter_fragment="filter('test_type', 'browser')",
        usage_program=(
            "data('synthetics.run.count', filter=filter('test_type', 'browser')).sum().publish(label='usage')"
        ),
        usage_aggregate="monthly_sum",
    ),
    EntitlementSpec(
        "syn_api_runs",
        "Synthetics",
        "Synthetic API test runs",
        "sf.org.synthetics.subscription.api_tests",
        "synthetics.run.count",
        filter_fragment="filter('test_type', 'api')",
        usage_program=(
            "data('synthetics.run.count', filter=filter('test_type', 'api')).sum().publish(label='usage')"
        ),
        usage_aggregate="monthly_sum",
    ),
    EntitlementSpec(
        "syn_uptime_runs",
        "Synthetics",
        "Synthetic uptime test runs",
        "sf.org.synthetics.subscription.uptime_tests",
        "synthetics.run.count",
        usage_program=(
            "(data('synthetics.run.count', filter=filter('test_type', 'http')).sum() + "
            "data('synthetics.run.count', filter=filter('test_type', 'port')).sum())"
            ".publish(label='usage')"
        ),
        usage_aggregate="monthly_sum",
    ),
)


def _subscription_effective_for_model(r: dict[str, Any] | None) -> float:
    """
    Numeric subscription for APM model choice. Missing series, errors, or non-numeric → 0
    (no licensed capacity from that SignalFlow program).
    """
    if not r:
        return 0.0
    if r.get("subscription_error"):
        return 0.0
    s = r.get("subscription")
    if s is None:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def filter_rows_by_active_license_models(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Omit the APM **traces (TAPM)** entitlement when the org is clearly on the **host / container**
    subscription model (hosts and/or containers have licensed capacity; traces subscription is zero).
    Omit **APM hosts** when **TAPM traces** subscription is licensed and host subscription is zero.

    Uses the same window subscription values as the utilization tables; ``subscription_error`` is
    treated as zero so a failed traces subscription query still allows hiding TAPM when hosts/containers
    show capacity.
    """
    meta: dict[str, Any] = {"excluded_keys": [], "notes": []}
    by_key = {str(r.get("key")): r for r in rows}

    host = by_key.get("apm_hosts")
    tapm = by_key.get("apm_traces_tapm")
    cont = by_key.get("apm_containers")

    if not host or not tapm:
        return rows, meta

    h = _subscription_effective_for_model(host)
    t = _subscription_effective_for_model(tapm)
    c = _subscription_effective_for_model(cont) if cont else 0.0

    exclude_key: str | None = None
    # Host / serverless container side has subscription; traces (TAPM) side does not → hide TAPM row
    if (h > 0 or c > 0) and t == 0:
        exclude_key = "apm_traces_tapm"
    elif t > 0 and h == 0:
        exclude_key = "apm_hosts"

    if exclude_key:
        meta["excluded_keys"].append(exclude_key)
        return [r for r in rows if str(r.get("key")) != exclude_key], meta

    return rows, meta


def _sub_for_month(row: dict[str, Any] | None, month: str) -> float:
    """Monthly subscription cell from a row's ``monthly`` list (0 if missing)."""
    if not row:
        return 0.0
    for m in row.get("monthly") or []:
        if str(m.get("month")) == month:
            s = m.get("subscription")
            return 0.0 if s is None else float(s)
    return 0.0


def _usage_for_month(row: dict[str, Any] | None, month: str) -> float:
    if not row:
        return 0.0
    for m in row.get("monthly") or []:
        if str(m.get("month")) == month:
            u = m.get("usage")
            return 0.0 if u is None else float(u)
    return 0.0


def enrich_apm_profiling_subscription_mb(rows: list[dict[str, Any]]) -> None:
    """
    License_utilizations.md: profiling has no subscription metric; allowance in MB is derived from
    host model (hosts × MB factor from container/host ratio) or TAPM (traces × factor from MMS/traces ratio).
    Compare derived bytes to sf.org.profiling.numMessageBytesReceived per month.
    """
    by_key = {str(r.get("key")): r for r in rows}
    prof = by_key.get("apm_profiling_bytes")
    if not prof or prof.get("usage_error"):
        return

    hosts = by_key.get("apm_hosts")
    cont = by_key.get("apm_containers")
    tapm = by_key.get("apm_traces_tapm")
    mms = by_key.get("apm_mms")

    months: set[str] = set()
    for m in prof.get("monthly") or []:
        mo = str(m.get("month") or "").strip()
        if mo:
            months.add(mo)

    new_monthly: list[dict[str, Any]] = []
    for month in sorted(months, key=_month_sort_key_newest_first, reverse=True):
        h = _sub_for_month(hosts, month)
        c = _sub_for_month(cont, month)
        t = _sub_for_month(tapm, month)
        mm = _sub_for_month(mms, month)
        u = _usage_for_month(prof, month)

        if h > 0:
            ratio = c / h if h else 0.0
            host_tier = (
                "enterprise_allowance_path"
                if abs(ratio - 20.0) < 0.01
                else "standard_allowance_path"
            )
            sub_mb = h * (10.24 if host_tier == "enterprise_allowance_path" else 5.12)
            tapm_tier = None
            ratio_t = None
        else:
            ratio_t = mm / t if t else 0.0
            tapm_tier = (
                "enterprise_allowance_path"
                if abs(ratio_t - 10.0) < 0.01
                else "standard_allowance_path"
            )
            sub_mb = t * (0.00256 if tapm_tier == "enterprise_allowance_path" else 0.00128)
            host_tier = None
            ratio = None

        sub_bytes = float(sub_mb) * BYTES_PER_MB
        pct = round(100.0 * u / sub_bytes, 2) if sub_bytes > 0 else 0.0
        new_monthly.append(
            {
                "month": month,
                "usage": u,
                "subscription": sub_bytes,
                "utilization_pct": pct,
                "severity": severity(pct),
                "profiling_derived": (
                    {
                        "apm_model": "host",
                        "allowance_tier": host_tier,
                        "subscription_containers": c,
                        "subscription_hosts": h,
                        "containers_per_host": round(ratio, 6) if ratio is not None else None,
                    }
                    if h > 0
                    else {
                        "apm_model": "tapm",
                        "allowance_tier": tapm_tier,
                        "subscription_mms": mm,
                        "subscription_traces": t,
                        "mms_per_million_traces": round(ratio_t, 6) if ratio_t is not None else None,
                    }
                ),
            }
        )

    prof["monthly"] = new_monthly
    # Latest completed month (tables are newest-first) — explains 10.24 vs 5.12 MB/host without opening each row.
    latest_prof = new_monthly[0].get("profiling_derived") if new_monthly else None
    if isinstance(latest_prof, dict):
        prof["profiling_derived"] = latest_prof
    prof["profiling_subscription_derived"] = True
    subs = [float(m["subscription"]) for m in new_monthly]
    prof["subscription"] = float(statistics.mean(subs)) if subs else 0.0
    u_agg = prof.get("usage")
    u_f = 0.0 if u_agg is None else float(u_agg)
    s_agg = prof["subscription"]
    prof["utilization_pct"] = (
        round(100.0 * u_f / s_agg, 2) if s_agg and s_agg > 0 else 0.0
    )
    prof["severity"] = severity(prof["utilization_pct"])


# ---------------------------------------------------------------------------
# SignalFlow HTTP execute (minimal SSE parser; org metrics + END_OF_CHANNEL)
# ---------------------------------------------------------------------------


def _qs(params: dict) -> str:
    filtered = {k: str(v) for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(filtered)) if filtered else ""


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


def _ts_ms_from_point(pt: dict, batch_ts: int | None) -> int | None:
    if batch_ts is not None and isinstance(batch_ts, (int, float)):
        return int(batch_ts)
    for k in ("logicalTimestampMs", "timestampMs", "timestamp"):
        x = pt.get(k)
        if not isinstance(x, (int, float)):
            continue
        xi = int(x)
        if xi < 10**11:
            xi *= 1000
        return xi
    return None


def execute_signalflow_time_series(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 120.0,
    read_timeout: float = 60.0,
    max_points: int = 5000,
) -> tuple[list[tuple[int, float]], str | None]:
    """
    Run one SignalFlow program; return (timestamp_ms, value) points (all series merged), sorted by time.
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
    points: list[tuple[int, float]] = []
    err: str | None = None
    t0 = time.monotonic()
    raw_bytes = 0

    def _ingest_data_msg(msg: dict) -> None:
        nonlocal err
        batch_ts = msg.get("logicalTimestampMs")
        if batch_ts is not None and not isinstance(batch_ts, (int, float)):
            batch_ts = None
        if batch_ts is not None:
            batch_ts = int(batch_ts)
        for pt in msg.get("data") or []:
            v = pt.get("value")
            if v is None or not isinstance(v, (int, float)):
                continue
            ts = _ts_ms_from_point(pt, batch_ts)
            if ts is None:
                continue
            points.append((ts, float(v)))
            if len(points) >= max_points:
                err = "max_points"
                return

    try:
        with urllib.request.urlopen(req, timeout=read_timeout + 30) as resp:
            block: list[str] = []
            while True:
                if time.monotonic() - t0 > wall_seconds:
                    err = "wall_timeout"
                    break
                line_b = resp.readline()
                if not line_b:
                    break
                raw_bytes += len(line_b)
                if raw_bytes > 16 * 1024 * 1024:
                    err = "max_response_bytes"
                    break
                line = line_b.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if block:
                        ev, msg = _parse_sse_block(block)
                        block = []
                        kind = _payload_kind(ev, msg)
                        if kind == "error":
                            err = json.dumps(msg)[:500]
                            break
                        if kind and kind != "data":
                            sr = _stop_from_event(msg.get("event"))
                            if sr == "end":
                                break
                        if kind == "data":
                            _ingest_data_msg(msg)
                            if err:
                                break
                    continue
                block.append(line)
            if block and err is None:
                ev, msg = _parse_sse_block(block)
                if _payload_kind(ev, msg) == "data":
                    _ingest_data_msg(msg)
    except TimeoutError:
        err = "socket_timeout"
    except urllib.error.HTTPError as e:
        body = (e.read() or b"")[:2048].decode("utf-8", errors="replace")
        err = f"http_{e.code}:{body}"
    except urllib.error.URLError as e:
        err = f"url_error:{e.reason!s}"

    points.sort(key=lambda x: x[0])
    return points, err


def _license_signalflow_client_limits(start_ms: int, stop_ms: int) -> tuple[float, float, int]:
    """Scale SSE read patience with window length so long spans are not truncated (wall_timeout)."""
    span_ms = max(0, int(stop_ms) - int(start_ms))
    days = max(1.0, span_ms / 86_400_000.0)
    wall = min(900.0, max(120.0, 90.0 + days * 2.0))
    read_to = min(720.0, max(75.0, 60.0 + days * 1.5))
    max_pts = min(250_000, max(8_000, int(days * 140) + 4_000))
    return wall, read_to, max_pts


def execute_signalflow_values(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 120.0,
    read_timeout: float = 60.0,
    max_points: int = 5000,
) -> tuple[list[float], str | None]:
    """Run one SignalFlow program; return numeric values only (same merge semantics as time series)."""
    points, err = execute_signalflow_time_series(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=wall_seconds,
        read_timeout=read_timeout,
        max_points=max_points,
    )
    return [v for _, v in points], err


def execute_signalflow_matrix(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 120.0,
    read_timeout: float = 90.0,
    max_body_bytes: int = 16 * 1024 * 1024,
    max_data_points: int = 100_000,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str | None, str | None]:
    """
    Stream SignalFlow execute; return ``(metadata_by_tsid, data_points, error, stop_reason)``.
    Each data point: ``tsId``, ``value``, ``timestampMs`` (logical batch when present).
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

    try:
        with urllib.request.urlopen(req, timeout=read_timeout + 30) as resp:
            block: list[str] = []
            while True:
                if time.monotonic() - t0 > wall_seconds:
                    stop_reason = stop_reason or "wall_timeout"
                    break
                line_b = resp.readline()
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


def month_key_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def month_key_for_splunk_mean_monthly_plot(ts_ms: int) -> str:
    """
    Map a ``Mean(monthly)`` / ``mean(cycle='month',…)`` output timestamp to the **calendar month the value describes**.

    In Splunk Observability Chart Builder, the monthly bar is often drawn at **UTC 00:00 on the 1st** of a month,
    and that value summarizes the **previous** full calendar month (e.g. 1 May 00:00 → April).
    """
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    # First hour of the 1st tolerates sub-second noise and 1h rollup buckets on month boundaries.
    if dt.day == 1 and dt.hour == 0:
        y, m = dt.year, dt.month
        if m == 1:
            return f"{y - 1}-12"
        return f"{y}-{m - 1:02d}"
    return month_key_utc(ts_ms)


def extend_stop_ms_for_splunk_monthly_cycle(stop_ms: int) -> int:
    """Push usage query stop past window end so Execute includes the next month-start boundary point."""
    return int(stop_ms + 3 * 24 * 3600 * 1000)


def _profile_positive_hours_first(
    profile: dict[str, str],
    keys: tuple[str, ...],
    *,
    default: int = 1,
) -> int:
    for k in keys:
        raw = (profile.get(k) or "").strip()
        if not raw:
            continue
        try:
            return max(1, int(float(raw)))
        except ValueError:
            logger.warning("Invalid %s=%r — trying next / default %s", k, raw, default)
    return default


def apm_span_usage_hourly_resolution_ms(profile: dict[str, str]) -> int:
    """Execute resolution for ``apm_span_bytes`` hourly mean path (hours); default 1h."""
    hours = _profile_positive_hours_first(
        profile,
        (
            "license_apm_span_bytes_usage_resolution_hours",
            "license_apm_span_bytes_cycle_resolution_hours",
        ),
    )
    return int(hours * 3600 * 1000)


def apm_span_platform_cycle_resolution_ms(profile: dict[str, str]) -> int:
    """Execute resolution for platform ``mean(cycle='month',…)`` comparisons (``compare_apm_span_bytes_methods``)."""
    return apm_span_usage_hourly_resolution_ms(profile)


def monthly_usage_from_platform_cycle_mean(
    points: list[tuple[int, float]],
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    """
    One published value per completed calendar month (after ``mean(cycle='month',…)``).

    Values are **not** integrated with bucket width — multiply only by ``scale`` (profile/CLI). Month keys use
    :func:`month_key_for_splunk_mean_monthly_plot`.
    """
    s = float(scale) if scale == scale and scale else 1.0
    deduped = _dedupe_ts_mean_points(points)
    out: dict[str, float] = {}
    for ts_ms, v in deduped:
        m = month_key_for_splunk_mean_monthly_plot(ts_ms)
        if m in out:
            logger.warning("platform_cycle_month: duplicate month %s — overwriting", m)
        out[m] = float(v) * s
    return dict(sorted(out.items()))


def _seconds_in_utc_month(month_label: str) -> int:
    """Seconds in a full UTC calendar month (YYYY-MM)."""
    s = (month_label or "").strip()
    parts = [p for p in s.replace("/", "-").split("-") if p]
    if len(parts) < 2:
        raise ValueError(f"Invalid month label: {month_label!r}")
    y, mo = int(parts[0]), int(parts[1])
    if mo < 1 or mo > 12:
        raise ValueError(f"Invalid month label: {month_label!r}")
    days = calendar.monthrange(y, mo)[1]
    return int(days * 24 * 3600)


def _dedupe_ts_mean_points(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """One value per timestamp (mean) when execute merges multiple series at the same logical time."""
    buckets: dict[int, list[float]] = {}
    for ts_ms, v in points:
        buckets.setdefault(int(ts_ms), []).append(float(v))
    return sorted((ts, statistics.mean(vs)) for ts, vs in buckets.items())


def monthly_mean_by_month(points: list[tuple[int, float]]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for ts_ms, v in points:
        m = month_key_utc(ts_ms)
        buckets.setdefault(m, []).append(v)
    return {m: float(statistics.mean(vs)) for m, vs in sorted(buckets.items())}


def monthly_mean_of_point_values_by_utc_month(
    points: list[tuple[int, float]],
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    """
    Arithmetic **mean of execute values** per UTC calendar month (no ×Δt, no integration).

    Use when comparing to Chart Builder **Mean(monthly)**-style summaries on the same unit scale as each point.
    ``scale`` multiplies each monthly mean (e.g. ``rate_integral_scale`` for ``apm_span_bytes``).
    """
    s = float(scale) if scale == scale and scale else 1.0
    deduped = _dedupe_ts_mean_points(points)
    buckets: dict[str, list[float]] = {}
    for ts_ms, v in deduped:
        m = month_key_utc(ts_ms)
        buckets.setdefault(m, []).append(float(v))
    return {mo: float(statistics.mean(vs)) * s for mo, vs in sorted(buckets.items())}


def monthly_bytes_from_rate_points(
    points: list[tuple[int, float]],
    resolution_ms: int,
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    """
    Integrate **bytes per second** over each resolution bucket to **bytes per calendar month**.

    Each SignalFlow point is averaged over ``resolution_ms``; contribution is
    ``value × scale × (resolution_ms / 1000)`` bytes. Values are summed per UTC month.
    For ``apm_span_bytes``, the program applies ``rollup='rate'`` and ``scale(60)``; ``scale`` here defaults to **1**
    (profile/CLI override only when a verified extra factor applies).
    """
    if resolution_ms <= 0:
        raise ValueError("resolution_ms must be positive")
    sec_per_bucket = resolution_ms / 1000.0
    s = float(scale) if scale == scale and scale else 1.0
    buckets: dict[str, float] = {}
    for ts_ms, v in points:
        m = month_key_utc(ts_ms)
        buckets[m] = buckets.get(m, 0.0) + float(v) * s * sec_per_bucket
    return {mo: buckets[mo] for mo in sorted(buckets.keys())}


def monthly_bytes_from_mean_rate_calendar_month(
    points: list[tuple[int, float]],
    *,
    scale: float = 1.0,
) -> dict[str, float]:
    """
    APM-style monthly trace bytes: **mean** of rate samples in each UTC month × **full calendar seconds**
    in that month × ``scale``.

    Aligns with “monthly average of snapshots” style billing: the mean is taken over all rollup buckets
    that fall in the month (after deduping duplicate timestamps). ``scale`` is ``rate_integral_scale``
    (profile/CLI) for ``apm_span_bytes``.
    """
    s = float(scale) if scale == scale and scale else 1.0
    deduped = _dedupe_ts_mean_points(points)
    buckets: dict[str, list[float]] = {}
    for ts_ms, v in deduped:
        m = month_key_utc(ts_ms)
        buckets.setdefault(m, []).append(float(v))
    out: dict[str, float] = {}
    for m, vs in sorted(buckets.items()):
        sec = _seconds_in_utc_month(m)
        out[m] = statistics.mean(vs) * s * float(sec)
    return out


def entitlement_rate_integral_scale_effective(
    spec: EntitlementSpec,
    profile: dict[str, str],
    apm_span_bytes_cli_scale: float | None,
) -> float:
    """
    Multiplier for ``apm_span_bytes``: scales integrated rates (``rate_*``), multiplies
    ``hourly_mean_month`` monthly means, or legacy platform ``mean(cycle='month',…)`` values
    (``platform_cycle_month``).

    ``apm_span_bytes`` alone accepts profile ``license_apm_span_bytes_rate_integral_scale`` and
    ``--apm-span-bytes-rate-scale`` (CLI wins).
    """
    base = float(spec.rate_integral_scale)
    if spec.key != "apm_span_bytes":
        return base
    raw = (profile.get("license_apm_span_bytes_rate_integral_scale") or "").strip()
    if raw:
        try:
            base = float(raw)
        except ValueError:
            logger.warning(
                "Invalid license_apm_span_bytes_rate_integral_scale=%r — using entitlement default %s",
                raw,
                spec.rate_integral_scale,
            )
            base = float(spec.rate_integral_scale)
    if apm_span_bytes_cli_scale is not None:
        base = float(apm_span_bytes_cli_scale)
    return base


def monthly_sum_by_month(points: list[tuple[int, float]]) -> dict[str, float]:
    """Sum of values per calendar month (e.g. deltas from a cumulative counter)."""
    buckets: dict[str, list[float]] = {}
    for ts_ms, v in points:
        m = month_key_utc(ts_ms)
        buckets.setdefault(m, []).append(v)
    return {m: float(sum(vs)) for m, vs in sorted(buckets.items())}


def _month_tuple_for_sort(month_label: str) -> tuple[int, int]:
    """Parse YYYY-MM for chronological ordering; unparsable values sort last."""
    s = (month_label or "").strip()
    if not s:
        return (9999, 12)
    parts = [p for p in s.replace("/", "-").split("-") if p]
    if len(parts) >= 2:
        try:
            y = int(parts[0])
            mo = int(parts[1])
            if 1 <= mo <= 12 and 1900 <= y <= 3000:
                return (y, mo)
        except ValueError:
            pass
    return (9999, 12)


def _month_sort_key_newest_first(month_label: str) -> tuple[int, int]:
    """Sort key: valid YYYY-MM sorts by calendar order; unparsable → (-1,-1) so newest-first sorts list them last."""
    t = _month_tuple_for_sort(month_label)
    if t == (9999, 12):
        return (-1, -1)
    return t


def build_monthly_util_rows(
    usage_by_month: dict[str, float],
    subscription_by_month: dict[str, float] | None,
) -> list[dict[str, Any]]:
    sub_keys = set(subscription_by_month) if subscription_by_month is not None else set()
    all_months = set(usage_by_month) | sub_keys
    months = sorted(all_months, key=_month_sort_key_newest_first, reverse=True)
    out: list[dict[str, Any]] = []
    for m in months:
        u_raw = usage_by_month.get(m)
        s_raw = subscription_by_month.get(m) if subscription_by_month is not None else None
        u_out = 0.0 if u_raw is None else float(u_raw)
        if subscription_by_month is not None:
            s_out = 0.0 if s_raw is None else float(s_raw)
        else:
            s_out = 0.0
        if s_out > 0 and u_raw is not None:
            pct = round(100.0 * float(u_raw) / s_out, 2)
        else:
            pct = 0.0
        out.append(
            {
                "month": m,
                "usage": u_out,
                "subscription": s_out,
                "utilization_pct": pct,
                "severity": severity(pct),
            }
        )
    return out


def sort_monthly_rows_chronological(monthly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort monthly rows by calendar month descending (latest first); unparsable months last."""
    return sorted(
        monthly,
        key=lambda row: _month_sort_key_newest_first(str(row.get("month") or "")),
        reverse=True,
    )


def sort_monthly_rows_oldest_first(monthly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by calendar month ascending (oldest first) for time-series charts; unparsable months last."""

    def _key(row: dict[str, Any]) -> tuple[int, int]:
        return _month_tuple_for_sort(str(row.get("month") or ""))

    return sorted(monthly, key=_key)


def build_license_entitlement_utilization_chart_md(
    r: dict[str, Any],
    monthly_for_chart: list[dict[str, Any]],
) -> list[str]:
    """
    Markdown for one entitlement: JSON payload for the health-report viewer (SVG combo chart).

    The viewer draws **bars = usage**, **line = subscription** on one Y scale, utilization % above each bar.
    Raw markdown without the viewer still shows the JSON block (machine-readable).

    ``monthly_for_chart`` should be complete UTC months only, **oldest first** (see
    ``sort_monthly_rows_oldest_first``). Chart semantics are documented in the License utilization section
    preamble; no per-entitlement caption is emitted.
    """
    lines: list[str] = []
    if not monthly_for_chart:
        lines.append("*No monthly samples in lookback window.*")
        lines.append("")
        return lines

    use_mb = bool(r.get("display_values_as_mb") or r.get("display_values_as_mib"))

    points: list[dict[str, Any]] = []
    for m in monthly_for_chart:
        ym = str(m.get("month") or "").strip()
        if not ym:
            continue
        raw_pct = m.get("utilization_pct")
        pct_out: float | None
        if raw_pct is None:
            pct_out = None
        else:
            try:
                pct_out = float(raw_pct)
            except (TypeError, ValueError):
                pct_out = None
        sev = m.get("severity")
        sev_s = str(sev).strip() if sev is not None else None
        points.append(
            {
                "month": ym,
                "subscription": float(m.get("subscription") or 0.0),
                "usage": float(m.get("usage") or 0.0),
                "utilizationPct": pct_out,
                "severity": sev_s if sev_s in ("Green", "Yellow", "Orange", "Red") else None,
            }
        )

    if not points:
        lines.append("*No monthly samples in lookback window.*")
        lines.append("")
        return lines

    payload: dict[str, Any] = {
        "v": 1,
        "title": str(r.get("label") or r.get("key") or "Entitlement"),
        "yUnit": "mib" if use_mb else "count",
        "points": points,
    }
    lines.append("```o11y-license-chart")
    lines.append(json.dumps(payload, indent=2))
    lines.append("```")
    lines.append("")
    return lines


def aggregate_values(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.mean(values))


def aggregate_sum(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values))


def severity(util_pct: float | None) -> str | None:
    """License utilization bands (Splunk-Observability-Health-Check.md License utilization)."""
    if util_pct is None:
        return None
    u = float(util_pct)
    if u > 100:
        return "Red"
    if u > 85:
        return "Orange"
    if u >= 40:
        return "Green"
    return "Yellow"


def latest_complete_month_label_utc(stop_ms: int) -> str:
    """YYYY-MM of the last fully completed calendar month before ``stop_ms`` (UTC)."""
    dt = datetime.fromtimestamp(stop_ms / 1000.0, tz=timezone.utc)
    y, m = dt.year, dt.month
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def _calendar_month_from_iso_date(date_str: str) -> str | None:
    """``YYYY-MM-DD`` (or prefix) → ``YYYY-MM`` of that calendar month, or ``None`` if unparsable."""
    s = str(date_str).strip()
    if len(s) < 7:
        return None
    try:
        y = int(s[0:4])
        mo = int(s[5:7])
    except ValueError:
        return None
    if y < 1970 or mo < 1 or mo > 12:
        return None
    return f"{y:04d}-{mo:02d}"


def monthly_cap_for_tables(stop_ms: int, fixed_window_end_date: str | None) -> str:
    """
    Upper bound (YYYY-MM) for which monthly rows appear in tables/charts.

    Rolling window: last UTC calendar month fully before ``stop_ms`` (omit in-progress month).

    Fixed ``--start-date``/``--end-date`` window: cap is the **calendar month of the inclusive end date**,
    so e.g. a window ending ``2026-01-31`` still includes **2026-01** (``latest_complete_month_label_utc`` alone
    would incorrectly drop January when ``stop_ms`` is still in January).
    """
    if fixed_window_end_date and str(fixed_window_end_date).strip():
        cm = _calendar_month_from_iso_date(str(fixed_window_end_date).strip())
        if cm:
            return cm
    return latest_complete_month_label_utc(stop_ms)


def _month_label_at_or_before_calendar(month: str, cap: str) -> bool:
    """True if ``month`` is valid YYYY-MM and not after ``cap`` (both YYYY-MM, UTC)."""
    tm = _month_tuple_for_sort(month)
    tc = _month_tuple_for_sort(cap)
    if tm == (9999, 12):
        return False
    if tc == (9999, 12):
        return True
    return tm <= tc


def filter_monthly_to_complete_months(
    monthly: list[dict[str, Any]],
    stop_ms: int,
    *,
    fixed_window_end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Keep monthly rows up to table cap (see ``monthly_cap_for_tables``).

    Rolling window: omit the in-progress calendar month. Fixed start/end window: include through the
    end month of the inclusive ``fixed_window_end_date``.
    """
    cap = monthly_cap_for_tables(stop_ms, fixed_window_end_date)
    out = [
        row
        for row in monthly
        if _month_label_at_or_before_calendar(str(row.get("month") or "").strip(), cap)
    ]
    return sort_monthly_rows_chronological(out)


def recompute_row_aggregates_from_monthly(row: dict[str, Any]) -> None:
    """After monthly list changes, refresh window-level usage, subscription, utilization %."""
    if row.get("usage_error"):
        return
    monthly = row.get("monthly") or []
    if not monthly:
        if not row.get("usage_error"):
            row["usage"] = 0.0
        if not row.get("subscription_error"):
            row["subscription"] = 0.0
            row["utilization_pct"] = 0.0
            row["severity"] = severity(0.0)
        return

    us = [float(m["usage"]) for m in monthly]
    row["usage"] = float(statistics.mean(us))

    if row.get("subscription_error"):
        return

    ss = [float(m["subscription"]) for m in monthly]
    row["subscription"] = float(statistics.mean(ss))
    u, s = row["usage"], row["subscription"]
    row["utilization_pct"] = round(100.0 * u / s, 2) if s > 0 else 0.0
    row["severity"] = severity(row["utilization_pct"])


def apply_complete_month_filter_to_rows(
    rows: list[dict[str, Any]],
    stop_ms: int,
    *,
    fixed_window_end_date: str | None = None,
) -> None:
    for r in rows:
        if r.get("usage_error"):
            continue
        monthly = list(r.get("monthly") or [])
        r["monthly"] = filter_monthly_to_complete_months(
            monthly, stop_ms, fixed_window_end_date=fixed_window_end_date
        )
        recompute_row_aggregates_from_monthly(r)


def drop_im_hosts_if_zero_subscription(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hide IM hosts when licensed host count is zero (both host and container SKUs use custom metrics)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("key")) != "im_hosts":
            out.append(r)
            continue
        if r.get("subscription_error"):
            out.append(r)
            continue
        if _subscription_effective_for_model(r) == 0.0:
            continue
        out.append(r)
    return out


def row_has_positive_subscription_allowance(r: dict[str, Any]) -> bool:
    """
    True if this entitlement reflects a positive licensed subscription allowance.
    Used to omit entire product sections when the org has no capacity in that product
    (e.g. RUM not on the contract — all sf.org.* subscription series are zero or absent).
    """
    if r.get("profiling_subscription_derived"):
        return _subscription_effective_for_model(r) > 0.0
    if r.get("subscription_metric") is None:
        return False
    return _subscription_effective_for_model(r) > 0.0


def filter_license_rows_for_subscription_report(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Keep only rows whose product has at least one entitlement with subscription allowance > 0.
    Products with no licensed subscription anywhere (all zeros / errors) are omitted from reports.
    """
    by_product: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        p = str(r.get("product") or "").strip()
        if not p:
            continue
        by_product.setdefault(p, []).append(r)
    keep_products = {
        p
        for p, prs in by_product.items()
        if any(row_has_positive_subscription_allowance(x) for x in prs)
    }
    return [r for r in rows if str(r.get("product") or "").strip() in keep_products]


def sort_license_rows_for_product(rows: list[dict[str, Any]], product: str) -> list[dict[str, Any]]:
    if product != "Infrastructure Monitoring":
        return rows
    order = {k: i for i, k in enumerate(IM_ENTITLEMENT_KEY_ORDER)}

    def sk(r: dict[str, Any]) -> tuple[int, str]:
        k = str(r.get("key") or "")
        return (order.get(k, 99), k)

    return sorted(rows, key=sk)


def _md_escape_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _fmt_cell_num(x: float | None) -> str:
    """Subscription / utilization counts (non-byte entitlements)."""
    if x is None:
        return "—"
    return fmt_table_number(x)


def _fmt_cell_mb(x: float | None) -> str:
    """Byte amounts as MiB for tables (JSON remains bytes)."""
    if x is None:
        return "—"
    return fmt_bytes_as_mib(x, bytes_per_mib=BYTES_PER_MIB)


def _product_order(rows: list[dict[str, Any]]) -> list[str]:
    order: list[str] = []
    for r in rows:
        p = r["product"]
        if p not in order:
            order.append(p)
    return order


def format_license_utilization_window_markdown(report: dict[str, Any]) -> str:
    """Single italic line: assessment date range for license utilization (UTC calendar dates)."""
    fw = report.get("fixed_window_utc") if isinstance(report.get("fixed_window_utc"), dict) else {}
    fwa, fwb = fw.get("startDate"), fw.get("endDate")
    if fwa and fwb:
        return f"*License utilization from: **{fwa}** through **{fwb}**.*"
    start_ms = int(report.get("start_ms") or 0)
    stop_ms = int(report.get("stop_ms") or 0)
    if start_ms <= 0 or stop_ms <= 0:
        return "*License utilization from: *(see JSON `start_ms` / `stop_ms`).*"
    d0 = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(stop_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"*License utilization from: **{d0}** through **{d1}**.*"


def build_license_markdown(
    report: dict[str, Any],
    *,
    title: str,
) -> str:
    """
    Markdown snapshot for skills to merge into the customer health check (License utilization).

    Skills: read the JSON inside the ```json fence under ## Machine-readable payload,
    or transcribe the Results tables per AGENTS.md (outcomes only in final report).
    """
    schema = int(report.get("schema") or 1)
    meta = {
        "o11y_license_utilization_schema": schema,
        "single_org": True,
        "realm": report["realm"],
        "window_days": report["window_days"],
        "resolution_ms": report["resolution_ms"],
        "monthly_buckets_utc": True,
        "start_ms": report["start_ms"],
        "stop_ms": report["stop_ms"],
        "generated_utc": report["generated_utc"],
    }
    fm_lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, bool):
            fm_lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            fm_lines.append(f'{k}: "{v}"')
        else:
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    rows = report["rows"]
    rows_display = filter_license_rows_for_subscription_report(rows)
    lines: list[str] = [
        *fm_lines,
        "",
        f"# {title}",
        "",
        "> **Internal snapshot — single organization** (token-bound). Merge into the customer "
        "health check per `AGENTS.md`: **outcomes only** in the deliverable; do not copy methodology "
        "or this preamble into the customer file verbatim.",
        "",
        "Derived from `License_utilizations.md` metric pairs. **Charts** use **completed UTC months** "
        "(oldest → newest on the axis).",
        "",
        "## License utilization",
        "",
        format_license_utilization_window_markdown(report),
        "",
        "- **Bars** = usage by month, with utilization %.",
        "- **Line** = subscription allowance.",
        "",
        "### Severity legend",
        "",
        "| Color | Criteria |",
        "| --- | --- |",
        "| Green | Utilization from 40% to 85% |",
        "| Yellow | Utilization less than 40% |",
        "| Orange | Utilization greater than 85% and up to 100% |",
        "| Red | Utilization greater than 100% |",
        "",
        "### Monthly utilization by product (UTC)",
        "",
    ]

    if not rows_display and rows:
        lines += [
            "*No product families with a subscription allowance — all entitlements are omitted (unlicensed or "
            "no positive subscription in window).*",
            "",
        ]

    rows_by_product: dict[str, list[dict[str, Any]]] = {}
    for r in rows_display:
        rows_by_product.setdefault(r["product"], []).append(r)

    for product in _product_order(rows_display):
        lines += [f"### {_md_escape_cell(product)}", ""]
        for r in sort_license_rows_for_product(rows_by_product[product], product):
            lines.append(f"#### Entitlement: {_md_escape_cell(r['label'])}")
            if r.get("notes"):
                lines.append("")
                lines.append(f"*{_md_escape_cell(str(r['notes']))}*")
            lines.append("")

            if r.get("usage_error"):
                lines.append(f"*Usage series unavailable: `{_md_escape_cell(r['usage_error'][:240])}`*")
                lines.append("")
                continue

            stop_ms = int(report.get("stop_ms") or 0)
            fw = report.get("fixed_window_utc") if isinstance(report.get("fixed_window_utc"), dict) else {}
            fw_end = fw.get("endDate")
            fw_end_s = str(fw_end).strip() if fw_end else None
            monthly = filter_monthly_to_complete_months(
                list(r.get("monthly") or []),
                stop_ms if stop_ms > 0 else int(time.time() * 1000),
                fixed_window_end_date=fw_end_s,
            )
            monthly_chart = sort_monthly_rows_oldest_first(monthly)
            lines.extend(build_license_entitlement_utilization_chart_md(r, monthly_chart))

    lines += [
        "",
        "### Machine-readable payload (for skills)",
        "",
        "Parse the JSON below for automated merge into the health-check report.",
        "",
        "```json",
        json.dumps(report, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Compute Splunk Observability license utilization rows (sf.org.* via SignalFlow).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
LIMITATIONS (read before customer-facing claims):
  - Billing uses contract-specific averaging (e.g. monthly averages, 1-minute snapshots for APM).
    Window and monthly cells use mean of SignalFlow points (**1h** execute resolution by default) — a directional estimate.
  - **APM trace volume** (``sf.org.apm.numSpanBytesReceived``): ``rollup='rate'``.scale(60) + ``mean()`` at **hourly**
    execute resolution (profile: ``license_apm_span_bytes_usage_resolution_hours``, fallback
    ``license_apm_span_bytes_cycle_resolution_hours``); Python stores the **mean of hourly values** per UTC month
    (× ``rate_integral_scale``). Aligns closely with Chart Builder **Mean(monthly)**; use ``compare_apm_span_bytes_methods.py``
    to contrast platform cycle vs hourly mean.
  - Monthly tables list **completed UTC months only** (the in-progress month is omitted). JSON stores **raw bytes**;
    span/profiling **tables and charts** label amounts as **MiB** (bytes ÷ 1 048 576). Profiling allowance still uses
    decimal **MB** multipliers from ``License_utilizations.md`` internally (e.g. 10.24 MB/host × 1 000 000 → bytes).
  - Monthly buckets are **UTC calendar months**.
  - Some metrics may be absent on trials, partial SKUs, or renamed; rows show error/no data.
  - IM host vs container usage use sf.org.numResourcesMonitored with resourceType host vs container (License_utilizations.md).
  - **RUM** (``sf.org.rum.numSessions``) and **Synthetics** (``synthetics.run.count``) usage: org-wide
    ``sum()`` per rollup bucket, then **sum of buckets per UTC month** (not local timezone); default **1h**
    resolution usually matches the platform UI. Row headline usage is the mean of those monthly totals.
    Synthetics uptime combines ``http`` and ``port`` test types with ``sum()``.
  - **Profiling** (``sf.org.profiling.numMessageBytesReceived``): **same** usage program and ``hourly_mean_month``
    math as **APM trace volume** (``rollup='rate'``.scale(60).``mean()``); hourly execute resolution from
    ``license_apm_span_bytes_usage_resolution_hours`` (fallback ``license_apm_span_bytes_cycle_resolution_hours``).

References:
  - License_utilizations.md (metric names)
  - https://dev.splunk.com/observability/reference/api/signalflow/latest/
        """,
    )
    p.add_argument(
        "--days",
        type=int,
        default=180,
        help="Lookback window in days (default 180 ≈ 6 months); ignored if --calendar-month or start/end dates",
    )
    p.add_argument(
        "--calendar-month",
        default=None,
        metavar="YYYY-MM",
        help="Single full UTC calendar month for license usage (overrides --days and --start-date/--end-date)",
    )
    p.add_argument(
        "--start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC start of license window (inclusive); requires --end-date (ignored if --calendar-month is set)",
    )
    p.add_argument(
        "--end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC end of license window (inclusive); requires --start-date",
    )
    p.add_argument("--resolution-hours", type=int, default=1, help="SignalFlow resolution in hours (default 1)")
    p.add_argument(
        "--realm",
        default=None,
        help="Splunk realm (default: profile, then SPLUNK_REALM, then us0)",
    )
    p.add_argument(
        "--profile",
        default=None,
        metavar="PATH",
        help="Customer YAML profile (access_token, realm, report_title). "
        "If omitted, uses first existing of customer-profile.local.yaml or customer-profile.yaml in cwd.",
    )
    p.add_argument(
        "--md-out",
        type=str,
        default=None,
        help="Write markdown snapshot for skills (YAML front matter + tables + JSON block)",
    )
    p.add_argument(
        "--report-title",
        type=str,
        default=None,
        help="H1 title in --md-out file (default: profile, then 'License utilization snapshot')",
    )
    p.add_argument("--json-out", type=str, default=None, help="Optional: also write JSON only to this path")
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print full JSON report to stdout (after the markdown table)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print the stdout markdown table (useful with --md-out)",
    )
    p.add_argument("--keys", nargs="*", help="Only run these entitlement keys (default: all)")
    p.add_argument(
        "--apm-span-bytes-rate-scale",
        type=float,
        default=None,
        metavar="N",
        help="Override APM trace volume scale (default 1.0): multiplies hourly monthly means, legacy cycle monthly "
        "values, or rate-integration paths. Profile key license_apm_span_bytes_rate_integral_scale applies when omitted.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)

    cal_m = (args.calendar_month or "").strip() or None
    start_d = (args.start_date or "").strip() or None
    end_d = (args.end_date or "").strip() or None
    if cal_m and (start_d or end_d):
        logger.error("Use either --calendar-month or --start-date/--end-date, not both.")
        return 1
    if cal_m:
        parts = cal_m.split("-")
        if len(parts) != 2:
            logger.error("Invalid --calendar-month (use YYYY-MM).")
            return 1
        try:
            y, mo = int(parts[0]), int(parts[1])
        except ValueError:
            logger.error("Invalid --calendar-month (use YYYY-MM).")
            return 1
        if y < 1970 or mo < 1 or mo > 12:
            logger.error("Invalid --calendar-month (use YYYY-MM).")
            return 1
        start_d = f"{y:04d}-{mo:02d}-01"
        last_d = calendar.monthrange(y, mo)[1]
        end_d = f"{y:04d}-{mo:02d}-{last_d:02d}"
        logger.info("License utilization: full UTC month %s (%s → %s)", cal_m, start_d, end_d)
    if (start_d and not end_d) or (end_d and not start_d):
        logger.error("Both --start-date and --end-date are required when using a fixed calendar window.")
        return 1
    if start_d and end_d:
        try:
            start_ms = utc_start_of_day_ms(start_d)
            stop_ms = utc_end_of_day_ms_inclusive(end_d)
        except ValueError:
            logger.error("Invalid --start-date or --end-date (use YYYY-MM-DD).")
            return 1
        if start_ms > stop_ms:
            logger.error("License --start-date must be on or before --end-date.")
            return 1
        window_days = max(1, int((stop_ms - start_ms) / (24 * 3600 * 1000)) + 1)
        logger.info(
            "License utilization starting (UTC window %s → %s, ~%s days, resolution: %sh)",
            start_d,
            end_d,
            window_days,
            args.resolution_hours,
        )
    else:
        stop_ms = int(time.time() * 1000)
        start_ms = stop_ms - args.days * 24 * 3600 * 1000
        window_days = args.days
        logger.info(
            "License utilization starting (SignalFlow window: %s days, resolution: %sh)",
            args.days,
            args.resolution_hours,
        )

    profile_path = resolve_profile_path(args.profile)
    profile: dict[str, str] = {}
    if profile_path:
        if not os.path.isfile(profile_path):
            logger.error("Profile file not found: %s", profile_path)
            return 1
        try:
            profile = load_customer_profile_scalars(profile_path)
        except OSError as e:
            logger.error("Cannot read profile %s: %s", profile_path, e)
            return 1

    token = (
        os.environ.get("SPLUNK_ACCESS_TOKEN")
        or profile.get("access_token")
        or profile.get("ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        logger.error(
            "Org access token required — set SPLUNK_ACCESS_TOKEN or access_token in a customer profile "
            "(see --profile / customer-profile.example.yaml)."
        )
        return 1

    realm = (
        args.realm
        or profile.get("realm")
        or os.environ.get("SPLUNK_REALM")
        or "us0"
    ).strip()
    report_title = (
        args.report_title or profile.get("report_title") or "License utilization snapshot"
    ).strip()

    stream_url = f"https://stream.{realm}.signalfx.com"
    resolution_ms = max(1, args.resolution_hours) * 3600 * 1000

    specs = list(DEFAULT_ENTITLEMENTS)
    if args.keys:
        want = set(args.keys)
        specs = [s for s in specs if s.key in want]
        missing = want - {s.key for s in specs}
        if missing:
            logger.error("Unknown --keys: %s", sorted(missing))
            return 1

    rows_out: list[dict[str, Any]] = []

    logger.info(
        "Realm: %s | querying %s entitlement SignalFlow pair(s)",
        realm,
        len(specs),
    )

    wall_s, read_to, max_pts = _license_signalflow_client_limits(start_ms, stop_ms)
    logger.info(
        "SignalFlow execute budgets: wall_seconds=%.0f read_timeout=%.0fs max_points=%d",
        wall_s,
        read_to,
        max_pts,
    )

    has_fixed_window = bool(start_d and end_d)

    for spec in specs:
        eff_rate_scale = entitlement_rate_integral_scale_effective(
            spec, profile, args.apm_span_bytes_rate_scale
        )
        if spec.key == "apm_span_bytes" and eff_rate_scale != float(spec.rate_integral_scale):
            logger.info(
                "apm_span_bytes: monthly scale factor rate_integral_scale=%s (see --apm-span-bytes-rate-scale / profile)",
                eff_rate_scale,
            )
        row: dict[str, Any] = {
            "key": spec.key,
            "product": spec.product,
            "label": spec.label,
            "notes": spec.notes,
            "subscription_metric": spec.subscription_metric,
            "usage_metric": spec.usage_metric,
            "filter_fragment": spec.filter_fragment,
            "usage_program": spec.usage_program,
            "display_values_as_mb": spec.display_values_as_mb,
            "usage_aggregate": spec.usage_aggregate,
            "rate_integral_scale": eff_rate_scale
            if spec.usage_aggregate
            in (
                "rate_integral",
                "rate_mean_calendar_month",
                "platform_cycle_month",
                "hourly_mean_month",
            )
            else spec.rate_integral_scale,
            "subscription_aggregate": spec.subscription_aggregate,
        }
        u_prog = spec.program_usage()
        usage_stop_ms = stop_ms
        usage_resolution_ms = resolution_ms
        if spec.usage_aggregate == "hourly_mean_month":
            usage_resolution_ms = apm_span_usage_hourly_resolution_ms(profile)
        elif spec.key == "apm_span_bytes" and spec.usage_aggregate == "platform_cycle_month":
            usage_resolution_ms = apm_span_platform_cycle_resolution_ms(profile)
            if has_fixed_window:
                usage_stop_ms = extend_stop_ms_for_splunk_monthly_cycle(stop_ms)
                logger.info(
                    "apm_span_bytes: usage query stop extended (+3d) to include Mean(monthly) boundary datapoint"
                )
        u_pts, u_err = execute_signalflow_time_series(
            stream_url=stream_url,
            token=token,
            program=u_prog,
            start_ms=start_ms,
            stop_ms=usage_stop_ms,
            resolution_ms=usage_resolution_ms,
            wall_seconds=wall_s,
            read_timeout=read_to,
            max_points=max_pts,
        )
        u_by_month: dict[str, float] = {}
        if u_err:
            if u_err == "wall_timeout":
                logger.warning(
                    "Usage SignalFlow wall_timeout for entitlement %s — partial time range; "
                    "try a narrower window or check stream API health.",
                    spec.key,
                )
            row["usage_error"] = u_err
            row["usage"] = None
        else:
            u_vals = [v for _, v in u_pts]
            if spec.usage_aggregate == "sum":
                agg_u = aggregate_sum(u_vals)
                u_by_month = monthly_sum_by_month(u_pts)
            elif spec.usage_aggregate == "monthly_sum":
                u_by_month = monthly_sum_by_month(u_pts)
                agg_u = aggregate_values(list(u_by_month.values()))
            elif spec.usage_aggregate == "rate_integral":
                u_by_month = monthly_bytes_from_rate_points(
                    u_pts,
                    resolution_ms,
                    scale=float(eff_rate_scale),
                )
                agg_u = aggregate_values(list(u_by_month.values()))
            elif spec.usage_aggregate == "rate_mean_calendar_month":
                u_by_month = monthly_bytes_from_mean_rate_calendar_month(
                    u_pts,
                    scale=float(eff_rate_scale),
                )
                agg_u = aggregate_values(list(u_by_month.values()))
            elif spec.usage_aggregate == "platform_cycle_month":
                u_by_month = monthly_usage_from_platform_cycle_mean(
                    u_pts,
                    scale=float(eff_rate_scale),
                )
                agg_u = aggregate_values(list(u_by_month.values()))
            elif spec.usage_aggregate == "hourly_mean_month":
                u_by_month = monthly_mean_of_point_values_by_utc_month(
                    u_pts,
                    scale=float(eff_rate_scale),
                )
                agg_u = aggregate_values(list(u_by_month.values()))
            else:
                agg_u = aggregate_values(u_vals)
                u_by_month = monthly_mean_by_month(u_pts)
            row["usage"] = 0.0 if agg_u is None else float(agg_u)

        sub_prog = spec.program_subscription()
        sub_dict_for_monthly: dict[str, float] | None = None
        if sub_prog:
            s_pts, s_err = execute_signalflow_time_series(
                stream_url=stream_url,
                token=token,
                program=sub_prog,
                start_ms=start_ms,
                stop_ms=stop_ms,
                resolution_ms=resolution_ms,
                wall_seconds=wall_s,
                read_timeout=read_to,
                max_points=max_pts,
            )
            if s_err:
                if s_err == "wall_timeout":
                    logger.warning(
                        "Subscription SignalFlow wall_timeout for entitlement %s — partial time range.",
                        spec.key,
                    )
                row["subscription_error"] = s_err
                row["subscription"] = None
            else:
                if spec.subscription_aggregate == "rate_integral":
                    sub_dict_for_monthly = monthly_bytes_from_rate_points(s_pts, resolution_ms)
                    agg_s = aggregate_values(list(sub_dict_for_monthly.values()))
                else:
                    agg_s = aggregate_values([v for _, v in s_pts])
                    sub_dict_for_monthly = monthly_mean_by_month(s_pts)
                row["subscription"] = 0.0 if agg_s is None else float(agg_s)
        else:
            row["subscription"] = 0.0

        row["monthly"] = sort_monthly_rows_chronological(
            build_monthly_util_rows(u_by_month, sub_dict_for_monthly)
        )

        usage = row.get("usage")
        sub = row.get("subscription")
        util_pct: float
        if usage is not None and sub is not None and float(sub) > 0:
            util_pct = round(100.0 * float(usage) / float(sub), 2)
        else:
            util_pct = 0.0
        row["utilization_pct"] = util_pct
        row["severity"] = severity(util_pct)
        rows_out.append(row)

        # Pace SignalFlow/API to reduce throttling when many entitlements are enabled.
        time.sleep(0.5)

    fixed_end = end_d if (start_d and end_d) else None
    apply_complete_month_filter_to_rows(rows_out, stop_ms, fixed_window_end_date=fixed_end)
    enrich_apm_profiling_subscription_mb(rows_out)
    apply_complete_month_filter_to_rows(rows_out, stop_ms, fixed_window_end_date=fixed_end)
    rows_out, model_filter_meta = filter_rows_by_active_license_models(rows_out)
    rows_out = drop_im_hosts_if_zero_subscription(rows_out)

    report = {
        "schema": 2,
        "single_org": True,
        "realm": realm,
        "window_days": window_days,
        "resolution_ms": resolution_ms,
        "start_ms": start_ms,
        "stop_ms": stop_ms,
        "latest_complete_month_utc": monthly_cap_for_tables(stop_ms, fixed_end),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": rows_out,
    }
    if model_filter_meta.get("excluded_keys"):
        report["license_model_filter"] = model_filter_meta
    if start_d and end_d:
        report["fixed_window_utc"] = {"startDate": start_d, "endDate": end_d}
        if cal_m:
            report["fixed_window_utc"]["calendarMonth"] = cal_m

    logger.info("Built license report: %s entitlement row(s) in JSON", len(rows_out))

    if args.md_out:
        parent = os.path.dirname(args.md_out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        md_body = build_license_markdown(report, title=report_title)
        with open(args.md_out, "w", encoding="utf-8") as f:
            f.write(md_body)
        logger.info("Wrote markdown snapshot: %s", args.md_out)

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        logger.info("Wrote JSON: %s", args.json_out)

    if not args.quiet:
        # Stdout summary (Subscription → Utilization → Util %, aligned with report tables)
        rows_print = filter_license_rows_for_subscription_report(rows_out)
        print(
            "| Product | Entitlement | Subscription (avg) | Utilization (avg) | Utilization % | Color |"
        )
        print("| --- | --- | --- | --- | --- | --- |")
        if not rows_print and rows_out:
            print(
                "| — | *(no product with subscription entitlement — omitted)* | — | — | — | — |",
            )
            print(
                "(Products with no subscription allowance on any line item are omitted; full JSON still lists all queries.)",
                file=sys.stderr,
            )
        for r in rows_print:
            u = r.get("usage")
            s = r.get("subscription")
            pct = r.get("utilization_pct")
            sev = r.get("severity") or ("—" if pct is None else "")
            u_s = (
                fmt_table_number(u)
                if u is not None
                else (r.get("usage_error") or "n/a")
            )
            s_s = (
                fmt_table_number(s)
                if s is not None
                else (r.get("subscription_error") or ("—" if r.get("subscription") is None else "n/a"))
            )
            pct_s = (
                fmt_table_number(pct, decimals=1, percent=True) if pct is not None else "0.0%"
            )
            label = r["label"]
            if r.get("notes"):
                label = f"{label} *"
            print(f"| {r['product']} | {label} | {s_s} | {u_s} | {pct_s} | {sev or '—'} |")

        if any(r.get("notes") for r in rows_print):
            print("\n* See snapshot `notes` for caveats.", file=sys.stderr)

    if args.print_json:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
