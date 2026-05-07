#!/usr/bin/env python3
"""
Splunk Observability Cloud — Infrastructure Monitoring metrics usage breakdown (health-check helper).

Aligns with **Metric Cardinality & Volume** in Splunk-Observability-Health-Check.md: **Billing Class**,
**Cardinality (MTS)**, **Utilization** (R0–R4), **% Over Total** (metric MTS ÷ Σ MTS in the result set).
Calls the same **Usage analytics → Metrics** REST API the UI uses:

  GET https://app.{realm}.signalfx.com/v2/metrics-usage/metrics/_?lookbackPeriod=...&limit=...&billable=...&orderBy=...

The API returns estimated **average hourly MTS** per metric (see UI “Analyze metric usage”). Data refreshes
about hourly; heavy polling may be rate-limited (wait ~2 minutes per product docs).

Environment:
  SPLUNK_ACCESS_TOKEN  — org API token (required unless profile has access_token)
  SPLUNK_REALM         — optional when --realm omitted (default us0)

Profile (optional YAML): same as other scripts — customer-profile.local.yaml / customer-profile.yaml
with access_token, realm.

Usage:
  SPLUNK_ACCESS_TOKEN=... SPLUNK_REALM=us1 python3 scripts/o11y_im_metrics_usage_breakdown.py
  python3 scripts/o11y_im_metrics_usage_breakdown.py --lookback P7D --limit 5000 --md-out reports/im-metrics-usage.md
  python3 scripts/o11y_im_metrics_usage_breakdown.py --print-schema   # show keys from first row if parsing fails
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_report_format import fmt_table_number  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

LOOKBACK_CHOICES = ("P1D", "P7D", "P30D")

logger = logging.getLogger(__name__)
STRUCTURED_SCHEMA = "o11y_im_metrics_usage/v2"

# Customer-facing **Recommendation** only (must match ``Splunk-Observability-Health-Check.md``). Methodology lives in
# ``internal-process-notes.md`` (repo root), not in reports.
_IM_METRIC_CARDINALITY_RECOMMENDATION_CHECKLIST = (
    "Use Metric Pipeline Management (MPP) to create rules to archive or drop metrics and dimensions not being used "
    "in detectors, dashboards, or API calls. Consider enabling auto-archiving rules."
)

# Checklist labels (Splunk-Observability-Health-Check.md — Metric Cardinality & Volume)
BILLING_CLASSES: tuple[str, ...] = (
    "Custom",
    "Default / Bundled (App)",
    "Default / Bundled (Infra)",
    "Other",
)
UTILIZATION_RANKS: tuple[str, ...] = (
    "R0 - Unused",
    "R1 - Inactive Charts",
    "R2 - API queries",
    "R3 - Active charts",
    "R4 - Detectors",
)

# Metric Cardinality & Volume table: only list rows whose share of Σ MTS (full candidate set) is at least this %.
MIN_PCT_OVER_TOTAL: float = 1.0


def _md_cell_im(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _dict_looks_like_metric_usage_row(d: dict[str, Any]) -> bool:
    """Heuristic: Usage analytics rows include MTS and/or metric identity fields."""
    lk = {str(k).lower() for k in d}
    mts_keys = (
        "averagehourlymtscount",
        "averagehourlymts",
        "mtscount",
        "hourlymtsaverage",
        "avghourlymts",
    )
    if any(k in lk for k in mts_keys):
        return True
    id_keys = ("metricname", "metric", "name", "id", "metric_name")
    util_keys = (
        "utilization",
        "utilizationtier",
        "usagetier",
        "maximumutilizationreach",
        "billingclass",
        "metricbillingcategory",
    )
    if (lk & set(id_keys)) and (lk & set(util_keys)):
        return True
    # Some API versions expose mostly identity + billing with sparse utilization fields
    if (lk & set(id_keys)) and len(lk) >= 4:
        return True
    return False


def _deep_extract_metric_rows(payload: Any, *, max_depth: int = 14) -> list[dict[str, Any]]:
    """
    Walk JSON for the largest list of dicts that look like Usage analytics metric rows.
    Covers nested envelopes (e.g. ``data.metricsUsage.results``) when top-level keys change.
    """
    best: list[dict[str, Any]] = []

    def walk(o: Any, depth: int) -> None:
        nonlocal best
        if depth > max_depth:
            return
        if isinstance(o, list) and o:
            if all(isinstance(x, dict) for x in o) and _dict_looks_like_metric_usage_row(o[0]):
                if len(o) > len(best):
                    best = [x for x in o if isinstance(x, dict)]
                return
            for x in o:
                walk(x, depth + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)

    walk(payload, 0)
    return best


def _extract_row_list(payload: Any) -> list[dict[str, Any]]:
    """Normalize API JSON to a list of metric row dicts (handles several envelope shapes)."""
    if isinstance(payload, list):
        out = [x for x in payload if isinstance(x, dict)]
        if out:
            return out
        return _deep_extract_metric_rows(payload)
    if not isinstance(payload, dict):
        return []
    for key in ("results", "metrics", "data", "items", "records", "rows"):
        v = payload.get(key)
        if isinstance(v, list):
            out = [x for x in v if isinstance(x, dict)]
            if out:
                return out
    nested = payload.get("data")
    if isinstance(nested, dict):
        sub = _extract_row_list(nested)
        if sub:
            return sub
    deep = _deep_extract_metric_rows(payload)
    return deep


def _metric_name(row: dict[str, Any]) -> str:
    for key in ("metricName", "name", "metric", "id", "metric_name"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _mts_value(row: dict[str, Any]) -> float:
    for key in (
        "averageHourlyMtsCount",
        "averageHourlyMts",
        "mtsCount",
        "hourlyMtsAverage",
        "avgHourlyMts",
    ):
        v = row.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip():
            try:
                return float(v.strip())
            except ValueError:
                continue
    return 0.0


def _optional_source(row: dict[str, Any]) -> str:
    for key in ("source", "integration", "integrationType", "category", "metricType", "origin"):
        v = row.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _row_get_ci(row: dict[str, Any], *candidates: str) -> Any:
    """First value whose key matches one of ``candidates`` case-insensitively."""
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _intish(x: Any) -> int | None:
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x)
    if isinstance(x, str) and x.strip():
        try:
            return int(float(x.strip()))
        except ValueError:
            return None
    return None


def _normalize_billing_label(raw: str) -> str | None:
    t = raw.strip().lower()
    if not t:
        return None
    if "custom" in t and "non-custom" not in t:
        return "Custom"
    if "bundled" in t or "bundle" in t:
        if "app" in t or "application" in t:
            return "Default / Bundled (App)"
        if "infra" in t or t.startswith("infrastructure"):
            return "Default / Bundled (Infra)"
    if "default" in t and "app" in t:
        return "Default / Bundled (App)"
    if "default" in t and ("infra" in t or "infrastructure" in t):
        return "Default / Bundled (Infra)"
    if t in ("other", "unknown", "misc", "miscellaneous"):
        return "Other"
    return None


def _billing_class_from_row(row: dict[str, Any], metric_name: str) -> str:
    """Map API or metric name to checklist **Billing Class**."""
    raw = _row_get_ci(
        row,
        "billingClass",
        "billingCategory",
        "metricBillingCategory",
        "billingType",
        "metricBillingType",
        "productCategory",
        "category",
        "metricCategory",
    )
    if isinstance(raw, str) and raw.strip():
        norm = _normalize_billing_label(raw)
        if norm:
            return norm
        # Passthrough if already one of the canonical labels
        for b in BILLING_CLASSES:
            if raw.strip().lower() == b.lower():
                return b
    n = metric_name.lower()
    for p in (
        "aws.",
        "gcp.",
        "azure.",
        "k8s.",
        "kubernetes",
        "container.",
        "kube_",
        "host.",
        "process.",
        "system.",
        "network.",
        "disk.",
        "memory.",
        "cpu.",
    ):
        if n.startswith(p) or f".{p}" in n:
            return "Default / Bundled (Infra)"
    if n.startswith("sf."):
        return "Default / Bundled (App)"
    if ".custom." in n or n.startswith("custom."):
        return "Custom"
    return "Other"


def _normalize_utilization_label(raw: str) -> str | None:
    t = raw.strip().upper()
    if not t:
        return None
    if "R4" in t or "DETECTOR" in t:
        return "R4 - Detectors"
    if "R3" in t or ("ACTIVE" in t and "CHART" in t):
        return "R3 - Active charts"
    if "R2" in t or "API" in t:
        return "R2 - API queries"
    if "R1" in t or ("INACTIVE" in t and "CHART" in t):
        return "R1 - Inactive Charts"
    if "R0" in t or "UNUSED" in t or t == "NONE":
        return "R0 - Unused"
    return None


def _utilization_from_row(row: dict[str, Any]) -> str:
    """Map API fields to checklist **Utilization** R0–R4; else **R0 - Unused** when utilization cannot be inferred."""
    raw = _row_get_ci(
        row,
        "utilization",
        "utilizationTier",
        "usageTier",
        "utilizationReach",
        "maximumUtilizationReach",
        "maxUtilizationReach",
        "reach",
        "usageReach",
        "metricUtilization",
    )
    if isinstance(raw, str) and raw.strip():
        norm = _normalize_utilization_label(raw)
        if norm:
            return norm
        for u in UTILIZATION_RANKS:
            if raw.strip().lower() == u.lower():
                return u

    d = _intish(_row_get_ci(row, "usedInDetectors", "detectorCount", "detectorsCount", "numDetectors"))
    ac = _intish(
        _row_get_ci(
            row,
            "usedInActiveCharts",
            "activeChartCount",
            "activeChartsCount",
            "numActiveCharts",
        )
    )
    ic = _intish(
        _row_get_ci(
            row,
            "usedInInactiveCharts",
            "inactiveChartCount",
            "inactiveChartsCount",
            "numInactiveCharts",
        )
    )
    api = _intish(_row_get_ci(row, "usedInApi", "apiQueryCount", "numApiQueries", "apiCount"))

    if d is not None and d > 0:
        return "R4 - Detectors"
    if ac is not None and ac > 0:
        return "R3 - Active charts"
    if api is not None and api > 0:
        return "R2 - API queries"
    if ic is not None and ic > 0:
        return "R1 - Inactive Charts"
    zs = (d, ac, ic, api)
    if all(z is not None for z in zs) and all(z == 0 for z in zs):
        return "R0 - Unused"

    return "R0 - Unused"


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest MTS first (stable sort by metric name)."""
    decorated = sorted(
        rows,
        key=lambda r: (-_mts_value(r), _metric_name(r)),
    )
    return decorated


