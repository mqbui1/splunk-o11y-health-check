#!/usr/bin/env python3
"""
Splunk Observability Cloud — Dashboards health check (read-only).

Uses REST on ``https://api.{realm}.signalfx.com`` (``/v2/dashboardgroup``, ``/v2/dashboard``,
``/v2/detector``, ``/v2/metrictimeseries``) aligned with ``Splunk-Observability-Health-Check.md``
**Dashboards Health Checks**.

Dashboard names and duplicate dashboard IDs in markdown tables render as HTML links (``target="_blank"``,
``rel="noopener noreferrer"``) to ``https://app.{realm}.signalfx.com/#/dashboard/{id}?groupId=…&configId=…`` when
a chart context exists (``configId`` omitted for duplicate-ID-only rows).

Environment: ``SPLUNK_ACCESS_TOKEN`` / profile ``access_token``; ``realm`` in profile or ``SPLUNK_REALM``.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import http.client
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_dashboards_health/v1"

logger = logging.getLogger(__name__)

# --- Checklist copy (## Dashboards Health Checks) ---------------------------------
DASHBOARD_HEALTH_CHECKLIST: dict[str, dict[str, str]] = {
    "detectorLinks": {
        "title": "Links to Deleted/Inactive Detectors",
        "description": (
            "List of charts that have a link to a non-existing detector or an inactive detector."
        ),
        "recommendation": "Delete old links.",
    },
    "inactiveCharts": {
        "title": "Inactive Charts",
        "description": "List of charts that are using inactive metrics.",
        "recommendation": (
            "Review dashboard groups that have many inactive charts/dashboards; they may no longer be used. "
            "Delete old charts/dashboards that are no longer needed."
        ),
    },
    "duplicateDashboards": {
        "title": "Duplicate Dashboards",
        "description": (
            "Teams may often clone dashboards. This can lead to many identical dashboards across the organization."
        ),
        "recommendation": (
            "Consolidate commonly used dashboards into a public dashboard group accessible to all teams."
        ),
    },
}


def api_base(realm: str) -> str:
    return f"https://api.{realm}.signalfx.com"


def app_base(realm: str) -> str:
    return f"https://app.{realm}.signalfx.com"


def dashboard_open_url(
    realm: str,
    dashboard_id: str,
    group_id: str,
    *,
    config_id: str | None = None,
) -> str:
    """
    Splunk Observability UI URL for a dashboard (same host family as detector links).

    Example (realm ``us1``)::

      https://app.us1.signalfx.com/#/dashboard/{dashboardId}?groupId={groupId}&configId={configId}

    ``config_id`` is omitted when unknown (e.g. duplicate-dashboard rows without a chart context).
    """
    r = str(realm or "").strip()
    did = str(dashboard_id or "").strip()
    if not r or not did:
        return ""
    path = f"/#/dashboard/{urllib.parse.quote(did, safe='')}"
    q: list[tuple[str, str]] = []
    gid = str(group_id or "").strip()
    if gid:
        q.append(("groupId", gid))
    cfg = (config_id or "").strip()
    if cfg and cfg != "—":
        q.append(("configId", cfg))
    qs = urllib.parse.urlencode(q, doseq=True) if q else ""
    return f"{app_base(r).rstrip('/')}{path}" + (f"?{qs}" if qs else "")


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _md_dashboard_name_cell(row: dict[str, Any], realm: str) -> str:
    """Dashboard Name column: link to UI when id + realm are available."""
    name = str(row.get("dashboardName") or "—").replace("\n", " ")
    did = str(row.get("dashboardId") or "").strip()
    gid = str(row.get("dashboardGroupId") or "").strip()
    cfg = str(row.get("chartConfigId") or "").strip()
    if cfg in ("", "—"):
        cfg = None
    url = dashboard_open_url(realm, did, gid, config_id=cfg) if realm and did else ""
    if not url:
        return _md_cell(name)
    esc_href = html.escape(url, quote=True)
    esc_text = html.escape(name, quote=False).replace("|", "&#124;")
    return f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>'


def _md_duplicate_dashboard_ids_cell(row: dict[str, Any], realm: str) -> str:
    """Duplicate Dashboard IDs: comma-separated id links (new tab), same pattern as redundant detector ids."""
    ids = row.get("duplicateDashboardIdList")
    gid = str(row.get("dashboardGroupId") or "").strip()
    if not isinstance(ids, list) or not ids:
        return _md_cell(str(row.get("duplicateDashboardIds") or ""))
    parts: list[str] = []
    for oid in ids:
        o = str(oid).strip()
        if not o:
            continue
        url = dashboard_open_url(realm, o, gid, config_id=None) if realm else ""
        esc_text = html.escape(o, quote=False).replace("|", "&#124;")
        if not url:
            parts.append(esc_text)
            continue
        esc_href = html.escape(url, quote=True)
        parts.append(f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>')
    if row.get("duplicateDashboardIdsHasMore"):
        parts.append("…")
    return ", ".join(parts) if parts else "—"


def api_get(token: str, realm: str, path: str, params: dict[str, Any] | None = None) -> tuple[Any | None, str | None]:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    url = f"{api_base(realm).rstrip('/')}{path}"
    if q:
        url = f"{url}?{q}"
    req = urllib.request.Request(
        url,
        headers={"X-SF-Token": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = (e.read() or b"").decode("utf-8", errors="replace")
        return None, f"HTTP {e.code} {path}: {err[:1500]}"
    except (OSError, http.client.IncompleteRead) as e:
        return None, str(e)
    if not raw.strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON {path}: {e}"


def paginate_results(
    token: str,
    realm: str,
    path: str,
    *,
    page_size: int,
    extra_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = dict(extra_params or {})
        params["limit"] = page_size
        params["offset"] = offset
        payload, err = api_get(token, realm, path, params)
        if err:
            return [], err
        if payload is None:
            break
        if isinstance(payload, list):
            rows = [x for x in payload if isinstance(x, dict)]
            out.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
            continue
        if isinstance(payload, dict):
            chunk = payload.get("results")
            if isinstance(chunk, list):
                rows = [x for x in chunk if isinstance(x, dict)]
                out.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
                continue
            return [], f"unexpected envelope keys: {list(payload.keys())[:20]}"
        break
    return out, None


_DATA_METRIC_RE = re.compile(r"""data\(\s*['\"]([^'\"]+)['\"]""", re.I)


def extract_metrics_from_text(blob: str) -> list[str]:
    out: list[str] = []
    for m in _DATA_METRIC_RE.finditer(blob or ""):
        name = m.group(1).strip()
        if name and name not in out:
            out.append(name)
    return out


_DETECTOR_URL_RE = re.compile(
    r"(?:#/detector/v2/|/detector/v2/|detectors?/)([A-Za-z0-9_-]{8,})",
    re.I,
)


def extract_detector_ids_from_text(blob: str) -> list[str]:
    if not blob:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _DETECTOR_URL_RE.finditer(blob):
        did = m.group(1).strip()
        if did and did not in seen:
            seen.add(did)
            out.append(did)
    return out


def collect_strings(obj: Any) -> str:
    """Flatten string values for regex extraction (chart subtree)."""
    parts: list[str] = []

    def w(o: Any) -> None:
        if isinstance(o, str):
            parts.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                w(v)
        elif isinstance(o, list):
            for x in o:
                w(x)

    w(obj)
    return "\n".join(parts)


def iter_chart_units(dashboard: dict[str, Any]) -> list[tuple[str, str, str]]:
    """
    Yield ``(chart_id, chart_name, text_blob)`` for each chart-like object in a dashboard payload.
    """
    found: list[tuple[str, str, str]] = []
    charts = dashboard.get("charts")
    if isinstance(charts, dict):
        for cid, cobj in charts.items():
            if not isinstance(cobj, dict):
                continue
            name = str(cobj.get("name") or cobj.get("chartName") or cid)
            inner = cobj.get("chart") if isinstance(cobj.get("chart"), dict) else cobj
            blob = collect_strings(inner)
            found.append((str(cid), name, blob))
    elif isinstance(charts, list):
        for i, cobj in enumerate(charts):
            if not isinstance(cobj, dict):
                continue
            cid = str(cobj.get("id") or f"chart_{i}")
            name = str(cobj.get("name") or cid)
            inner = cobj.get("chart") if isinstance(cobj.get("chart"), dict) else cobj
            blob = collect_strings(inner)
            found.append((cid, name, blob))
    if not found:
        blob = json.dumps(dashboard)
        if blob.strip():
            found.append(("—", "(dashboard JSON)", blob))
    return found


def load_detector_index(token: str, realm: str) -> tuple[dict[str, dict[str, Any]], str | None]:
    """``detector_id`` -> ``{exists, disabled, name}`` from paginated ``GET /v2/detector``."""
    rows, err = paginate_results(token, realm, "/v2/detector", page_size=100)
    if err:
        return {}, err
    idx: dict[str, dict[str, Any]] = {}
    for r in rows:
        did = str(r.get("id") or "").strip()
        if not did:
            continue
        disabled = bool(r.get("disabled")) if r.get("disabled") is not None else False
        if isinstance(r.get("enabled"), bool) and not r["enabled"]:
            disabled = True
        if r.get("paused") is True:
            disabled = True
        st = str(r.get("status") or "").strip().lower()
        if st in ("disabled", "inactive", "paused"):
            disabled = True
        idx[did] = {
            "exists": True,
            "disabled": disabled,
            "name": str(r.get("name") or did),
        }
    return idx, None


def load_dashboard_groups(token: str, realm: str) -> tuple[dict[str, str], str | None]:
    """``group_id`` -> display name."""
    rows, err = paginate_results(token, realm, "/v2/dashboardgroup", page_size=100)
    if err:
        # Some tokens may lack scope — return empty map, names fall back to id
        return {}, err
    out: dict[str, str] = {}
    for r in rows:
        gid = str(r.get("id") or "").strip()
        if gid:
            out[gid] = str(r.get("name") or gid)
    return out, None


def metric_has_any_mts(token: str, realm: str, metric: str) -> tuple[bool, str | None]:
    """True if at least one MTS exists for ``metric:`` query (liveness proxy)."""
    esc = metric.replace("\\", "\\\\").replace('"', '\\"')
    q = f'metric:"{esc}"'
    payload, err = api_get(token, realm, "/v2/metrictimeseries", {"query": q, "limit": "1", "offset": "0"})
    if err:
        return False, err
    if not isinstance(payload, dict):
        return False, None
    res = payload.get("results")
    if isinstance(res, list) and len(res) > 0:
        return True, None
    return False, None


def _subsection_md(key: str, block: dict[str, Any], table_lines: list[str]) -> list[str]:
    c = DASHBOARD_HEALTH_CHECKLIST[key]
    desc = str(block.get("description") or c["description"]).strip()
    rec = str(block.get("recommendation") or c["recommendation"]).strip()
    out: list[str] = [f"### {c['title']}\n", "", desc, ""]
    out.extend(["### Results", "", *table_lines, "", "### Recommendation", "", rec, ""])
    return out


@dataclass
class DashboardHealthConfig:
    realm: str
    max_dashboards: int
    max_inactive_metric_checks: int
    sleep_s: float


def run_dashboard_health(token: str, cfg: DashboardHealthConfig) -> dict[str, Any]:
    group_map, group_err = load_dashboard_groups(token, cfg.realm)
    det_idx, det_err = load_detector_index(token, cfg.realm)

    all_dashes, derr = paginate_results(token, cfg.realm, "/v2/dashboard", page_size=100)
    if derr:
        return {
            "schema": STRUCTURED_SCHEMA,
            "realm": cfg.realm,
            "error": derr,
            "checks": {},
        }

    dash_total = len(all_dashes)
    # Stable order: name
    all_dashes.sort(key=lambda r: str(r.get("name") or r.get("id") or "").lower())
    dash_list = all_dashes[: cfg.max_dashboards]

    detector_link_rows: list[dict[str, Any]] = []
    inactive_chart_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []

    # Duplicate dashboards: (groupId, normalized name) -> [dashboard ids]
    name_buckets: dict[tuple[str, str], list[str]] = {}

    # Dedup metric -> has_mts for inactive check (cap unique API calls)
    metric_mts_cache: dict[str, bool | None] = {}
    metric_check_budget = max(0, cfg.max_inactive_metric_checks)

    for dr in dash_list:
        did = str(dr.get("id") or "").strip()
        if not did:
            continue
        dname = str(dr.get("name") or did)
        gid = str(dr.get("groupId") or dr.get("group_id") or dr.get("dashboardGroupId") or "").strip()
        gname = group_map.get(gid, gid or "—")

        key_nm = re.sub(r"\s+", " ", dname.strip().lower())
        name_buckets.setdefault((gid, key_nm), []).append(did)

        detail, gerr = api_get(token, cfg.realm, f"/v2/dashboard/{urllib.parse.quote(did, safe='')}", None)
        if gerr or not isinstance(detail, dict):
            if cfg.sleep_s > 0:
                time.sleep(cfg.sleep_s)
            continue

        charts = iter_chart_units(detail)
        for chart_id, chart_name, blob in charts:
            # Detector references
            for det_id in extract_detector_ids_from_text(blob):
                if det_err or not det_idx:
                    continue
                info = det_idx.get(det_id)
                if info is None:
                    detector_link_rows.append(
                        {
                            "color": "Yellow",
                            "dashboardGroup": gname,
                            "dashboardGroupId": gid,
                            "dashboardId": did,
                            "chartConfigId": chart_id,
                            "dashboardName": dname,
                            "chartName": chart_name,
                            "detectorLink": f"{det_id} (not found)",
                            "detectorId": det_id,
                        }
                    )
                elif info.get("disabled"):
                    detector_link_rows.append(
                        {
                            "color": "Yellow",
                            "dashboardGroup": gname,
                            "dashboardGroupId": gid,
                            "dashboardId": did,
                            "chartConfigId": chart_id,
                            "dashboardName": dname,
                            "chartName": chart_name,
                            "detectorLink": f"{det_id} (inactive/disabled)",
                            "detectorId": det_id,
                        }
                    )

            # Inactive metrics (sampled)
            for m in extract_metrics_from_text(blob):
                if m not in metric_mts_cache:
                    if metric_check_budget <= 0:
                        metric_mts_cache[m] = None  # unknown — skip row for this metric
                        continue
                    metric_check_budget -= 1
                    ok, _merr = metric_has_any_mts(token, cfg.realm, m)
                    metric_mts_cache[m] = ok
                    if cfg.sleep_s > 0:
                        time.sleep(cfg.sleep_s)
                alive = metric_mts_cache.get(m)
                if alive is False:
                    inactive_chart_rows.append(
                        {
                            "color": "Yellow",
                            "dashboardGroup": gname,
                            "dashboardGroupId": gid,
                            "dashboardId": did,
                            "chartConfigId": chart_id,
                            "dashboardName": dname,
                            "chartName": f"{chart_name} (metric: {m})",
                        }
                    )

        if cfg.sleep_s > 0:
            time.sleep(cfg.sleep_s)

    # Duplicates
    for (_gid, _nm), ids in name_buckets.items():
        u = sorted(set(ids), key=str.lower)
        if len(u) <= 1:
            continue
        canonical = u[0]
        dup_rest = ", ".join(u[1:9]) + ("…" if len(u) > 9 else "")
        # Resolve display name from first id
        meta = next((x for x in dash_list if str(x.get("id")) == canonical), {})
        dname = str(meta.get("name") or canonical)
        gid = str(meta.get("groupId") or meta.get("group_id") or "").strip()
        gname = group_map.get(gid, gid or "—")
        dup_list = u[1:9]
        duplicate_rows.append(
            {
                "color": "Yellow",
                "dashboardGroup": gname,
                "dashboardGroupId": gid,
                "dashboardId": canonical,
                "dashboardName": dname,
                "duplicateDashboardIds": dup_rest,
                "duplicateDashboardIdList": dup_list,
                "duplicateDashboardIdsHasMore": len(u) > 9,
            }
        )

    checks = {
        "detectorLinks": {
            "rows": detector_link_rows[:500],
            "description": DASHBOARD_HEALTH_CHECKLIST["detectorLinks"]["description"],
            "recommendation": DASHBOARD_HEALTH_CHECKLIST["detectorLinks"]["recommendation"],
        },
        "inactiveCharts": {
            "rows": inactive_chart_rows[:500],
            "description": DASHBOARD_HEALTH_CHECKLIST["inactiveCharts"]["description"],
            "recommendation": DASHBOARD_HEALTH_CHECKLIST["inactiveCharts"]["recommendation"],
        },
        "duplicateDashboards": {
            "rows": duplicate_rows[:500],
            "description": DASHBOARD_HEALTH_CHECKLIST["duplicateDashboards"]["description"],
            "recommendation": DASHBOARD_HEALTH_CHECKLIST["duplicateDashboards"]["recommendation"],
        },
    }

    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "dashboardListTotal": dash_total,
        "dashboardsAnalyzed": len(dash_list),
        "maxDashboardsCap": cfg.max_dashboards,
        "groupListError": group_err,
        "detectorIndexError": det_err,
        "checks": checks,
    }


