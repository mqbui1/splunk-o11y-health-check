#!/usr/bin/env python3
"""
Splunk Observability Cloud — Real User Monitoring (RUM) health check (read-only).

Uses SignalFlow on **sf.org** RUM subscription and usage metrics (same family as
``License_utilizations.md`` / ``o11y_license_utilization.py``), aligned with
``Splunk-Observability-Health-Check.md`` **Real User Monitoring (RUM) Health Checks**.

**Volume by application** and **RUM MMS** rows are derived when ``sf.org.rum.numSessions`` /
``sf.org.numRumMonitoringMetricSetMetrics`` return usable **by-dimension** series (typically
``sf_key`` / org dimensions). Otherwise the script emits **Findings** explaining the gap while
keeping checklist-shaped **Results** tables.

**Not automated here** (UI or future APIs): per-session bot IP lists, custom event names with
true cardinality, RUM TMS license splits — those checks return explicit **Findings** instead of
invented data.

Environment: ``SPLUNK_ACCESS_TOKEN`` / profile ``access_token``; ``realm`` in profile or
``SPLUNK_REALM``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import (  # noqa: E402
    execute_signalflow_matrix,
    execute_signalflow_time_series,
    load_customer_profile_scalars,
    resolve_profile_path,
    severity,
)
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_rum_health/v1"

logger = logging.getLogger(__name__)

RUM_CHECKLIST: dict[str, dict[str, str]] = {
    "volumeByApplication": {
        "title": "Volume by Application",
        "description": "List of applications by total number of sessions.",
        "recommendation": (
            "Review to identify if any application is generating a disproportionate number of sessions. "
            "Review the environment for the application; if this is a dev/test application, should it be "
            "sending RUM data?"
        ),
    },
    "filterBotTraffic": {
        "title": "Filter Synthetic/Bot traffic",
        "description": "Ensure crawler and bot sessions aren't being ingested as real user sessions.",
        "recommendation": (
            "Check RUM instrumentation and enable the disableBots flag to stop tracing data from known bots."
        ),
    },
    "reviewCustomEvents": {
        "title": "Review Custom Events",
        "description": (
            "Are teams sending excessive custom RUM events that provide little analytical value. "
            "Compare this against your RUM session volume entitlement."
        ),
        "recommendation": (
            "Review each custom event and its cardinality to determine if it provides value. "
            "Remove the low value custom events."
        ),
    },
    "rumTms": {
        "title": "RUM Troubleshooting Metrics Sets (TMS) Usage Analysis",
        "description": "Provide a list of TMS by license usage %.",
        "recommendation": (
            "Review TMS usage in the Splunk Observability RUM and usage views; consolidate or archive "
            "high-cardinality sets where appropriate."
        ),
    },
    "rumMms": {
        "title": "RUM Monitoring Metrics Sets (MMS) Usage Analysis",
        "description": (
            "Provide a list of MMS by license usage %, number of services, endpoints enabled."
        ),
        "recommendation": "Determine if the MMS can be converted into TMS to reduce license usage.",
    },
}

# Try coarser splits first (fewer MTS), then finer — stop at first program that returns series.
_RUM_SESSION_PROGRAMS: tuple[str, ...] = (
    "data('sf.org.rum.numSessions').mean(by=['sf_organization_id']).publish(label='rum_sessions')",
    "data('sf.org.rum.numSessions').mean(by=['sf_organization_id', 'sf_key']).publish(label='rum_sessions')",
)

_RUM_MMS_PROGRAMS: tuple[str, ...] = (
    "data('sf.org.numRumMonitoringMetricSetMetrics').mean(by=['sf_organization_id']).publish(label='rum_mms')",
    "data('sf.org.numRumMonitoringMetricSetMetrics').mean(by=['sf_organization_id', 'sf_key']).publish(label='rum_mms')",
)


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _stream_url(realm: str) -> str:
    return f"https://stream.{(realm or '').strip()}.signalfx.com"


def _mean_time_series_value(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
) -> tuple[float | None, str | None]:
    """Single aggregate: mean of all numeric points returned (org-wide gauges)."""
    pts, err = execute_signalflow_time_series(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=120.0,
        read_timeout=90.0,
        max_points=20_000,
    )
    if err:
        logger.warning("SignalFlow scalar program failed: %s", err[:300])
        return None, err
    vals = [float(v) for _, v in pts if isinstance(v, (int, float))]
    if not vals:
        return None, "no_points"
    return sum(vals) / len(vals), None


def _aggregate_max_per_tsid(
    meta: dict[str, dict[str, Any]],
    dps: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any], float]]:
    best: dict[str, float] = {}
    for pt in dps:
        tid = pt.get("tsId")
        if tid is None:
            continue
        ts = str(tid)
        v = pt.get("value")
        if not isinstance(v, (int, float)):
            continue
        fv = float(v)
        if ts not in best:
            best[ts] = fv
        else:
            best[ts] = max(best[ts], fv)
    out: list[tuple[str, dict[str, Any], float]] = []
    for ts, mx in best.items():
        props = meta.get(ts) or {}
        if not isinstance(props, dict):
            props = {}
        out.append((ts, props, mx))
    return out


def _app_label_from_props(props: dict[str, Any]) -> str:
    """Best-effort application label from SignalFlow metadata properties."""
    candidates = (
        "app",
        "application",
        "application_name",
        "app.name",
        "rum.application",
        "sf_key",
        "sfKey",
        "service",
        "deployment.environment",
        "deployment_environment",
    )
    for k in candidates:
        raw = props.get(k)
        if raw is None:
            raw = props.get(k.replace(".", "_"))
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:240]
    return "—"


def _mms_name_from_props(props: dict[str, Any]) -> str:
    sk = props.get("sf_key") or props.get("sfKey")
    if isinstance(sk, str) and sk.strip():
        return sk.strip()[:240]
    return "RUM MMS"


def _run_matrix_program(
    *,
    stream_url: str,
    token: str,
    program: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    label: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], str | None]:
    logger.debug("SignalFlow %s: %s", label, program[:200])
    meta, dps, err, stop = execute_signalflow_matrix(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=180.0,
        read_timeout=120.0,
        max_data_points=120_000,
    )
    if err:
        logger.warning("SignalFlow %s failed: %s", label, err[:400])
    elif stop:
        logger.info("SignalFlow %s stop_reason=%s (points=%s)", label, stop, len(dps))
    else:
        logger.info("SignalFlow %s OK (series=%s, points=%s)", label, len(meta), len(dps))
    return meta, dps, err


def _fetch_volume_rows(
    *,
    stream_url: str,
    token: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    subscription_mean: float | None,
    max_rows: int,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    findings: list[str] = []
    last_err: str | None = None
    for program in _RUM_SESSION_PROGRAMS:
        meta, dps, err = _run_matrix_program(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=start_ms,
            stop_ms=stop_ms,
            resolution_ms=resolution_ms,
            label="rum_sessions_matrix",
        )
        if err:
            last_err = err
            continue
        agg = _aggregate_max_per_tsid(meta, dps)
        if not agg:
            last_err = "no_series"
            continue
        rows: list[dict[str, Any]] = []
        for _tid, props, mx in sorted(agg, key=lambda x: -x[2])[: max(1, max_rows)]:
            app = _app_label_from_props(props)
            pct: float | None = None
            if subscription_mean is not None and subscription_mean > 0:
                pct = round(100.0 * mx / subscription_mean, 2)
            sev = severity(pct) if pct is not None else "Yellow"
            rows.append(
                {
                    "color": sev or "Yellow",
                    "applicationName": app,
                    "numberOfSessions": int(round(mx)) if mx >= 0 else 0,
                    "licenseUtilizationPct": pct,
                }
            )
        if len(agg) == 1 and rows and rows[0].get("applicationName") == "—":
            findings.append(
                "Session usage is only available as an organization aggregate in this window — "
                "per-application splits require additional dimensions or RUM UI export."
            )
        return rows, findings, None

    if last_err:
        findings.append(f"Could not load session usage from SignalFlow ({last_err[:200]}).")
    return [], findings, last_err


def _fetch_mms_rows(
    *,
    stream_url: str,
    token: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    mms_limit_mean: float | None,
    max_rows: int,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    findings: list[str] = []
    last_err: str | None = None
    for program in _RUM_MMS_PROGRAMS:
        meta, dps, err = _run_matrix_program(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=start_ms,
            stop_ms=stop_ms,
            resolution_ms=resolution_ms,
            label="rum_mms_matrix",
        )
        if err:
            last_err = err
            continue
        agg = _aggregate_max_per_tsid(meta, dps)
        if not agg:
            last_err = "no_series"
            continue
        rows: list[dict[str, Any]] = []
        for _tid, props, mx in sorted(agg, key=lambda x: -x[2])[: max(1, max_rows)]:
            app = _app_label_from_props(props)
            mms_name = _mms_name_from_props(props)
            card = int(round(mx)) if mx >= 0 else 0
            pct: float | None = None
            if mms_limit_mean is not None and mms_limit_mean > 0:
                pct = round(100.0 * mx / mms_limit_mean, 2)
            sev = severity(pct) if pct is not None else "Yellow"
            rows.append(
                {
                    "color": sev or "Yellow",
                    "applicationName": app,
                    "mmsName": mms_name,
                    "mmsCardinality": card,
                    "licensePct": pct,
                }
            )
        return rows, findings, None

    if last_err:
        findings.append(f"Could not load RUM MMS usage from SignalFlow ({last_err[:200]}).")
    return [], findings, last_err


@dataclass
class RumHealthConfig:
    realm: str
    lookback_hours: int
    resolution_minutes: int
    max_rows_per_check: int


def run_rum_health(token: str, cfg: RumHealthConfig) -> dict[str, Any]:
    stream_url = _stream_url(cfg.realm)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, min(cfg.lookback_hours, 720)) * 3600 * 1000
    resolution_ms = max(60_000, cfg.resolution_minutes * 60_000)

    sub_sessions, e_sub = _mean_time_series_value(
        stream_url=stream_url,
        token=token,
        program="data('sf.org.rum.subscription.sessionsPerMonth').mean().publish(label='rum_sub_sessions')",
        start_ms=start_ms,
        stop_ms=now_ms,
        resolution_ms=resolution_ms,
    )
    if e_sub and e_sub != "no_points":
        logger.warning("RUM session subscription query: %s", e_sub[:300])
    mms_limit, e_lim = _mean_time_series_value(
        stream_url=stream_url,
        token=token,
        program="data('sf.org.rum.limit.monitoringMetricSets').mean().publish(label='rum_mms_limit')",
        start_ms=start_ms,
        stop_ms=now_ms,
        resolution_ms=resolution_ms,
    )
    if e_lim and e_lim != "no_points":
        logger.warning("RUM MMS limit query: %s", e_lim[:300])

    vol_rows, vol_findings, vol_err = _fetch_volume_rows(
        stream_url=stream_url,
        token=token,
        start_ms=start_ms,
        stop_ms=now_ms,
        resolution_ms=resolution_ms,
        subscription_mean=sub_sessions,
        max_rows=cfg.max_rows_per_check,
    )
    if not vol_rows and not vol_findings:
        vol_findings.append(
            "No RUM session usage series returned for the configured window — confirm RUM is licensed "
            "and sf.org.rum.numSessions is present for this org."
        )

    mms_rows, mms_findings, _mms_err = _fetch_mms_rows(
        stream_url=stream_url,
        token=token,
        start_ms=start_ms,
        stop_ms=now_ms,
        resolution_ms=resolution_ms,
        mms_limit_mean=mms_limit,
        max_rows=cfg.max_rows_per_check,
    )
    if not mms_rows and not mms_findings:
        mms_findings.append(
            "No RUM MMS usage series returned — confirm sf.org.numRumMonitoringMetricSetMetrics exists, "
            "or review MMS in the RUM / usage UI."
        )

    bot_findings = [
        "Bot and synthetic session detail (per-app IP lists) is not available from the SignalFlow sources "
        "used here — assess crawler traffic in the Splunk Observability RUM UI and enable **disableBots** "
        "on applications where appropriate."
    ]
    custom_findings = [
        "Custom RUM event names and per-event cardinality are not queried by this automation — review "
        "custom events in the RUM UI against session entitlement."
    ]
    tms_findings = [
        "RUM Troubleshooting Metric Sets (TMS) are not represented as a separate sf.org usage series in "
        "this automation — evaluate TMS in RUM settings and license views."
    ]

    checks: dict[str, Any] = {
        "volumeByApplication": {
            "rows": vol_rows,
            "findings": vol_findings,
            "description": RUM_CHECKLIST["volumeByApplication"]["description"],
            "recommendation": RUM_CHECKLIST["volumeByApplication"]["recommendation"],
        },
        "filterBotTraffic": {
            "rows": [],
            "findings": bot_findings,
            "description": RUM_CHECKLIST["filterBotTraffic"]["description"],
            "recommendation": RUM_CHECKLIST["filterBotTraffic"]["recommendation"],
        },
        "reviewCustomEvents": {
            "rows": [],
            "findings": custom_findings,
            "description": RUM_CHECKLIST["reviewCustomEvents"]["description"],
            "recommendation": RUM_CHECKLIST["reviewCustomEvents"]["recommendation"],
        },
        "rumTms": {
            "rows": [],
            "findings": tms_findings,
            "description": RUM_CHECKLIST["rumTms"]["description"],
            "recommendation": RUM_CHECKLIST["rumTms"]["recommendation"],
        },
        "rumMms": {
            "rows": mms_rows,
            "findings": mms_findings,
            "description": RUM_CHECKLIST["rumMms"]["description"],
            "recommendation": RUM_CHECKLIST["rumMms"]["recommendation"],
        },
    }

    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "lookbackHours": cfg.lookback_hours,
        "resolutionMinutes": cfg.resolution_minutes,
        "subscriptionSessionsPerMonthEstimate": sub_sessions,
        "rumMmsLimitEstimate": mms_limit,
        "signalFlowNotes": {
            "sessionSubscriptionError": None if sub_sessions is not None else (e_sub or "missing"),
            "mmsLimitError": None if mms_limit is not None else (e_lim or "missing"),
            "volumeMatrixError": vol_err,
        },
        "checks": checks,
    }


def render_rum_checks_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Real User Monitoring (RUM) health checks`` section (matches consolidated report casing)."""
    # Use ``report is None`` — ``not {}`` would mis-treat an empty dict as missing.
    if report is None or report.get("error"):
        err = str(report.get("error") or "") if report is not None else ""
        msg = "*None — check not executed.*"
        if err.strip():
            msg = f"*RUM health check failed ({_md_cell(err[:220])}).*"
        parts_err = [
            "## Real User Monitoring (RUM) health checks\n\n",
            f"### {RUM_CHECKLIST['volumeByApplication']['title']}\n\n",
            RUM_CHECKLIST["volumeByApplication"]["description"] + "\n\n",
            "### Results\n\n",
            "| Color | Application Name | Number of Sessions | % Utilization of License |\n",
            "| --- | --- | --- | --- |\n",
            "|  |  |  |  |\n\n",
            "### Findings\n\n",
            f"{msg}\n\n",
            "### Recommendation\n\n",
            RUM_CHECKLIST["volumeByApplication"]["recommendation"] + "\n\n",
        ]
        return "".join(parts_err)

    chk = report.get("checks") or {}

    def _block(
        key: str,
        table_lines: list[str],
        findings: list[str],
        rec_override: str | None = None,
    ) -> list[str]:
        c = RUM_CHECKLIST[key]
        block = chk.get(key) or {}
        desc = str(block.get("description") or c["description"]).strip()
        rec = str(rec_override or block.get("recommendation") or c["recommendation"]).strip()
        fins = list(block.get("findings") or findings)
        out: list[str] = [
            f"### {c['title']}\n\n",
            desc,
            "\n\n### Results\n\n",
            *table_lines,
            "\n### Findings\n\n",
        ]
        if fins:
            for f in fins:
                out.append(f"- {_md_cell(f)}\n")
        else:
            out.append("*No findings.*\n")
        out.extend(["\n### Recommendation\n\n", rec, "\n\n"])
        return out

    lines: list[str] = ["## Real User Monitoring (RUM) health checks\n\n"]

    vb = chk.get("volumeByApplication") or {}
    vb_rows = list(vb.get("rows") or [])

    def _session_sort_key(row: dict[str, Any]) -> float:
        v = row.get("numberOfSessions")
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    vb_rows.sort(key=_session_sort_key, reverse=True)

    vb_table = [
        "| Color | Application Name | Number of Sessions | % Utilization of License |\n",
        "| --- | --- | --- | --- |\n",
    ]
    for r in vb_rows:
        lic = r.get("licenseUtilizationPct")
        lic_s = "—" if lic is None else f"{float(lic):.2f}%"
        vb_table.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('applicationName')))} | "
            f"{_md_cell(str(r.get('numberOfSessions')))} | {_md_cell(lic_s)} |\n"
        )
    if not vb_rows:
        vb_table.append("|  |  |  |  |\n")
    lines.extend(_block("volumeByApplication", vb_table, []))

    fb = chk.get("filterBotTraffic") or {}
    fb_table = [
        "| Color | Application Name | IP Addresses likely to be bots or crawlers |\n",
        "| --- | --- | --- |\n",
        "|  |  |  |\n",
    ]
    lines.extend(_block("filterBotTraffic", fb_table, list(fb.get("findings") or [])))

    ce = chk.get("reviewCustomEvents") or {}
    ce_table = [
        "| Color | Application Name | Custom Events | Cardinality of Event |\n",
        "| --- | --- | --- | --- |\n",
        "|  |  |  |  |\n",
    ]
    lines.extend(_block("reviewCustomEvents", ce_table, list(ce.get("findings") or [])))

    rt = chk.get("rumTms") or {}
    rt_table = [
        "| Color | Application Name | TMS Name | TMS Cardinality | % of License |\n",
        "| --- | --- | --- | --- | --- |\n",
        "|  |  |  |  |  |\n",
    ]
    lines.extend(_block("rumTms", rt_table, list(rt.get("findings") or [])))

    rm = chk.get("rumMms") or {}
    rm_rows = list(rm.get("rows") or [])
    rm_table = [
        "| Color | Application Name | MMS Name | MMS Cardinality | % of License |\n",
        "| --- | --- | --- | --- | --- |\n",
    ]
    for r in rm_rows:
        lp = r.get("licensePct")
        lp_s = "—" if lp is None else f"{float(lp):.2f}%"
        rm_table.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('applicationName')))} | "
            f"{_md_cell(str(r.get('mmsName')))} | {_md_cell(str(r.get('mmsCardinality')))} | {_md_cell(lp_s)} |\n"
        )
    if not rm_rows:
        rm_table.append("|  |  |  |  |  |\n")
    lines.extend(_block("rumMms", rm_table, []))

    return "".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Splunk Observability RUM health check (SignalFlow sf.org metrics).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument(
        "--lookback-hours",
        type=int,
        default=168,
        help="SignalFlow window in hours (default 168 = 7d; max 720).",
    )
    p.add_argument(
        "--resolution-minutes",
        type=int,
        default=360,
        help="Rollup resolution in minutes (default 360 = 6h).",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=200,
        help="Max rows per volumetric table (default 200).",
    )
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section (## RUM…).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("RUM health check starting (SignalFlow: sf.org RUM metrics)")

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

    cfg = RumHealthConfig(
        realm=realm,
        lookback_hours=max(1, min(int(args.lookback_hours), 720)),
        resolution_minutes=max(1, min(int(args.resolution_minutes), 1440)),
        max_rows_per_check=max(10, min(int(args.max_rows), 2000)),
    )
    logger.info(
        "Realm=%s lookback_hours=%s resolution_minutes=%s max_rows=%s",
        cfg.realm,
        cfg.lookback_hours,
        cfg.resolution_minutes,
        cfg.max_rows_per_check,
    )

    exit_code = 0
    try:
        report = run_rum_health(token, cfg)
    except Exception:
        logger.exception("RUM health check crashed")
        report = {
            "schema": STRUCTURED_SCHEMA,
            "realm": realm,
            "error": "run_rum_health raised an exception (see stderr log)",
            "checks": {},
        }
        exit_code = 1

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_rum_checks_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    n_vol = len(((report.get("checks") or {}).get("volumeByApplication") or {}).get("rows") or [])
    n_mms = len(((report.get("checks") or {}).get("rumMms") or {}).get("rows") or [])
    logger.info("Done: volume_rows=%s mms_rows=%s exit=%s", n_vol, n_mms, exit_code)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