def _build_url(
    realm: str,
    *,
    lookback: str,
    limit: int,
    billable: bool,
    order_by: str,
) -> str:
    base = f"https://app.{realm}.signalfx.com"
    path = "/v2/metrics-usage/metrics/_"
    q = urllib.parse.urlencode(
        {
            "lookbackPeriod": lookback,
            "limit": str(limit),
            "billable": "true" if billable else "false",
            "orderBy": order_by,
        }
    )
    return f"{base}{path}?{q}"


def fetch_metrics_usage_payload(
    token: str,
    realm: str,
    *,
    lookback: str,
    limit: int,
    billable: bool,
    order_by: str,
) -> tuple[Any | None, str | None]:
    """
    GET Usage analytics metrics. Returns (parsed JSON, None) or (None, error message).
    """
    url = _build_url(
        realm,
        lookback=lookback,
        limit=limit,
        billable=billable,
        order_by=order_by,
    )
    req = urllib.request.Request(
        url,
        headers={"X-SF-Token": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = (e.read() or b"").decode("utf-8", errors="replace")
        return None, f"HTTP {e.code}: {err[:2000]}"
    except OSError as e:
        return None, str(e)

    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"


def build_structured_report(
    *,
    realm: str,
    lookback: str,
    limit: int,
    billable: bool,
    order_by: str,
    payload: Any,
    top: int | None,
) -> dict[str, Any]:
    """
    Normalized report for ``o11y_health_check_run.py`` and JSON export.
    """
    rows = _extract_row_list(payload)
    rows = _sort_rows(rows)
    if top is not None:
        rows = rows[: max(0, top)]
    total_mts = sum(_mts_value(r) for r in rows)
    candidate_metric_count = len(rows)
    metrics: list[dict[str, Any]] = []
    for r in rows:
        mts = _mts_value(r)
        pct = (100.0 * mts / total_mts) if total_mts > 0 else 0.0
        if pct < MIN_PCT_OVER_TOTAL:
            continue
        mname = _metric_name(r)
        metrics.append(
            {
                "metricName": mname,
                "billingClass": _billing_class_from_row(r, mname),
                "utilization": _utilization_from_row(r),
                "averageHourlyMts": mts,
                "pctOverTotal": round(pct, 4),
                "pctOfListedTotal": round(pct, 4),
            }
        )
    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": realm,
        "lookbackPeriod": lookback,
        "limit": limit,
        "billable": billable,
        "orderBy": order_by,
        "metricCount": len(metrics),
        "candidateMetricCount": candidate_metric_count,
        "minPctOverTotal": MIN_PCT_OVER_TOTAL,
        "metrics": metrics,
        "totalMtsSum": total_mts,
        "note": (
            "Cardinality (MTS) is estimated average hourly MTS from Usage analytics. "
            "**% Over Total** = this metric’s MTS ÷ sum of MTS for all metrics in the candidate set (before the "
            f"**≥ {MIN_PCT_OVER_TOTAL:g}%** share filter). "
            "Billing class uses API fields when present, else name heuristics (may be **Other**). "
            "Utilization R0–R4 uses API fields when present; otherwise **R0 - Unused** when tiers cannot be inferred."
        ),
    }


def render_im_checks_markdown(
    im_report: dict[str, Any] | None,
    im_integrations_report: dict[str, Any] | None = None,
) -> str:
    """
    Infrastructure monitoring section for the consolidated health-check report.
    **Metric Cardinality & Volume** (metrics usage) plus **Analyze Integrations** (integration list API).
    """
    from o11y_im_integrations import render_analyze_integrations_markdown

    parts: list[str] = ["## Infrastructure monitoring health checks\n"]

    # --- Metric Cardinality & Volume ---
    parts.append("### Metric Cardinality & Volume\n")
    parts.append(
        "List of metrics with high cardinality and whether they are being utilized in detectors, dashboards, or API. "
        "Only metrics with **% Over Total ≥ 1%** are listed.\n\n"
    )
    parts.append("### Results\n")
    parts.append("")
    parts.append("| Metric Name | Billing Class | Cardinality (MTS) | Utilization | % Over Total |")
    parts.append("| --- | --- | --- | --- | --- |")

    if not im_report:
        parts.append("|  |  |  |  |  |")
        parts.append("")
        parts.append("### Recommendation\n")
        parts.append("")
        parts.append("*None — check not executed.*\n")
        parts.append(render_analyze_integrations_markdown(im_integrations_report))
        return "\n".join(parts)

    err = im_report.get("error")
    metrics = im_report.get("metrics")
    if not isinstance(metrics, list):
        metrics = []

    if err:
        parts.append("|  |  |  |  |  |")
        parts.append("")
        parts.append("### Recommendation\n")
        parts.append("")
        parts.append(
            f"*Could not populate metrics table:* {_md_cell_im(str(err))} "
            "Verify Splunk Observability access and Usage analytics data for this org.\n"
        )
        parts.append(render_analyze_integrations_markdown(im_integrations_report))
        return "\n".join(parts)

    if not metrics:
        parts.append("|  |  |  |  |  |")
        parts.append("")
        cand = im_report.get("candidateMetricCount")
        if isinstance(cand, int) and cand > 0:
            msg = (
                f"No metrics meet the **≥ {MIN_PCT_OVER_TOTAL:g}%** **% Over Total** threshold "
                f"({cand} candidate metric(s) in the Usage analytics result set)."
            )
        else:
            msg = "No metric rows returned."
        parts.append("### Recommendation\n")
        parts.append("")
        parts.append(f"*{msg}*\n")
        parts.append(render_analyze_integrations_markdown(im_integrations_report))
        return "\n".join(parts)

    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = _md_cell_im(str(m.get("metricName") or "—"))
        bill = _md_cell_im(str(m.get("billingClass") or "—"))
        mts = m.get("averageHourlyMts")
        mts_s = fmt_table_number(mts) if mts is not None else "—"
        util = _md_cell_im(str(m.get("utilization") or "—"))
        pct = m.get("pctOverTotal")
        if pct is None:
            pct = m.get("pctOfListedTotal")
        pct_s = fmt_table_number(pct, decimals=2) + "%" if pct is not None else "—"
        parts.append(f"| {name} | {bill} | {mts_s} | {util} | {pct_s} |")

    parts.append("")
    parts.append("### Recommendation\n")
    parts.append("")
    parts.append(_IM_METRIC_CARDINALITY_RECOMMENDATION_CHECKLIST + "\n")

    parts.append(render_analyze_integrations_markdown(im_integrations_report))
    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description="Metrics usage breakdown (avg hourly MTS per metric).")
    p.add_argument("--realm", default=None, help="Realm (e.g. us1). Default: SPLUNK_REALM / profile / us0")
    p.add_argument("--profile", default=None, help="YAML profile path (default: customer-profile*.yaml)")
    p.add_argument(
        "--lookback",
        choices=LOOKBACK_CHOICES,
        default="P1D",
        help="Lookback period (default P1D).",
    )
    p.add_argument("--limit", type=int, default=10000, help="Max metrics to return (default 10000).")
    p.add_argument(
        "--billable",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Billable metrics only (default: true).",
    )
    p.add_argument(
        "--order-by",
        default="-averageHourlyMtsCount",
        help='Sort field for API (default: -averageHourlyMtsCount).',
    )
    p.add_argument("--json-out", metavar="PATH", help="Write full API JSON response to this file.")
    p.add_argument(
        "--structured-json-out",
        metavar="PATH",
        help="Write normalized report JSON (schema %s) for o11y_health_check_run.py."
        % STRUCTURED_SCHEMA,
    )
    p.add_argument(
        "--md-out",
        metavar="PATH",
        help="Write markdown table: Metric Name, Avg hourly MTS, %% of sum (this page only).",
    )
    p.add_argument("--top", type=int, default=None, help="Only include top N rows in printed/md tables.")
    p.add_argument(
        "--print-schema",
        action="store_true",
        help="Print JSON keys from the first parsed row (debug unknown response shape).",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("IM metrics usage breakdown starting (GET /v2/metrics-usage/metrics)")

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

    logger.info("Realm: %s | lookback: %s | billable filter: %s", realm, args.lookback, args.billable)

    billable_effective = args.billable
    used_non_billable_fallback = False

    payload, fetch_err = fetch_metrics_usage_payload(
        token,
        realm,
        lookback=args.lookback,
        limit=args.limit,
        billable=args.billable,
        order_by=args.order_by,
    )
    if fetch_err:
        logger.error("Usage analytics API failed: %s", fetch_err[:1500])
        structured = {
            "schema": STRUCTURED_SCHEMA,
            "realm": realm,
            "lookbackPeriod": args.lookback,
            "limit": args.limit,
            "billable": args.billable,
            "error": fetch_err,
            "metrics": [],
        }
        if args.structured_json_out:
            p = Path(args.structured_json_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(structured, indent=2) + "\n", encoding="utf-8")
            logger.info("Wrote error structured JSON: %s", args.structured_json_out)
        return 1

    # Billable-only can return zero rows for some orgs; retry once with non-billable metrics.
    rows_probe = _extract_row_list(payload)
    if not rows_probe and args.billable:
        payload2, err2 = fetch_metrics_usage_payload(
            token,
            realm,
            lookback=args.lookback,
            limit=args.limit,
            billable=False,
            order_by=args.order_by,
        )
        if not err2:
            rows2 = _extract_row_list(payload2)
            if rows2:
                logger.warning(
                    "billable=true returned no rows; retrying with billable=false (broader metric set)."
                )
                payload = payload2
                billable_effective = False
                used_non_billable_fallback = True
            else:
                rows_probe = _extract_row_list(payload)
        else:
            rows_probe = _extract_row_list(payload)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rows = _extract_row_list(payload)
    if not rows:
        logger.warning(
            "No metric rows in parsed response; use --json-out to inspect raw payload (check token and UI parity)."
        )
        if isinstance(payload, dict):
            logger.debug("Top-level keys: %s", list(payload.keys()))
        structured = {
            "schema": STRUCTURED_SCHEMA,
            "realm": realm,
            "lookbackPeriod": args.lookback,
            "limit": args.limit,
            "billable": billable_effective,
            "billableRequested": args.billable,
            "usedNonBillableFallback": used_non_billable_fallback,
            "error": "no_metric_rows_in_response",
            "metrics": [],
            "debugHint": (
                f"Empty table after billable and non-billable attempts (when applicable). "
                f"Confirm token can reach Usage analytics on app.{realm}.signalfx.com; "
                "save --json-out of the raw response if the UI shows metrics but this script does not."
            ),
        }
        if args.structured_json_out:
            p = Path(args.structured_json_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(structured, indent=2) + "\n", encoding="utf-8")
            logger.info("Wrote empty structured JSON: %s", args.structured_json_out)
        return 2

    if args.print_schema and rows:
        first = rows[0]
        print("First row keys:", sorted(first.keys()), file=sys.stderr)
        logger.info("First row keys (schema probe): %s", sorted(first.keys()))

    structured = build_structured_report(
        realm=realm,
        lookback=args.lookback,
        limit=args.limit,
        billable=billable_effective,
        order_by=args.order_by,
        payload=payload,
        top=args.top,
    )
    structured["billableRequested"] = args.billable
    if used_non_billable_fallback:
        structured["usedNonBillableFallback"] = True
        extra = (
            " First query used billable metrics only and returned no rows; "
            "this table uses **non-billable** metrics (broader set) — compare with the Usage analytics UI filter."
        )
        structured["note"] = str(structured.get("note") or "") + extra

    metrics_out = structured.get("metrics") or []
    if args.structured_json_out:
        p = Path(args.structured_json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(structured, indent=2) + "\n", encoding="utf-8")
        logger.info("Wrote structured JSON: %s (%s metric row(s))", args.structured_json_out, len(metrics_out))

    # stdout: simple TSV-friendly table (same rows as structured JSON / consolidated report)
    print(
        f"# Metrics usage (lookback={args.lookback}, realm={realm}, "
        f"n={len(metrics_out)} shown, {structured.get('candidateMetricCount', 0)} candidates, "
        f"% over total ≥ {MIN_PCT_OVER_TOTAL:g}%)"
    )
    print("metric_name\tbilling_class\tutilization\tavg_hourly_mts\tpct_over_total")
    for m in metrics_out:
        if not isinstance(m, dict):
            continue
        name = str(m.get("metricName") or "—")
        mts = m.get("averageHourlyMts")
        mts_f = float(mts) if isinstance(mts, (int, float)) else 0.0
        pct = m.get("pctOverTotal")
        pct_f = float(pct) if isinstance(pct, (int, float)) else 0.0
        print(
            f"{name}\t{m.get('billingClass', '')}\t{m.get('utilization', '')}\t"
            f"{fmt_table_number(mts_f)}\t{fmt_table_number(pct_f, decimals=2)}%"
        )

    if args.md_out:
        cand = structured.get("candidateMetricCount")
        lines = [
            "## Metric cardinality & volume (usage analytics)",
            "",
            f"Lookback: **{args.lookback}** · Realm: **{realm}** · **{len(metrics_out)}** metric(s) listed "
            f"(**% Over Total ≥ {MIN_PCT_OVER_TOTAL:g}%**; {cand} candidate(s) before filter; API limit {args.limit}).",
            "",
            "| Metric Name | Billing Class | Cardinality (MTS) | Utilization | % Over Total |",
            "| --- | --- | --- | --- | --- |",
        ]
        for m in metrics_out:
            if not isinstance(m, dict):
                continue
            name = str(m.get("metricName") or "—")
            mts = m.get("averageHourlyMts")
            pct = m.get("pctOverTotal")
            pct_f = float(pct) if isinstance(pct, (int, float)) else 0.0
            lines.append(
                f"| {name} | {m.get('billingClass', '—')} | {fmt_table_number(mts)} | "
                f"{m.get('utilization', '—')} | {fmt_table_number(pct_f, decimals=2)}% |"
            )
        md_path = Path(args.md_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    logger.info("Done: %s metric row(s) emitted on stdout", len(metrics_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