def render_dashboards_checks_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Dashboards health checks`` section."""
    if not report or report.get("error"):
        return _dashboards_placeholder_markdown(str(report.get("error") or "") if report else "")

    realm = str(report.get("realm") or "")
    chk = report.get("checks") or {}

    parts: list[str] = ["## Dashboards health checks\n"]

    # --- Links to detectors ---
    dl = chk.get("detectorLinks") or {}
    dl_rows = sorted(dl.get("rows") or [], key=lambda r: (str(r.get("dashboardGroup")), str(r.get("dashboardName"))))
    d_lines = [
        "| Color | Dashboard Group | Dashboard Name | Chart Name | Detector Link |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in dl_rows:
        d_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('dashboardGroup')))} | "
            f"{_md_dashboard_name_cell(r, realm)} | {_md_cell(str(r.get('chartName')))} | "
            f"{_md_cell(str(r.get('detectorLink')))} |"
        )
    if not dl_rows:
        d_lines.append("|  |  |  |  |  |")
    parts.extend(_subsection_md("detectorLinks", dl, d_lines))

    # --- Inactive charts ---
    ic = chk.get("inactiveCharts") or {}
    ic_rows = sorted(ic.get("rows") or [], key=lambda r: (str(r.get("dashboardGroup")), str(r.get("dashboardName"))))
    i_lines = [
        "| Color | Dashboard Group | Dashboard Name | Chart Name |",
        "| --- | --- | --- | --- |",
    ]
    for r in ic_rows:
        i_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('dashboardGroup')))} | "
            f"{_md_dashboard_name_cell(r, realm)} | {_md_cell(str(r.get('chartName')))} |"
        )
    if not ic_rows:
        i_lines.append("|  |  |  |  |")
    block_ic = dict(ic)
    if ic.get("note"):
        block_ic = {**ic, "description": (ic.get("description") or "") + "\n\n" + str(ic.get("note"))}
    parts.extend(_subsection_md("inactiveCharts", block_ic, i_lines))

    # --- Duplicates ---
    dd = chk.get("duplicateDashboards") or {}
    dd_rows = sorted(dd.get("rows") or [], key=lambda r: str(r.get("dashboardName") or ""))
    dd_lines = [
        "| Color | Dashboard Group | Dashboard Name | Duplicate Dashboard IDs |",
        "| --- | --- | --- | --- |",
    ]
    for r in dd_rows:
        dd_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('dashboardGroup')))} | "
            f"{_md_dashboard_name_cell(r, realm)} | {_md_duplicate_dashboard_ids_cell(r, realm)} |"
        )
    if not dd_rows:
        dd_lines.append("|  |  |  |  |")
    parts.extend(_subsection_md("duplicateDashboards", dd, dd_lines))

    return "\n".join(parts)


def _dashboards_placeholder_markdown(err_hint: str) -> str:
    msg = "*None — check not executed.*"
    if err_hint.strip():
        msg = f"*Dashboard automation failed ({_md_cell(err_hint[:200])}).*"
    layout = [
        (
            "detectorLinks",
            "| Color | Dashboard Group | Dashboard Name | Chart Name | Detector Link |",
            "| --- | --- | --- | --- | --- |",
            "|  |  |  |  |  |",
        ),
        (
            "inactiveCharts",
            "| Color | Dashboard Group | Dashboard Name | Chart Name |",
            "| --- | --- | --- | --- |",
            "|  |  |  |  |",
        ),
        (
            "duplicateDashboards",
            "| Color | Dashboard Group | Dashboard Name | Duplicate Dashboard IDs |",
            "| --- | --- | --- | --- |",
            "|  |  |  |  |",
        ),
    ]
    parts: list[str] = ["## Dashboards health checks\n"]
    for key, h, sep, empty in layout:
        c = DASHBOARD_HEALTH_CHECKLIST[key]
        block = {"description": c["description"], "recommendation": msg}
        parts.extend(_subsection_md(key, block, [h, sep, empty]))
    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description="Dashboards health check (Splunk Observability API).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--max-dashboards", type=int, default=150, help="Max dashboards to fetch full detail for (default 150).")
    p.add_argument(
        "--max-inactive-metric-checks",
        type=int,
        default=80,
        help="Max distinct metrics to check via MTS search (default 80).",
    )
    p.add_argument("--sleep", type=float, default=0.05, help="Seconds between API bursts (default 0.05).")
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON for o11y_health_check_run.py.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section (## Dashboards health checks).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Dashboards health check starting (dashboard groups + dashboard detail APIs)")

    token = (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    realm = (args.realm or os.environ.get("SPLUNK_REALM") or "").strip()
    profile_path = resolve_profile_path(args.profile)
    if profile_path:
        prof = load_customer_profile_scalars(profile_path)
        token = token or (prof.get("access_token") or "").strip()
        realm = realm or (prof.get("realm") or "").strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1
    if not realm:
        realm = "us0"

    logger.info("Realm: %s | max_dashboards: %s", realm, max(1, min(args.max_dashboards, 5000)))

    cfg = DashboardHealthConfig(
        realm=realm,
        max_dashboards=max(1, min(args.max_dashboards, 5000)),
        max_inactive_metric_checks=max(0, min(args.max_inactive_metric_checks, 500)),
        sleep_s=max(0.0, float(args.sleep)),
    )
    report = run_dashboard_health(token, cfg)

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_dashboards_checks_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    if report.get("error"):
        logger.error("Report error: %s", str(report.get("error"))[:500])
        print(json.dumps(report, indent=2))
        return 1
    brief = {k: report[k] for k in ("schema", "realm", "dashboardListTotal", "dashboardsAnalyzed") if k in report}
    print(json.dumps(brief, indent=2))
    logger.info("Done: analyzed %s of %s dashboards", brief.get("dashboardsAnalyzed"), brief.get("dashboardListTotal"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
