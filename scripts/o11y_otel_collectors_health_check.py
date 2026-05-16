#!/usr/bin/env python3
"""
Splunk Observability Cloud — OpenTelemetry Collectors inventory (read-only).

Uses SignalFlow on internal ``otelcol_*`` metrics (default: ``otelcol_process_uptime``) with
``host.name`` and optional resource dimensions (``host.id``, ``cloud.*``, ``k8s.*``,
``deployment.environment``, ``os.type``, ``service.*``) when present on the MTS, aligned with
``Splunk-Observability-Health-Check.md`` **OpenTelemetry Collectors**.

**Depreciation Date** (Splunk-distro collectors, inferred from ``service.name``): optional catalog from
`GitHub <https://github.com/signalfx/splunk-otel-collector/releases>`_ — each ``vX.Y.Z`` release’s
``published_at`` plus **support** window (default **730 days**, ~2 years). If the catalog cannot be
fetched or the version is unknown, the cell shows **—** and color falls back to semver vs minimums.
"""

from __future__ import annotations

import argparse
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import execute_signalflow_matrix, load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_otel_collectors_health/v1"

logger = logging.getLogger(__name__)

OTEL_CHECKLIST: dict[str, dict[str, str]] = {
    "collectorsByVersion": {
        "title": "List of Collectors by version",
        "description": (
            "List of deployed OpenTelemetry Collectors and their version. "
            "When present on the metric time series, **Host ID** and **Deployment context** "
            "reflect OpenTelemetry resource attributes (for example `host.id`, cloud region, "
            "Kubernetes namespace or pod name, deployment environment).\n\n"
            "Yellow – Collector version 30 – 90 days from deprecation/support  \n"
            "Red – Collector version less than 30 days deprecation/support"
        ),
        "recommendation": "Review collectors that are near or have already reached their deprecated date.",
    },
}

_DEFAULT_PROGRAMS: tuple[str, ...] = (
    # Richest rollup first (fails if dimensions are absent from the metric in this org).
    (
        "data('otelcol_process_uptime').mean(by=["
        "'host.name','host.id','service.name','service.instance.id','service.version',"
        "'cloud.provider','cloud.platform','cloud.region','k8s.cluster.name',"
        "'k8s.namespace.name','k8s.pod.name','deployment.environment','os.type'"
        "]).publish(label='otel_uptime')"
    ),
    (
        "data('otelcol_process_uptime')"
        ".mean(by=['host.name','host.id','service.name','service.instance.id','service.version'])"
        ".publish(label='otel_uptime')"
    ),
    (
        "data('otelcol_process_uptime')"
        ".mean(by=['host.name', 'service.name', 'service.instance.id', 'service.version'])"
        ".publish(label='otel_uptime')"
    ),
    (
        "data('otelcol_process_uptime')"
        ".mean(by=['host.name', 'service.name', 'service.version']).publish(label='otel_uptime')"
    ),
    (
        "data('otelcol_process_uptime').mean(by=['host.name', 'service.version']).publish(label='otel_uptime')"
    ),
)


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def parse_semver_prefix(ver: str) -> tuple[int, int, int] | None:
    v = (ver or "").strip()
    if not v:
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        m2 = re.match(r"^(\d+)\.(\d+)", v)
        if not m2:
            return None
        return (int(m2.group(1)), int(m2.group(2)), 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def semver_lt(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return a < b


def collector_list_by_version_sort_key(r: dict[str, Any]) -> tuple:
    """Oldest semver first; ties broken by host, host id, collector name; non-semver versions last."""
    ver = str(r.get("version") or "")
    pv = parse_semver_prefix(ver.lstrip("vV"))
    host = str(r.get("hostName") or "")
    hid = str(r.get("hostId") or "")
    cname = str(r.get("collectorName") or "")
    if pv is None:
        return (1, ver, host, hid, cname)
    return (0, pv, host, hid, cname)


def service_name_from_collector_display(cname: str) -> str:
    """First segment of ``service.name / service.instance.id`` display."""
    s = (cname or "").strip()
    if " / " in s:
        return s.split(" / ", 1)[0].strip()
    return s


def _deployment_context_richness(ctx: str) -> int:
    c = (ctx or "").strip()
    return 0 if not c or c == "—" else len(c)


SPLUNK_OTEL_GITHUB = "signalfx/splunk-otel-collector"
SPLUNK_OTEL_RELEASES_API = f"https://api.github.com/repos/{SPLUNK_OTEL_GITHUB}/releases"


def normalize_collector_version_key(ver: str) -> str | None:
    """``v0.147.0`` / ``0.147.0`` / ``0.147`` → ``0.147.0`` for lookup."""
    v = (ver or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", v)
    if not m:
        return None
    patch = int(m.group(3)) if m.group(3) is not None else 0
    return f"{int(m.group(1))}.{int(m.group(2))}.{patch}"


def _parse_github_iso_date(iso: str) -> date | None:
    s = (iso or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError, OSError):
        return None


def fetch_splunk_otel_release_publish_dates(
    *,
    max_pages: int = 25,
    per_page: int = 100,
    github_token: str | None = None,
) -> tuple[dict[str, date], str | None]:
    """
    GitHub Releases API: each release ``tag_name`` like ``v0.150.0`` → ``published_at`` date (UTC).

    Returns mapping **normalized semver** (``0.150.0``) -> **release** date (first occurrence per
    page order: newest first; one tag per version expected).
    """
    out: dict[str, date] = {}
    tag_ok = re.compile(r"^v?\d+\.\d+\.\d+$", re.IGNORECASE)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "o11y-health-check-otel/1.0",
    }
    gt = (github_token or os.environ.get("GITHUB_TOKEN") or "").strip()
    if gt:
        headers["Authorization"] = f"Bearer {gt}"

    for page in range(1, max_pages + 1):
        q = urllib.parse.urlencode({"per_page": str(per_page), "page": str(page)})
        url = f"{SPLUNK_OTEL_RELEASES_API}?{q}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = (e.read() or b"").decode("utf-8", errors="replace")[:500]
            return out, f"GitHub HTTP {e.code}: {body}"
        except (OSError, http.client.IncompleteRead) as e:
            return out, str(e)
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError as e:
            return out, f"GitHub invalid JSON: {e}"
        if not isinstance(batch, list) or not batch:
            break
        for rel in batch:
            if not isinstance(rel, dict):
                continue
            tag = str(rel.get("tag_name") or "").strip()
            if not tag_ok.match(tag):
                continue
            pub = rel.get("published_at")
            if not isinstance(pub, str):
                continue
            d = _parse_github_iso_date(pub)
            if d is None:
                continue
            vk = normalize_collector_version_key(tag)
            if vk and vk not in out:
                out[vk] = d
        if len(batch) < per_page:
            break

    return out, None


def depreciation_date_iso(
    release_date: date,
    *,
    support_days: int,
) -> str:
    dep = release_date + timedelta(days=max(0, support_days))
    return dep.strftime("%Y-%m-%d")


def color_from_days_until_depreciation(days_left: int) -> str:
    """Checklist: Red < 30d; Yellow 30–90d; Green > 90d; past due → Red."""
    if days_left < 0:
        return "Red"
    if days_left <= 30:
        return "Red"
    if days_left <= 90:
        return "Yellow"
    return "Green"


def infer_distribution(service_name: str, _version: str) -> str:
    """Internal only: Splunk distro vs upstream collector vs unknown (depreciation / semver policy)."""
    s = (service_name or "").strip().lower()
    if not s:
        return "Unknown"
    if "splunk" in s or "signalfx" in s:
        return "Splunk"
    if "otelcol" == s or s.startswith("otelcol") or "opentelemetry-collector" in s:
        return "OSS"
    return "Unknown"


def _prop_str(props: dict[str, Any], *keys: str) -> str:
    """First non-empty string; tries dotted and underscored Splunk dimension spellings."""
    for k in keys:
        if not k:
            continue
        for variant in (k, k.replace(".", "_")):
            v = props.get(variant)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def host_id_from_props(props: dict[str, Any]) -> str:
    return _prop_str(props, "host.id", "host_id") or "—"


def host_deployment_context_from_props(props: dict[str, Any]) -> str:
    """
    Compact host-adjacent context from OTel resource attributes when present on the MTS:
    cloud provider/region, K8s cluster/namespace/pod, deployment environment, OS family.
    """
    parts: list[str] = []
    cp = _prop_str(props, "cloud.provider", "cloud_provider")
    plat = _prop_str(props, "cloud.platform", "cloud_platform")
    reg = _prop_str(
        props,
        "cloud.region",
        "cloud_region",
        "cloud.availability_zone",
        "cloud_availability_zone",
    )
    cloud_bits = " ".join(x for x in (cp, plat, reg) if x)
    if cloud_bits.strip():
        parts.append(cloud_bits.strip())

    dep_env = _prop_str(props, "deployment.environment", "deployment_environment")
    if dep_env:
        parts.append(f"env:{dep_env}")

    kcl = _prop_str(props, "k8s.cluster.name", "k8s_cluster_name")
    kns = _prop_str(props, "k8s.namespace.name", "k8s_namespace_name")
    kpod = _prop_str(props, "k8s.pod.name", "k8s_pod_name")
    k8s_fragments: list[str] = []
    if kcl:
        k8s_fragments.append(f"cluster:{kcl}")
    if kns:
        k8s_fragments.append(f"ns:{kns}")
    if kpod:
        k8s_fragments.append(kpod if len(kpod) <= 56 else kpod[:53] + "…")
    if k8s_fragments:
        parts.append(" ".join(k8s_fragments))

    ost = _prop_str(props, "os.type", "os_type")
    if ost:
        parts.append(f"os:{ost}")

    out = " · ".join(parts) if parts else "—"
    if len(out) > 240:
        return out[:237] + "…"
    return out


def collector_display_name(props: dict[str, Any]) -> str:
    sn = props.get("service.name") or props.get("service_name")
    si = props.get("service.instance.id") or props.get("service_instance_id") or props.get("service.instance")
    parts: list[str] = []
    if isinstance(sn, str) and sn.strip():
        parts.append(sn.strip())
    if isinstance(si, str) and si.strip() and si.strip() != (sn or "").strip():
        parts.append(si.strip())
    return " / ".join(parts) if parts else "—"


def host_from_props(props: dict[str, Any]) -> str:
    h = _prop_str(props, "host.name", "host_name", "host")
    return h if h else "—"


def version_from_props(props: dict[str, Any]) -> str:
    v = _prop_str(props, "service.version", "service_version", "version")
    return v if v else "—"


def fetch_otel_inventory(
    token: str,
    realm: str,
    *,
    lookback_hours: int,
    resolution_ms: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Returns one row per distinct MTS (host + service identity + version) with max observed uptime value.
    """
    stream_url = f"https://stream.{realm}.signalfx.com"
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, lookback_hours) * 3600 * 1000
    res = max(60_000, int(resolution_ms))

    # Try successive SignalFlow programs (dimension sets differ by deployment).
    last_err: str | None = None
    for idx, program in enumerate(_DEFAULT_PROGRAMS, start=1):
        logger.debug(
            "SignalFlow otelcol query %s/%s (window %sh, resolution %sms)",
            idx,
            len(_DEFAULT_PROGRAMS),
            lookback_hours,
            res,
        )
        meta, dps, err, _sr = execute_signalflow_matrix(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=start_ms,
            stop_ms=now_ms,
            resolution_ms=res,
            wall_seconds=180.0,
            read_timeout=120.0,
            max_data_points=250_000,
        )
        if err:
            last_err = err
            logger.warning("SignalFlow program %s/%s failed: %s", idx, len(_DEFAULT_PROGRAMS), err[:400])
            continue
        per_tsid: dict[str, float] = defaultdict(float)
        for pt in dps:
            tid = pt.get("tsId")
            if tid is None:
                continue
            v = pt.get("value")
            if isinstance(v, (int, float)):
                per_tsid[str(tid)] = max(per_tsid[str(tid)], float(v))

        rows: list[dict[str, Any]] = []
        for tsid, uptime in per_tsid.items():
            props = meta.get(tsid) or {}
            if not isinstance(props, dict):
                props = {}
            host = host_from_props(props)
            ver = version_from_props(props)
            cname = collector_display_name(props)
            # Capture service.instance.id for dedup key so multiple collectors on
            # the same host (e.g. agent + gateway) are not collapsed into one row.
            inst_id = _prop_str(props, "service.instance.id", "service_instance_id") or ""
            rows.append(
                {
                    "hostName": host,
                    "hostId": host_id_from_props(props),
                    "hostContext": host_deployment_context_from_props(props),
                    "collectorName": cname,
                    "version": ver,
                    "uptimeSample": uptime,
                    "_instanceId": inst_id,
                }
            )
        if rows:
            # Dedup key: (host, hostId, instanceId, version) — keeps distinct
            # collector instances on the same host separate.
            merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for r in rows:
                k = (
                    str(r.get("hostName") or ""),
                    str(r.get("hostId") or ""),
                    str(r.get("_instanceId") or ""),
                    str(r.get("version") or ""),
                )
                prev = merged.get(k)
                if prev is None:
                    merged[k] = r
                    continue
                ru = float(r.get("uptimeSample") or 0)
                pu = float(prev.get("uptimeSample") or 0)
                cr = _deployment_context_richness(str(r.get("hostContext") or ""))
                cp = _deployment_context_richness(str(prev.get("hostContext") or ""))
                if ru > pu or (ru == pu and cr > cp):
                    merged[k] = r
            deduped = list(merged.values())
            # Strip internal field before returning
            for r in deduped:
                r.pop("_instanceId", None)
            logger.info(
                "OTel inventory OK (program %s/%s): %s datapoint(s), %s deduped collector row(s)",
                idx,
                len(_DEFAULT_PROGRAMS),
                len(dps),
                len(deduped),
            )
            return deduped, None

    msg = last_err or "no_otel_metrics"
    logger.error("No otelcol_process_uptime inventory: %s", msg[:500])
    return [], msg


def classify_row(
    *,
    version: str,
    distribution: str,
    min_splunk: tuple[int, int, int],
    min_oss: tuple[int, int, int],
    depreciation_date_iso_str: str | None,
    today: date | None = None,
) -> str:
    """
    Prefer **time-to-depreciation** when a concrete date is known (checklist 30d / 90d bands).
    Otherwise fall back to semver vs minimum **policy** versions.
    """
    t = today or date.today()
    dep_s = (depreciation_date_iso_str or "").strip()
    if dep_s and dep_s != "—":
        try:
            dep = datetime.strptime(dep_s, "%Y-%m-%d").date()
            days_left = (dep - t).days
            return color_from_days_until_depreciation(days_left)
        except ValueError:
            pass

    pv = parse_semver_prefix(version)
    if pv is None:
        return "Yellow"
    target = min_splunk if distribution == "Splunk" else min_oss
    if distribution == "Unknown":
        target = min_oss
    if semver_lt(pv, target):
        return "Yellow"
    return "Green"


def render_otel_collectors_markdown(report: dict[str, Any] | None) -> str:
    if not report or report.get("error"):
        err = str(report.get("error") or "") if report else ""
        msg = "*None — check not executed.*"
        if err.strip():
            msg = f"*OpenTelemetry Collectors automation failed ({_md_cell(err[:200])}).*"
        c = OTEL_CHECKLIST["collectorsByVersion"]
        empty = (
            "| Color | Host Name | Host ID | Deployment context | OTel Collector Name | Version | Depreciation Date |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "|  |  |  |  |  |  |  |\n"
        )
        return (
            "## OpenTelemetry Collectors\n\n"
            f"### {c['title']}\n\n"
            f"{c['description']}\n\n"
            "### Results\n\n"
            f"{empty}"
            "### Recommendation\n\n"
            f"{msg}\n\n"
        )

    chk = (report.get("checks") or {}).get("collectorsByVersion") or {}
    rows = sorted(chk.get("rows") or [], key=collector_list_by_version_sort_key)
    c = OTEL_CHECKLIST["collectorsByVersion"]
    desc = str(chk.get("description") or c["description"]).strip()
    rec = str(chk.get("recommendation") or c["recommendation"]).strip()

    lines = [
        "## OpenTelemetry Collectors\n\n",
        f"### {c['title']}\n\n",
        desc,
        "\n\n### Results\n\n",
        "| Color | Host Name | Host ID | Deployment context | OTel Collector Name | Version | Depreciation Date |\n",
        "| --- | --- | --- | --- | --- | --- | --- |\n",
    ]
    for r in rows:
        lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_cell(str(r.get('hostName')))} | "
            f"{_md_cell(str(r.get('hostId')))} | {_md_cell(str(r.get('hostContext')))} | "
            f"{_md_cell(str(r.get('collectorName')))} | {_md_cell(str(r.get('version')))} | "
            f"{_md_cell(str(r.get('depreciationDate')))} |\n"
        )
    if not rows:
        lines.append("|  |  |  |  |  |  |  |\n")
    lines.extend(["\n### Recommendation\n\n", rec, "\n\n"])
    return "".join(lines)


@dataclass
class OtelCollectorsConfig:
    realm: str
    lookback_hours: int
    resolution_minutes: int
    min_splunk_version: str
    min_oss_version: str
    #: Days after GitHub **release** date treated as end of support (default ~2 years).
    support_days: int = 730
    #: If True, do not call GitHub — all depreciation dates **—**, semver-only colors.
    skip_github_catalog: bool = False
    #: Optional ``GITHUB_TOKEN`` override for higher GitHub API rate limits.
    github_token: str | None = None


def run_otel_collectors_health(token: str, cfg: OtelCollectorsConfig) -> dict[str, Any]:
    """Build checklist JSON from SignalFlow-derived collector rows."""
    min_sp = parse_semver_prefix(cfg.min_splunk_version) or (0, 90, 0)
    min_os = parse_semver_prefix(cfg.min_oss_version) or (0, 95, 0)

    publish_dates: dict[str, date] = {}
    catalog_err: str | None = None
    if not cfg.skip_github_catalog:
        publish_dates, catalog_err = fetch_splunk_otel_release_publish_dates(
            github_token=cfg.github_token,
        )
        if catalog_err:
            logger.warning("Splunk OTel GitHub release catalog: %s", catalog_err[:400])
        else:
            logger.info(
                "Splunk OTel GitHub catalog: %s release tag(s) (signalfx/splunk-otel-collector)",
                len(publish_dates),
            )

    raw_rows, err = fetch_otel_inventory(
        token,
        cfg.realm,
        lookback_hours=max(1, min(cfg.lookback_hours, 168)),
        resolution_ms=max(60_000, cfg.resolution_minutes * 60_000),
    )

    support_days = max(0, int(cfg.support_days))

    out_rows: list[dict[str, Any]] = []
    for r in raw_rows:
        c_disp = str(r.get("collectorName") or "")
        dist = infer_distribution(service_name_from_collector_display(c_disp), str(r.get("version") or ""))
        ver = str(r.get("version") or "")
        vk = normalize_collector_version_key(ver)

        dep_cell = "—"
        if dist == "Splunk" and vk and vk in publish_dates:
            dep_cell = depreciation_date_iso(publish_dates[vk], support_days=support_days)

        color = classify_row(
            version=ver,
            distribution=dist,
            min_splunk=min_sp,
            min_oss=min_os,
            depreciation_date_iso_str=dep_cell if dep_cell != "—" else None,
        )
        out_rows.append(
            {
                "color": color,
                "hostName": r.get("hostName"),
                "hostId": r.get("hostId"),
                "hostContext": r.get("hostContext"),
                "collectorName": r.get("collectorName"),
                "version": ver if ver else "—",
                "depreciationDate": dep_cell,
            }
        )

    out_rows.sort(key=collector_list_by_version_sort_key)

    checks = {
        "collectorsByVersion": {
            "rows": out_rows[:5000],
            "description": OTEL_CHECKLIST["collectorsByVersion"]["description"],
            "recommendation": OTEL_CHECKLIST["collectorsByVersion"]["recommendation"],
        }
    }

    out: dict[str, Any] = {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "inventoryError": err,
        "checks": checks,
        "splunkOtelGithubCatalog": {
            "repository": SPLUNK_OTEL_GITHUB,
            "releasesUrl": f"https://github.com/{SPLUNK_OTEL_GITHUB}/releases",
            "tagsUrl": f"https://github.com/{SPLUNK_OTEL_GITHUB}/tags",
            "releasesMapped": len(publish_dates),
            "catalogError": catalog_err,
            "supportDaysAfterRelease": support_days,
            "depreciationModel": "release_published_at_plus_support_days",
            "skipped": bool(cfg.skip_github_catalog),
        },
    }
    if err:
        logger.warning("Inventory completed with warning: %s", err[:400])
    else:
        logger.debug("Classified %s collector row(s) for report", len(out_rows))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="OpenTelemetry Collectors health check (SignalFlow).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--lookback-hours", type=int, default=24, help="SignalFlow window (default 24).")
    p.add_argument("--resolution-minutes", type=int, default=5, help="Rollup resolution (default 5).")
    p.add_argument("--min-splunk-version", default="0.90.0", help="Minimum Splunk distro semver (default 0.90.0).")
    p.add_argument("--min-oss-version", default="0.90.0", help="Minimum OSS collector semver (default 0.90.0).")
    p.add_argument(
        "--support-days",
        type=int,
        default=730,
        help="Depreciation = GitHub release date + this many days (default 730 ≈ 2 years).",
    )
    p.add_argument(
        "--skip-github-catalog",
        action="store_true",
        help="Do not query GitHub for splunk-otel-collector releases; depreciation dates show as —.",
    )
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section (## OpenTelemetry Collectors).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("OpenTelemetry Collectors health check starting (SignalFlow: otelcol_process_uptime)")

    token = (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    realm = (args.realm or os.environ.get("SPLUNK_REALM") or "").strip()
    profile_path = resolve_profile_path(args.profile)
    prof: dict[str, str] = {}
    if profile_path:
        prof = load_customer_profile_scalars(profile_path)
        token = token or (prof.get("access_token") or "").strip()
        realm = realm or (prof.get("realm") or "").strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1
    if not realm:
        realm = "us0"

    logger.info("Realm: %s | lookback: %sh", realm, max(1, min(int(args.lookback_hours), 168)))

    gh_tok = (os.environ.get("GITHUB_TOKEN") or "").strip() or None

    support_days = max(0, min(int(args.support_days), 3650))
    skip_gh = bool(args.skip_github_catalog)
    if prof:
        sd_prof = (prof.get("health_check_otel_support_days") or "").strip()
        if sd_prof:
            try:
                support_days = max(0, min(int(sd_prof), 3650))
            except ValueError:
                pass
        if (prof.get("health_check_otel_skip_github_catalog") or "").strip().lower() in (
            "true",
            "1",
            "yes",
        ):
            skip_gh = True

    cfg = OtelCollectorsConfig(
        realm=realm,
        lookback_hours=max(1, min(int(args.lookback_hours), 168)),
        resolution_minutes=max(1, min(int(args.resolution_minutes), 60)),
        min_splunk_version=str(args.min_splunk_version),
        min_oss_version=str(args.min_oss_version),
        support_days=support_days,
        skip_github_catalog=skip_gh,
        github_token=gh_tok,
    )
    report = run_otel_collectors_health(token, cfg)

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_otel_collectors_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    if report.get("error"):
        print(json.dumps(report, indent=2))
        logger.error("Report contains error field; exiting non-zero")
        return 1
    brief = {k: report[k] for k in ("schema", "realm", "inventoryError") if k in report}
    n = len((report.get("checks") or {}).get("collectorsByVersion", {}).get("rows") or [])
    brief["collectorRows"] = n
    cat = report.get("splunkOtelGithubCatalog")
    if isinstance(cat, dict):
        brief["splunkOtelGithubReleasesMapped"] = cat.get("releasesMapped")
        if cat.get("catalogError"):
            brief["splunkOtelGithubCatalogError"] = cat.get("catalogError")
    print(json.dumps(brief, indent=2))
    logger.info("Done: collectorRows=%s", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
